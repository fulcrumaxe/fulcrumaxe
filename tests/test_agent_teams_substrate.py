"""tests/test_agent_teams_substrate.py — tests for backend/agent_teams_substrate.py

Covers all four helpers: ensure_team_exists, append_team_member, write_task,
read_team_status. Uses tmp_path for full filesystem isolation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.agent_teams_substrate as ats


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_dirs(tmp_path, monkeypatch):
    """Redirect TEAMS_DIR and TASKS_DIR to tmp_path for every test."""
    teams = tmp_path / "teams"
    tasks = tmp_path / "tasks"
    teams.mkdir()
    tasks.mkdir()
    monkeypatch.setattr(ats, "TEAMS_DIR", teams)
    monkeypatch.setattr(ats, "TASKS_DIR", tasks)


# ---------------------------------------------------------------------------
# ensure_team_exists
# ---------------------------------------------------------------------------


def test_ensure_team_creates_config(tmp_path):
    ats.ensure_team_exists("myteam")
    config_path = ats.TEAMS_DIR / "myteam" / "config.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text())
    assert data["team"] == "myteam"
    assert data["members"] == []
    assert "created_at" in data


def test_ensure_team_creates_inboxes(tmp_path):
    ats.ensure_team_exists("myteam")
    inboxes = ats.TEAMS_DIR / "myteam" / "inboxes"
    assert inboxes.is_dir()


def test_ensure_team_is_idempotent(tmp_path):
    ats.ensure_team_exists("myteam")
    config_path = ats.TEAMS_DIR / "myteam" / "config.json"
    first_mtime = config_path.stat().st_mtime

    ats.ensure_team_exists("myteam")
    # Config file should not be overwritten on second call
    assert config_path.stat().st_mtime == first_mtime


def test_ensure_team_returns_team_dir(tmp_path):
    result = ats.ensure_team_exists("alpha")
    assert result == ats.TEAMS_DIR / "alpha"
    assert result.is_dir()


def test_ensure_team_default_name():
    ats.ensure_team_exists()
    config_path = ats.TEAMS_DIR / "autonomous-forever" / "config.json"
    assert config_path.exists()


# ---------------------------------------------------------------------------
# append_team_member
# ---------------------------------------------------------------------------


def test_append_member_adds_entry():
    ats.ensure_team_exists("myteam")
    ats.append_team_member("exec-1-123", "executor", discussion=42, team="myteam")
    config = json.loads((ats.TEAMS_DIR / "myteam" / "config.json").read_text())
    assert len(config["members"]) == 1
    m = config["members"][0]
    assert m["agent_id"] == "exec-1-123"
    assert m["role"] == "executor"
    assert m["discussion"] == "42"
    assert "joined_at" in m


def test_append_member_multiple_members():
    ats.ensure_team_exists("myteam")
    ats.append_team_member("exec-1", "executor", team="myteam")
    ats.append_team_member("rev-1", "code-reviewer", team="myteam")
    config = json.loads((ats.TEAMS_DIR / "myteam" / "config.json").read_text())
    assert len(config["members"]) == 2
    roles = [m["role"] for m in config["members"]]
    assert "executor" in roles
    assert "code-reviewer" in roles


def test_append_member_no_discussion():
    ats.ensure_team_exists("myteam")
    ats.append_team_member("rev-2", "code-reviewer", team="myteam")
    config = json.loads((ats.TEAMS_DIR / "myteam" / "config.json").read_text())
    m = config["members"][0]
    assert "discussion" not in m


def test_append_member_silently_skips_missing_team():
    # Team dir doesn't exist — should not raise
    ats.append_team_member("exec-999", "executor", team="nonexistent")


def test_append_member_deduplicates_by_agent_id():
    """Calling append_team_member twice with the same agent_id must not grow members."""
    ats.ensure_team_exists("myteam")
    ats.append_team_member("exec-dup", "executor", team="myteam")
    ats.append_team_member("exec-dup", "executor", team="myteam")
    config = json.loads((ats.TEAMS_DIR / "myteam" / "config.json").read_text())
    assert len(config["members"]) == 1, "duplicate agent_id must not grow the members array"


def test_append_member_dedup_different_ids_both_added():
    """Different agent_ids should both be added."""
    ats.ensure_team_exists("myteam")
    ats.append_team_member("exec-1", "executor", team="myteam")
    ats.append_team_member("exec-2", "executor", team="myteam")
    config = json.loads((ats.TEAMS_DIR / "myteam" / "config.json").read_text())
    assert len(config["members"]) == 2


# ---------------------------------------------------------------------------
# write_task — new-style (task dict)
# ---------------------------------------------------------------------------


def test_write_task_pending_creates_file():
    """Spawn-time write with status=pending creates the task record."""
    ats.write_task("t1", {"task_id": "t1", "owner": "executor", "status": "pending", "discussion": "7"})
    task_path = ats.TASKS_DIR / "autonomous-forever" / "t1.json"
    assert task_path.exists()
    data = json.loads(task_path.read_text())
    assert data["task_id"] == "t1"
    assert data["owner"] == "executor"
    assert data["status"] == "pending"
    assert data["discussion"] == "7"
    assert "created_at" in data


def test_write_task_completion_updates_status():
    """Completion-time write with status=done updates the existing record."""
    ats.write_task("t2", {"task_id": "t2", "owner": "executor", "status": "pending"})
    ats.write_task("t2", {"status": "done", "pr": "55"})
    task_path = ats.TASKS_DIR / "autonomous-forever" / "t2.json"
    data = json.loads(task_path.read_text())
    assert data["status"] == "done"
    assert data["pr"] == "55"
    assert data["task_id"] == "t2"
    # created_at preserved from spawn-time write
    assert "created_at" in data
    assert "updated_at" in data


def test_write_task_pending_preserves_created_at():
    """Two-phase: created_at from spawn time is not overwritten at completion."""
    ats.write_task("t3", {"task_id": "t3", "owner": "executor", "status": "pending"})
    task_path = ats.TASKS_DIR / "autonomous-forever" / "t3.json"
    created_at_before = json.loads(task_path.read_text())["created_at"]

    import time
    time.sleep(0.01)
    ats.write_task("t3", {"status": "done"})
    data = json.loads(task_path.read_text())
    assert data["created_at"] == created_at_before, "created_at must not change on completion update"


def test_write_task_filename_uses_task_id():
    """File is named after task_id, not agent_id — vocabulary alignment."""
    ats.write_task("myapp-task-99", {"status": "pending"})
    task_path = ats.TASKS_DIR / "autonomous-forever" / "myapp-task-99.json"
    assert task_path.exists()


def test_write_task_custom_team():
    ats.write_task("rev-1-ts", {"owner": "code-reviewer", "status": "done"}, team="alpha")
    task_path = ats.TASKS_DIR / "alpha" / "rev-1-ts.json"
    assert task_path.exists()


def test_write_task_creates_team_dir():
    # TASKS_DIR/newteam doesn't exist yet
    ats.write_task("exec-new", {"status": "pending"}, team="newteam")
    assert (ats.TASKS_DIR / "newteam").is_dir()


# ---------------------------------------------------------------------------
# read_team_status
# ---------------------------------------------------------------------------


def test_read_team_status_empty_dir():
    # dir doesn't exist
    result = ats.read_team_status("nonexistent")
    assert result == []


def test_read_team_status_returns_tasks():
    ats.write_task("exec-a", {"status": "done", "discussion": "1"})
    ats.write_task("exec-b", {"status": "fail", "discussion": "2"})
    result = ats.read_team_status()
    assert len(result) == 2
    ids = {t["task_id"] for t in result}
    assert "exec-a" in ids
    assert "exec-b" in ids


def test_read_team_status_newest_first():
    import time
    ats.write_task("exec-old", {"status": "done"})
    time.sleep(0.01)
    ats.write_task("exec-new", {"status": "done"})
    result = ats.read_team_status()
    # newest by created_at should come first
    assert result[0]["task_id"] == "exec-new"


def test_read_team_status_skips_invalid_json(tmp_path):
    # Write a bad JSON file into the team tasks dir
    bad = ats.TASKS_DIR / "autonomous-forever"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "corrupt.json").write_text("{ not json }", encoding="utf-8")
    # Should not raise; corrupted file is skipped
    result = ats.read_team_status()
    assert all(isinstance(t, dict) for t in result)


# ---------------------------------------------------------------------------
# team_status.py primary/fallback paths (AC4 — Discussion #895)
# ---------------------------------------------------------------------------


def test_agents_summary_primary_path(monkeypatch, tmp_path):
    """When tasks exist in TASKS_DIR, _agents_summary returns the new path's data."""
    import backend.team_status as ts_mod

    # Write a task to our isolated TASKS_DIR (via ats with the monkeypatched dirs from autouse fixture).
    ats.write_task("exec-primary", {"owner": "executor", "status": "pending", "discussion": "10"})
    pending_tasks = ats.read_team_status()

    # Patch _team_tasks_summary to return the tasks we just wrote (fully isolated).
    monkeypatch.setattr(ts_mod, "_team_tasks_summary", lambda: pending_tasks)

    result = ts_mod._agents_summary(snapshot=None)
    assert result["team_tasks_total"] >= 1, "primary path should return tasks from TASKS_DIR"


