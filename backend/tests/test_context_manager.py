"""
Tests for backend/context_manager.py

All tests use a temporary directory via the `ctx` fixture — the real
.autonomous-team/project-context.json is never touched.

**Known production bug documented here (not fixed):**
  `_EMPTY_SKELETON` is a module-level dict whose list values are shared
  references. `load()` uses `dict(_EMPTY_SKELETON)` (shallow copy), so
  mutations via `ctx.setdefault(...)` propagate back into the module-level
  constant. The `reset_skeleton` autouse fixture patches this between tests
  so each test gets a clean slate without touching production code.

Run with:
    python3 -m pytest backend/tests/test_context_manager.py -v
"""

from __future__ import annotations

import copy
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.context_manager as cm_mod
from backend.context_manager import LockTimeout, ProjectContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_skeleton():
    """
    Reset _EMPTY_SKELETON before every test.

    Production bug: dict(_EMPTY_SKELETON) is a shallow copy, so list
    values are shared. Mutations from one test leak into subsequent tests
    via the module-level constant.  We patch the constant with a deep copy
    before each test and restore afterward, without touching production code.
    """
    original = {k: copy.deepcopy(v) for k, v in cm_mod._EMPTY_SKELETON.items()}
    with patch.object(cm_mod, "_EMPTY_SKELETON", original):
        yield


@pytest.fixture()
def ctx(tmp_path: Path) -> ProjectContext:
    """ProjectContext isolated to a temp directory — never touches real state."""
    return ProjectContext(state_dir=tmp_path / "state")


# ---------------------------------------------------------------------------
# Isolation guard — verify no real state dir is used
# ---------------------------------------------------------------------------


def test_fixture_uses_temp_path(tmp_path: Path) -> None:
    """The fixture state_dir is inside tmp_path, not .autonomous-team/."""
    c = ProjectContext(state_dir=tmp_path / "state")
    real = Path(__file__).resolve().parent.parent.parent / ".autonomous-team" / "project-context.json"
    assert c._data_path != real


def test_bug_documented_shallow_copy_isolation(tmp_path: Path) -> None:
    """
    Documents the _EMPTY_SKELETON shallow-copy bug.

    Without the reset_skeleton fixture, appending to the list returned by
    load() would mutate the module-level constant. The reset_skeleton fixture
    prevents cross-test contamination.
    """
    # With the fixture active, fresh load() returns empty lists
    ctx = ProjectContext(state_dir=tmp_path / "state")
    result = ctx.load()
    assert result["goals"] == [], "Expected clean skeleton each test"


# ---------------------------------------------------------------------------
# load — missing / corrupt file defaults
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_skeleton(ctx: ProjectContext) -> None:
    """load() returns the empty skeleton when no file exists yet."""
    result = ctx.load()
    assert result["goals"] == []
    assert result["decisions"] == []
    assert result["milestones"] == []
    assert result["stack"] == []
    assert result["banned"] == []
    assert result["version"] == 1


def test_load_corrupt_json_returns_skeleton(ctx: ProjectContext) -> None:
    """load() returns the empty skeleton when the file contains invalid JSON."""
    ctx._state_dir.mkdir(parents=True, exist_ok=True)
    ctx._data_path.write_text("NOT { VALID JSON !!!", encoding="utf-8")
    result = ctx.load()
    assert result["goals"] == []


def test_load_partial_json_fills_missing_keys(ctx: ProjectContext) -> None:
    """load() merges partial data with the skeleton so all keys are present."""
    ctx._state_dir.mkdir(parents=True, exist_ok=True)
    ctx._data_path.write_text(json.dumps({"version": 1, "goals": []}), encoding="utf-8")
    result = ctx.load()
    assert "decisions" in result
    assert "stack" in result
    assert "banned" in result
    assert "milestones" in result


# ---------------------------------------------------------------------------
# add_goal / get_goals
# ---------------------------------------------------------------------------


def test_add_goal_returns_id(ctx: ProjectContext) -> None:
    """add_goal() returns a string ID."""
    gid = ctx.add_goal("Build interactive TUI")
    assert isinstance(gid, str)
    assert gid.startswith("g")


def test_add_goal_read_back(ctx: ProjectContext) -> None:
    """add_goal() persists the goal and get_goals() retrieves it."""
    ctx.add_goal("First goal", status="in-progress")
    goals = ctx.get_goals()
    assert len(goals) == 1
    assert goals[0]["text"] == "First goal"
    assert goals[0]["status"] == "in-progress"


def test_add_goal_default_status_in_progress(ctx: ProjectContext) -> None:
    """Default status for a new goal is 'in-progress'."""
    ctx.add_goal("Some goal")
    goals = ctx.get_goals()
    assert goals[0]["status"] == "in-progress"


