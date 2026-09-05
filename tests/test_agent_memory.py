"""
Tests for backend/agent_memory.py — AgentMemory class.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
from backend.agent_memory import AgentMemory


@pytest.fixture
def mem(tmp_path):
    """Isolated AgentMemory instance backed by a temp blackboard and temp SQLite db."""
    from backend.db import Database
    bb = Blackboard(root=tmp_path / "blackboard")
    db = Database(db_path=tmp_path / "state.db")
    return AgentMemory(bb=bb, db=db)


# -----------------------------------------------------------------------
# 1. record_lesson
# -----------------------------------------------------------------------

def test_record_returns_nonempty_key(mem):
    key = mem.record_lesson(
        discussion=14,
        role="executor",
        lesson_type="failure",
        content="type error in AgentFeed",
        files=["tui/src/AgentFeed.tsx"],
        tags=["type-error"],
    )
    assert isinstance(key, str) and len(key) > 0


def test_record_stores_retrievable_lesson(mem):
    mem.record_lesson(
        discussion=14,
        role="executor",
        lesson_type="failure",
        content="type error in AgentFeed",
        files=["tui/src/AgentFeed.tsx"],
        tags=["type-error"],
    )
    lessons = mem.query_lessons(files=["tui/src/AgentFeed.tsx"])
    assert len(lessons) == 1
    assert lessons[0]["content"] == "type error in AgentFeed"
    assert lessons[0]["discussion"] == 14
    assert lessons[0]["role"] == "executor"
    assert lessons[0]["lesson_type"] == "failure"


# -----------------------------------------------------------------------
# 2. query_lessons — by files
# -----------------------------------------------------------------------

def test_query_by_files_matches_shared_file(mem):
    mem.record_lesson(14, "executor", "failure", "msg A", ["a.py", "b.py"], [])
    mem.record_lesson(15, "executor", "success", "msg B", ["c.py"], [])
    results = mem.query_lessons(files=["a.py"])
    assert len(results) == 1
    assert results[0]["content"] == "msg A"


def test_query_by_files_no_match_returns_empty(mem):
    mem.record_lesson(14, "executor", "failure", "msg A", ["a.py"], [])
    results = mem.query_lessons(files=["z.py"])
    assert results == []


# -----------------------------------------------------------------------
# 3. query_lessons — by tags
# -----------------------------------------------------------------------

def test_query_by_tags_matches_shared_tag(mem):
    mem.record_lesson(14, "executor", "failure", "type err", ["a.py"], ["type-error"])
    mem.record_lesson(15, "executor", "success", "import ok", ["b.py"], ["import-ok"])
    results = mem.query_lessons(tags=["type-error"])
    assert len(results) == 1
    assert results[0]["content"] == "type err"


def test_query_by_tags_no_match_returns_empty(mem):
    mem.record_lesson(14, "executor", "failure", "msg", ["a.py"], ["type-error"])
    results = mem.query_lessons(tags=["unknown-tag"])
    assert results == []


# -----------------------------------------------------------------------
# 4. query_lessons — by role
# -----------------------------------------------------------------------

def test_query_by_role_filters_correctly(mem):
    mem.record_lesson(14, "executor", "failure", "exec fail", ["a.py"], [])
    mem.record_lesson(15, "code-reviewer", "success", "review ok", ["a.py"], [])
    results = mem.query_lessons(files=["a.py"], role="executor")
    assert len(results) == 1
    assert results[0]["role"] == "executor"


def test_query_by_role_only_filters_without_file_or_tag(mem):
    mem.record_lesson(14, "executor", "failure", "exec msg", ["a.py"], ["t1"])
    mem.record_lesson(15, "code-reviewer", "success", "rev msg", ["b.py"], ["t2"])
    # role-only filter without files/tags returns all, then filters by role
    results = mem.query_lessons(role="code-reviewer")
    assert all(l["role"] == "code-reviewer" for l in results)
    assert len(results) == 1


# -----------------------------------------------------------------------
# 5. query_lessons — limit
# -----------------------------------------------------------------------

def test_query_limit_caps_results(mem):
    for i in range(10):
        mem.record_lesson(i, "executor", "pattern", f"lesson {i}", ["shared.py"], [])
    results = mem.query_lessons(files=["shared.py"], limit=2)
    assert len(results) == 2


def test_query_limit_default_is_5(mem):
    for i in range(8):
        mem.record_lesson(i, "executor", "pattern", f"lesson {i}", ["shared.py"], [])
    results = mem.query_lessons(files=["shared.py"])
    assert len(results) == 5


# -----------------------------------------------------------------------
# 6. get_context_block
# -----------------------------------------------------------------------

def test_get_context_block_returns_formatted_string(mem):
    mem.record_lesson(14, "executor", "failure",
                      "TypeScript type error: missing prop",
                      ["tui/src/AgentFeed.tsx", "tui/src/types.ts"],
                      ["type-error"])
    block = mem.get_context_block(files=["tui/src/AgentFeed.tsx"])
    assert "lessons" in block.lower()
    assert "[failure]" in block
    assert "Discussion #14" in block
    assert "executor" in block
    assert "TypeScript type error" in block
    assert "tui/src/AgentFeed.tsx" in block


def test_get_context_block_empty_when_no_matches(mem):
    block = mem.get_context_block(files=["nonexistent.py"])
    assert block == ""


# -----------------------------------------------------------------------
# 7. prune_old — by count
# -----------------------------------------------------------------------

def test_prune_by_max_entries_removes_oldest(mem):
    # Record 5 lessons; prune to max 3 — oldest 2 should be removed
    for i in range(5):
        mem.record_lesson(i, "executor", "pattern", f"lesson {i}", [f"file{i}.py"], [])

    removed = mem.prune_old(max_age_days=9999, max_entries=3)
    # prune_old removes from both SQL (2) and blackboard (2), so at least 2 removed
    assert removed >= 2

    # Only 3 remain in SQL
    all_lessons = mem.query_lessons(limit=100)
    assert len(all_lessons) == 3


def test_prune_within_max_entries_removes_nothing(mem):
    mem.record_lesson(1, "executor", "pattern", "only one", ["a.py"], [])
    removed = mem.prune_old(max_age_days=9999, max_entries=10)
    # Nothing should be pruned — only 1 entry, well within max_entries
    assert removed == 0


# -----------------------------------------------------------------------
# 8. prune_old — by age
# -----------------------------------------------------------------------

def test_prune_by_age_removes_old_entries(tmp_path):
    """Manually inject an old lesson to test age-based pruning."""
    from backend.db import Database
    bb = Blackboard(root=tmp_path / "blackboard")
    db = Database(db_path=tmp_path / "state.db")
    mem = AgentMemory(bb=bb, db=db)

    # Write a fresh lesson via the normal path (goes to SQL)
    mem.record_lesson(2, "executor", "success", "fresh lesson", ["fresh.py"], [])

    # Directly insert an old lesson into the SQL table
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
    import uuid as _uuid
    conn = db._conn()
    conn.execute(
        "INSERT INTO agent_lessons (id, session_id, discussion, role, lesson_type, content, files, tags, created_at, relevance)"
        " VALUES (?, NULL, 1, 'executor', 'failure', 'old lesson', '[]', '[]', ?, 1.0)",
        (str(_uuid.uuid4()), old_ts),
    )
    conn.commit()

    removed = mem.prune_old(max_age_days=30, max_entries=9999)
    assert removed >= 1

    remaining = mem.query_lessons(limit=100)
    assert len(remaining) == 1
    assert remaining[0]["content"] == "fresh lesson"


# -----------------------------------------------------------------------
# 9. empty queries
# -----------------------------------------------------------------------

def test_query_no_filters_returns_all_lessons(mem):
    """No file/tag/role filter — returns all lessons (up to limit)."""
    mem.record_lesson(1, "executor", "failure", "lesson one", ["a.py"], ["t1"])
    mem.record_lesson(2, "code-reviewer", "success", "lesson two", ["b.py"], ["t2"])
    results = mem.query_lessons(limit=100)
    assert len(results) == 2


def test_query_empty_blackboard_returns_empty(mem):
    results = mem.query_lessons(files=["anything.py"], tags=["anything"])
    assert results == []


# -----------------------------------------------------------------------
# 10. CLI smoke test
# -----------------------------------------------------------------------

def test_cli_record_and_query(tmp_path):
    """Smoke test: CLI record then query via subprocess."""
    import os
    env = os.environ.copy()
    # Override the blackboard root via a custom Blackboard root — but the CLI
    # uses the default Blackboard. Instead, test the module functions directly
    # with a patched blackboard via monkeypatching would be complex;
    # verify the CLI argument parsing at least produces expected exit code.
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            sys.executable, str(repo_root / "backend" / "agent_memory.py"),
            "record",
            "--discussion", "99",
            "--role", "executor",
            "--type", "pattern",
            "--content", "CLI smoke test lesson",
            "--files", "smoke.py",
            "--tags", "smoke-test",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=env,
    )
    assert result.returncode == 0, f"CLI record failed: {result.stderr}"
    key = result.stdout.strip()
    assert key.startswith("memory/")

    # Query via CLI
    result2 = subprocess.run(
        [
            sys.executable, str(repo_root / "backend" / "agent_memory.py"),
            "query",
            "--files", "smoke.py",
            "--limit", "5",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=env,
    )
    assert result2.returncode == 0, f"CLI query failed: {result2.stderr}"
    data = json.loads(result2.stdout)
    assert isinstance(data, list)
    assert any(l.get("content") == "CLI smoke test lesson" for l in data)