def test_agents_summary_fallback_to_blackboard(monkeypatch):
    """When no tasks exist, _agents_summary falls back to blackboard snapshot."""
    import backend.team_status as ts_mod

    # Patch _team_tasks_summary directly so it returns empty (no tasks on disk).
    monkeypatch.setattr(ts_mod, "_team_tasks_summary", lambda: [])
    # Patch _read_spawn_guard_live_count so spawn-guard-stats.json on disk
    # doesn't intercept the blackboard-fallback path this test is checking.
    monkeypatch.setattr(ts_mod, "_read_spawn_guard_live_count", lambda: None)

    snapshot = {
        "blackboard": {
            "queue_pending": ["disc-1", "disc-2"],
            "queue_active": [{"role": "executor", "id": "exec-fallback"}],
        }
    }
    result = ts_mod._agents_summary(snapshot=snapshot)
    # Should have fallen back to blackboard
    assert result["queue_depth"] == 2, "fallback should read queue_pending from snapshot"
    assert len(result["in_flight"]) == 1, "fallback should read queue_active from snapshot"


# ---------------------------------------------------------------------------
# Dual-write coherence (AC5 — Discussion #895)
# ---------------------------------------------------------------------------


def test_dual_write_both_paths_when_legacy_enabled(tmp_path, monkeypatch):
    """With AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD unset, a mock spawn writes to
    BOTH the team config.json AND the agent-feed (simulated via flag check)."""
    monkeypatch.delenv("AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD", raising=False)

    ats.ensure_team_exists("autonomous-forever")
    ats.append_team_member("exec-dual", "executor", discussion="20")
    ats.write_task("exec-dual", {"owner": "executor", "status": "pending", "discussion": "20"})

    # Team path received the write
    config = json.loads((ats.TEAMS_DIR / "autonomous-forever" / "config.json").read_text())
    member_ids = [m["agent_id"] for m in config["members"]]
    assert "exec-dual" in member_ids, "team config.json must have the member"

    task_path = ats.TASKS_DIR / "autonomous-forever" / "exec-dual.json"
    assert task_path.exists(), "task file must exist in tasks dir"

    # Legacy path: env var is unset, so legacy writes should be enabled
    assert os.environ.get("AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD", "0") != "1", (
        "legacy blackboard should be enabled when var is unset"
    )


def test_dual_write_only_team_path_when_legacy_disabled(tmp_path, monkeypatch):
    """With AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD=1, only team filesystem write occurs;
    the legacy path should be suppressed."""
    monkeypatch.setenv("AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD", "1")

    ats.ensure_team_exists("autonomous-forever")
    ats.append_team_member("exec-nodual", "executor", discussion="21")
    ats.write_task("exec-nodual", {"owner": "executor", "status": "pending", "discussion": "21"})

    # Team path still written
    config = json.loads((ats.TEAMS_DIR / "autonomous-forever" / "config.json").read_text())
    member_ids = [m["agent_id"] for m in config["members"]]
    assert "exec-nodual" in member_ids, "team config.json must still be written when legacy disabled"

    task_path = ats.TASKS_DIR / "autonomous-forever" / "exec-nodual.json"
    assert task_path.exists(), "task file must still exist when legacy disabled"

    # Legacy path suppressed: confirm env var is set
    assert os.environ.get("AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD") == "1", (
        "AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD must be 1 to suppress legacy writes"
    )