def test_add_goal_done_status(ctx: ProjectContext) -> None:
    """add_goal() accepts 'done' as the status."""
    ctx.add_goal("Completed goal", status="done")
    goals = ctx.get_goals()
    assert goals[0]["status"] == "done"


def test_add_multiple_goals_incremental_ids(ctx: ProjectContext) -> None:
    """Multiple goals get sequential IDs (g1, g2, g3 …)."""
    g1 = ctx.add_goal("Goal A")
    g2 = ctx.add_goal("Goal B")
    g3 = ctx.add_goal("Goal C")
    assert g1 == "g1"
    assert g2 == "g2"
    assert g3 == "g3"
    assert len(ctx.get_goals()) == 3


def test_add_goal_has_added_date(ctx: ProjectContext) -> None:
    """Each goal entry has an 'added' field with a YYYY-MM-DD date string."""
    ctx.add_goal("Dated goal")
    goal = ctx.get_goals()[0]
    assert "added" in goal
    assert len(goal["added"]) == 10  # YYYY-MM-DD


# ---------------------------------------------------------------------------
# add_decision / get_decisions
# ---------------------------------------------------------------------------


def test_add_decision_returns_id(ctx: ProjectContext) -> None:
    """add_decision() returns a string ID starting with 'd'."""
    did = ctx.add_decision("Use ink for TUI")
    assert did.startswith("d")


def test_add_decision_read_back(ctx: ProjectContext) -> None:
    """add_decision() persists and get_decisions() retrieves it."""
    ctx.add_decision("Use TypeScript", rationale="Strong types")
    decisions = ctx.get_decisions()
    assert len(decisions) == 1
    assert decisions[0]["text"] == "Use TypeScript"
    assert decisions[0]["rationale"] == "Strong types"


def test_add_decision_without_rationale(ctx: ProjectContext) -> None:
    """Decisions without rationale omit the 'rationale' key."""
    ctx.add_decision("Some choice")
    d = ctx.get_decisions()[0]
    assert d.get("rationale", "") == ""


def test_add_decision_with_discussion(ctx: ProjectContext) -> None:
    """Decisions can carry an optional discussion reference."""
    ctx.add_decision("Major decision", discussion=42)
    d = ctx.get_decisions()[0]
    assert d.get("discussion") == 42


def test_add_multiple_decisions_incremental_ids(ctx: ProjectContext) -> None:
    """Multiple decisions get sequential IDs (d1, d2 …)."""
    ctx.add_decision("A")
    ctx.add_decision("B")
    ids = [d["id"] for d in ctx.get_decisions()]
    assert ids == ["d1", "d2"]


# ---------------------------------------------------------------------------
# add_milestone / mark_milestone_done / get_milestones
# ---------------------------------------------------------------------------


def test_add_milestone_returns_id(ctx: ProjectContext) -> None:
    """add_milestone() returns a string ID starting with 'm'."""
    mid = ctx.add_milestone("Ship TUI MVP")
    assert mid.startswith("m")


def test_add_milestone_read_back(ctx: ProjectContext) -> None:
    """add_milestone() persists and get_milestones() retrieves it."""
    ctx.add_milestone("Release v1")
    milestones = ctx.get_milestones()
    assert len(milestones) == 1
    assert milestones[0]["text"] == "Release v1"
    assert milestones[0]["status"] == "pending"


def test_mark_milestone_done_returns_true(ctx: ProjectContext) -> None:
    """mark_milestone_done() returns True when the milestone exists."""
    mid = ctx.add_milestone("Finish docs")
    assert ctx.mark_milestone_done(mid) is True


def test_mark_milestone_done_updates_status(ctx: ProjectContext) -> None:
    """After mark_milestone_done(), the milestone status is 'done'."""
    mid = ctx.add_milestone("Write tests")
    ctx.mark_milestone_done(mid)
    m = ctx.get_milestones()[0]
    assert m["status"] == "done"


def test_mark_milestone_done_with_pr(ctx: ProjectContext) -> None:
    """mark_milestone_done() can attach a PR number."""
    mid = ctx.add_milestone("Implement feature")
    ctx.mark_milestone_done(mid, pr=99)
    m = ctx.get_milestones()[0]
    assert m.get("pr") == 99


def test_mark_milestone_done_missing_returns_false(ctx: ProjectContext) -> None:
    """mark_milestone_done() returns False when the ID doesn't exist."""
    result = ctx.mark_milestone_done("m999")
    assert result is False


def test_add_milestone_with_discussion(ctx: ProjectContext) -> None:
    """Milestones can carry an optional discussion reference."""
    ctx.add_milestone("Big feature", discussion=7)
    m = ctx.get_milestones()[0]
    assert m.get("discussion") == 7


# ---------------------------------------------------------------------------
# add_banned / get_banned
# ---------------------------------------------------------------------------


def test_add_banned_returns_id(ctx: ProjectContext) -> None:
    """add_banned() returns an ID starting with 'b'."""
    bid = ctx.add_banned("tmux send-keys")
    assert bid.startswith("b")


def test_add_banned_read_back(ctx: ProjectContext) -> None:
    """add_banned() persists and get_banned() retrieves it."""
    ctx.add_banned("git rm", reason="archive protocol")
    banned = ctx.get_banned()
    assert len(banned) == 1
    assert banned[0]["approach"] == "git rm"
    assert banned[0]["reason"] == "archive protocol"


def test_add_banned_without_reason(ctx: ProjectContext) -> None:
    """Banned entries without a reason omit the 'reason' key."""
    ctx.add_banned("bad approach")
    b = ctx.get_banned()[0]
    assert b.get("reason", "") == ""


def test_add_multiple_banned_incremental_ids(ctx: ProjectContext) -> None:
    """Multiple banned entries get sequential IDs."""
    ctx.add_banned("X")
    ctx.add_banned("Y")
    ids = [b["id"] for b in ctx.get_banned()]
    assert ids == ["b1", "b2"]


# ---------------------------------------------------------------------------
# add_stack / get_stack
# ---------------------------------------------------------------------------


def test_add_stack_entry(ctx: ProjectContext) -> None:
    """add_stack() persists a tech stack entry."""
    ctx.add_stack("TypeScript + ink (TUI)")
    stack = ctx.get_stack()
    assert "TypeScript + ink (TUI)" in stack


def test_add_stack_deduplication(ctx: ProjectContext) -> None:
    """add_stack() does not add duplicates."""
    ctx.add_stack("Python 3.11")
    ctx.add_stack("Python 3.11")
    assert ctx.get_stack().count("Python 3.11") == 1


def test_add_multiple_stack_entries(ctx: ProjectContext) -> None:
    """Multiple distinct stack entries are all stored."""
    ctx.add_stack("TypeScript")
    ctx.add_stack("Python")
    ctx.add_stack("SQLite")
    assert len(ctx.get_stack()) == 3


# ---------------------------------------------------------------------------
# Persistence round-trip — write then reload from disk
# ---------------------------------------------------------------------------


def test_round_trip_reload(tmp_path: Path) -> None:
    """Data written by one ProjectContext instance is readable by another on the same path."""
    state_dir = tmp_path / "shared"
    a = ProjectContext(state_dir=state_dir)
    a.add_goal("Shared goal")
    a.add_decision("Shared decision", rationale="because")
    a.add_banned("bad thing", reason="it breaks")
    a.add_stack("Rust")
    mid = a.add_milestone("First release")
    a.mark_milestone_done(mid, pr=1)

    # New instance pointing at the same directory
    b = ProjectContext(state_dir=state_dir)
    assert b.get_goals()[0]["text"] == "Shared goal"
    assert b.get_decisions()[0]["rationale"] == "because"
    assert b.get_banned()[0]["approach"] == "bad thing"
    assert "Rust" in b.get_stack()
    assert b.get_milestones()[0]["status"] == "done"


def test_persistence_file_is_valid_json(ctx: ProjectContext) -> None:
    """The context file on disk is always valid JSON after a write."""
    ctx.add_goal("Test goal")
    raw = ctx._data_path.read_text(encoding="utf-8")
    parsed = json.loads(raw)  # must not raise
    assert "goals" in parsed


def test_save_sets_updated_at_and_updated_by(ctx: ProjectContext) -> None:
    """save() stamps updated_at (ISO-8601) and updated_by on every write."""
    ctx.add_goal("Any goal", updated_by="test-runner")
    data = ctx.load()
    assert "updated_at" in data
    assert data["updated_at"]  # non-empty
    assert "T" in data["updated_at"]  # ISO-8601 contains T separator
    assert data["updated_by"] == "test-runner"


# ---------------------------------------------------------------------------
# Atomic write — no partial/corrupt file visible
# ---------------------------------------------------------------------------


def test_atomic_write_tmp_cleaned_up(ctx: ProjectContext) -> None:
    """No .tmp file remains after a successful write."""
    ctx.add_goal("Check cleanup")
    tmp = ctx._data_path.with_suffix(".tmp")
    assert not tmp.exists(), "Stale .tmp file left behind after atomic write"


def test_atomic_write_result_is_complete(ctx: ProjectContext) -> None:
    """Written file is a complete JSON object (not truncated)."""
    ctx.add_goal("Full write check")
    text = ctx._data_path.read_text(encoding="utf-8")
    assert text.strip().endswith("}")
    parsed = json.loads(text)
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------


def test_format_for_prompt_contains_headers(ctx: ProjectContext) -> None:
    """format_for_prompt() always starts with the Project Context heading."""
    result = ctx.format_for_prompt()
    assert "## Project Context" in result


def test_format_for_prompt_shows_goal(ctx: ProjectContext) -> None:
    """A goal added to the context appears in the prompt output."""
    ctx.add_goal("Ship MVP", status="in-progress")
    result = ctx.format_for_prompt()
    assert "Ship MVP" in result
    assert "[in-progress]" in result


def test_format_for_prompt_shows_done_goal(ctx: ProjectContext) -> None:
    """A done goal is marked [done] in the prompt output."""
    ctx.add_goal("Old goal", status="done")
    result = ctx.format_for_prompt()
    assert "[done]" in result


def test_format_for_prompt_shows_decision_with_rationale(ctx: ProjectContext) -> None:
    """Decisions with rationale appear as 'text — rationale' in the prompt."""
    ctx.add_decision("Use Python", rationale="team familiarity")
    result = ctx.format_for_prompt()
    assert "Use Python" in result
    assert "team familiarity" in result


def test_format_for_prompt_shows_milestone_pending(ctx: ProjectContext) -> None:
    """Pending milestones appear as unchecked items '- [ ]' in the prompt."""
    ctx.add_milestone("Pending task")
    result = ctx.format_for_prompt()
    assert "- [ ]" in result
    assert "Pending task" in result


def test_format_for_prompt_shows_milestone_done(ctx: ProjectContext) -> None:
    """Done milestones appear as checked items '- [x]' in the prompt."""
    mid = ctx.add_milestone("Done task")
    ctx.mark_milestone_done(mid, pr=5)
    result = ctx.format_for_prompt()
    assert "- [x]" in result
    assert "PR #5" in result


def test_format_for_prompt_shows_banned(ctx: ProjectContext) -> None:
    """Banned approaches appear in the prompt output."""
    ctx.add_banned("git rm", reason="archive protocol")
    result = ctx.format_for_prompt()
    assert "git rm" in result
    assert "archive protocol" in result


def test_format_for_prompt_shows_stack(ctx: ProjectContext) -> None:
    """Tech stack entries appear in the prompt."""
    ctx.add_stack("TypeScript + ink")
    result = ctx.format_for_prompt()
    assert "TypeScript + ink" in result


def test_format_for_prompt_truncates_long_content(ctx: ProjectContext) -> None:
    """format_for_prompt() truncates output longer than 2000 characters."""
    for i in range(100):
        ctx.add_goal(f"Goal number {i} with a fairly long description to pad length")
    result = ctx.format_for_prompt()
    assert len(result) <= 2000


def test_format_for_prompt_empty_context_no_section_headers(tmp_path: Path) -> None:
    """An empty context (fresh file with no data) emits no section headers."""
    # Use a fresh path guaranteed to have no data
    fresh = ProjectContext(state_dir=tmp_path / "empty-state")
    result = fresh.format_for_prompt()
    assert "### Goals" not in result
    assert "### Key Decisions" not in result
    assert "### Milestones" not in result
    assert "### Banned Approaches" not in result


# ---------------------------------------------------------------------------
# Concurrency — thread safety of flock-based write
# ---------------------------------------------------------------------------


def test_concurrent_add_goals_no_data_loss(tmp_path: Path) -> None:
    """Multiple threads adding goals concurrently all succeed without data loss."""
    # Use a fresh isolated ctx so goal count starts at 0
    fresh = ProjectContext(state_dir=tmp_path / "concurrent-state")
    errors: list[Exception] = []

    def add_one(i: int) -> None:
        try:
            fresh.add_goal(f"Concurrent goal {i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add_one, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
    goals = fresh.get_goals()
    assert len(goals) == 8


def test_concurrent_mixed_writes_no_corruption(tmp_path: Path) -> None:
    """Concurrent writes of different types leave a valid, complete JSON file."""
    fresh = ProjectContext(state_dir=tmp_path / "mixed-concurrent-state")

    def add_goals() -> None:
        for i in range(3):
            fresh.add_goal(f"Thread goal {i}")

    def add_banned() -> None:
        for i in range(3):
            fresh.add_banned(f"bad approach {i}")

    t1 = threading.Thread(target=add_goals)
    t2 = threading.Thread(target=add_banned)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    raw = fresh._data_path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert isinstance(parsed.get("goals"), list)
    assert isinstance(parsed.get("banned"), list)
