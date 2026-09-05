"""tests/test_substrate_reaper_and_inflight.py

Tests for:
  1. prune_terminal_substrate_tasks — removes old terminal records, keeps recent+running
  2. _agents_summary in team_status — in-flight count is truthful (spawn_guard beats
     stale substrate task list)

All tests use tmp_path / monkeypatch for full filesystem isolation.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.agent_teams_substrate as ats
import backend.team_status as ts_mod


# ---------------------------------------------------------------------------
# Shared fixture: redirect TASKS_DIR to a temp dir
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_tasks_dir(tmp_path, monkeypatch):
    """Redirect TASKS_DIR (and TEAMS_DIR) to tmp_path for every test in this file."""
    tasks = tmp_path / "tasks"
    teams = tmp_path / "teams"
    tasks.mkdir()
    teams.mkdir()
    monkeypatch.setattr(ats, "TASKS_DIR", tasks)
    monkeypatch.setattr(ats, "TEAMS_DIR", teams)


# ---------------------------------------------------------------------------
# Helper: write a task file with an explicit timestamp
# ---------------------------------------------------------------------------

def _write_task_file(
    tmp_tasks_dir: Path,
    task_id: str,
    status: str,
    age_days: float = 0,
    team: str = "autonomous-forever",
) -> Path:
    """Write a task JSON file directly (bypasses write_task to control timestamps)."""
    team_dir = tmp_tasks_dir / team
    team_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    record = {
        "task_id": task_id,
        "status": status,
        "created_at": created_at.isoformat(),
    }
    p = team_dir / f"{task_id}.json"
    p.write_text(json.dumps(record), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# prune_terminal_substrate_tasks — reaper tests
# ---------------------------------------------------------------------------


class TestPruneTerminalSubstrateTasks:
    """The reaper must only remove terminal-status records older than N days."""

    def test_removes_old_terminal_records(self, tmp_path):
        """Old done/fail/pass/needs-fix/skip records should be deleted."""
        tasks_dir = ats.TASKS_DIR
        for status in ("done", "fail", "pass", "needs-fix", "skip"):
            _write_task_file(tasks_dir, f"old-{status}", status, age_days=10)

        removed = ats.prune_terminal_substrate_tasks(days=7)

        assert removed == 5, f"expected 5 files removed, got {removed}"
        for status in ("done", "fail", "pass", "needs-fix", "skip"):
            p = tasks_dir / "autonomous-forever" / f"old-{status}.json"
            assert not p.exists(), f"{p.name} should have been pruned"

    def test_keeps_recent_terminal_records(self, tmp_path):
        """Terminal records created within the retention window must NOT be removed."""
        tasks_dir = ats.TASKS_DIR
        _write_task_file(tasks_dir, "recent-done", "done", age_days=2)
        _write_task_file(tasks_dir, "recent-fail", "fail", age_days=1)

        removed = ats.prune_terminal_substrate_tasks(days=7)

        assert removed == 0, "recent terminal records should not be removed"
        assert (tasks_dir / "autonomous-forever" / "recent-done.json").exists()
        assert (tasks_dir / "autonomous-forever" / "recent-fail.json").exists()

    def test_keeps_non_terminal_records_regardless_of_age(self, tmp_path):
        """Running/pending/queued tasks must NEVER be pruned, even if old."""
        tasks_dir = ats.TASKS_DIR
        for status in ("pending", "running", "in_progress", "queued"):
            _write_task_file(tasks_dir, f"active-{status}", status, age_days=30)

        removed = ats.prune_terminal_substrate_tasks(days=7)

        assert removed == 0, "non-terminal tasks must not be pruned"
        for status in ("pending", "running", "in_progress", "queued"):
            p = tasks_dir / "autonomous-forever" / f"active-{status}.json"
            assert p.exists(), f"{p.name} should still exist"

    def test_mixed_batch_of_800_stale_files(self, tmp_path):
        """Simulates the real bug: 800+ stale terminal files, a few recent ones, one live.

        After pruning, only old terminal records should be gone.
        """
        tasks_dir = ats.TASKS_DIR
        team_dir = tasks_dir / "autonomous-forever"
        team_dir.mkdir(parents=True, exist_ok=True)

        # 810 old terminal files (the observed real-world count)
        for i in range(810):
            _write_task_file(tasks_dir, f"stale-{i}", "done", age_days=10)

        # 3 recent terminal files (keep)
        _write_task_file(tasks_dir, "recent-a", "done", age_days=1)
        _write_task_file(tasks_dir, "recent-b", "fail", age_days=3)

        # 1 live running file (must keep)
        _write_task_file(tasks_dir, "live-executor", "pending", age_days=0)

        removed = ats.prune_terminal_substrate_tasks(days=7)

        assert removed == 810, f"should remove exactly 810 stale records, got {removed}"
        # Recent and live records must remain
        assert (team_dir / "recent-a.json").exists()
        assert (team_dir / "recent-b.json").exists()
        assert (team_dir / "live-executor.json").exists()

    def test_returns_zero_when_no_task_dir(self, tmp_path):
        """Should return 0 without error when team directory doesn't exist yet."""
        removed = ats.prune_terminal_substrate_tasks(team="no-such-team", days=7)
        assert removed == 0

    def test_skips_files_with_unparseable_json(self, tmp_path):
        """Corrupt JSON files should be left alone (non-fatal)."""
        tasks_dir = ats.TASKS_DIR
        team_dir = tasks_dir / "autonomous-forever"
        team_dir.mkdir(parents=True, exist_ok=True)
        bad = team_dir / "corrupt.json"
        bad.write_text("NOT JSON{{{", encoding="utf-8")

        removed = ats.prune_terminal_substrate_tasks(days=7)
        assert removed == 0
        assert bad.exists(), "corrupt file should not be touched"

    def test_respects_custom_days_threshold(self, tmp_path):
        """days=3 should remove records older than 3 days but keep 2-day-old ones."""
        tasks_dir = ats.TASKS_DIR
        _write_task_file(tasks_dir, "old-4d", "done", age_days=4)
        _write_task_file(tasks_dir, "fresh-2d", "done", age_days=2)

        removed = ats.prune_terminal_substrate_tasks(days=3)

        assert removed == 1
        assert not (tasks_dir / "autonomous-forever" / "old-4d.json").exists()
        assert (tasks_dir / "autonomous-forever" / "fresh-2d.json").exists()


# ---------------------------------------------------------------------------
# _agents_summary in-flight count — truthfulness tests
# ---------------------------------------------------------------------------


class TestAgentsSummaryInflightCount:
    """With 800+ stale substrate task files, in-flight must reflect real running count."""

    def _make_stale_tasks(self, n: int = 820) -> list[dict]:
        """Return a list of stale terminal task dicts (simulates reading 800+ files)."""
        tasks = []
        base_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        for i in range(n):
            tasks.append({
                "task_id": f"stale-{i}",
                "status": "done",  # terminal
                "created_at": base_ts,
            })
        return tasks

    def test_inflight_zero_when_spawn_guard_says_zero(self, monkeypatch, tmp_path):
        """When spawn_guard reports 0 live agents, in-flight must be 0,
        even if 820 stale non-terminal-filtered tasks are in the substrate."""
        # Patch _team_tasks_summary to return stale tasks
        stale = self._make_stale_tasks(820)
        monkeypatch.setattr(ts_mod, "_team_tasks_summary", lambda: stale)

        # Patch spawn_guard reader to report 0 live agents
        monkeypatch.setattr(ts_mod, "_read_spawn_guard_live_count", lambda: 0)

        result = ts_mod._agents_summary(snapshot=None)
        assert result["in_flight_count"] == 0, (
            f"in_flight_count must be 0 when spawn_guard says 0, got {result['in_flight_count']}"
        )
        assert result["queue_depth"] == 0, "queue_depth must be 0 when nothing is running"

    def test_inflight_matches_spawn_guard_when_agents_running(self, monkeypatch, tmp_path):
        """When spawn_guard reports N live agents, in-flight_count must equal N."""
        # Include some recent non-terminal tasks to simulate active agents
        now_iso = datetime.now(timezone.utc).isoformat()
        recent_active = [
            {"task_id": "exec-1", "status": "pending", "created_at": now_iso},
            {"task_id": "exec-2", "status": "running", "created_at": now_iso},
        ]
        # Also add a pile of stale terminal tasks (the real-world bug scenario)
        stale = self._make_stale_tasks(820)
        all_tasks = recent_active + stale

        monkeypatch.setattr(ts_mod, "_team_tasks_summary", lambda: all_tasks)
        monkeypatch.setattr(ts_mod, "_read_spawn_guard_live_count", lambda: 2)

        result = ts_mod._agents_summary(snapshot=None)
        assert result["in_flight_count"] == 2, (
            f"in_flight_count must come from spawn_guard (2), got {result['in_flight_count']}"
        )

    def test_inflight_count_never_exceeds_recent_tasks_plus_guard(self, monkeypatch):
        """in_flight_count must come from spawn_guard even if substrate has stale tasks."""
        stale = self._make_stale_tasks(776)  # the exact bug count reported
        monkeypatch.setattr(ts_mod, "_team_tasks_summary", lambda: stale)
        monkeypatch.setattr(ts_mod, "_read_spawn_guard_live_count", lambda: 0)

        result = ts_mod._agents_summary(snapshot=None)
        assert result["in_flight_count"] <= 8, (
            "in_flight_count must never exceed the concurrency cap (8), "
            f"got {result['in_flight_count']}"
        )
        assert result["in_flight_count"] == 0

    def test_fallback_to_substrate_when_spawn_guard_unavailable(self, monkeypatch, tmp_path):
        """When spawn_guard file is absent (None), fall back to recent substrate tasks."""
        now_iso = datetime.now(timezone.utc).isoformat()
        recent = [{"task_id": "exec-fallback", "status": "pending", "created_at": now_iso}]

        monkeypatch.setattr(ts_mod, "_team_tasks_summary", lambda: recent)
        monkeypatch.setattr(ts_mod, "_read_spawn_guard_live_count", lambda: None)

        result = ts_mod._agents_summary(snapshot=None)
        # Should see the 1 recent pending task
        assert result["in_flight_count"] == 1

    def test_fallback_to_blackboard_when_no_tasks_and_no_guard(self, monkeypatch):
        """Ultimate fallback: no substrate tasks and no spawn_guard → use snapshot blackboard."""
        monkeypatch.setattr(ts_mod, "_team_tasks_summary", lambda: [])
        monkeypatch.setattr(ts_mod, "_read_spawn_guard_live_count", lambda: None)

        snapshot = {
            "blackboard": {
                "queue_pending": ["d1", "d2"],
                "queue_active": [{"role": "executor", "id": "bb-exec"}],
            }
        }
        result = ts_mod._agents_summary(snapshot=snapshot)
        assert result["in_flight_count"] == 1
        assert result["queue_depth"] == 2

    def test_stale_non_terminal_tasks_not_counted_as_inflight(self, monkeypatch):
        """Substrate tasks that are non-terminal BUT old (> 20min) must not inflate in-flight."""
        # Simulate tasks that were never completed but are very old (stuck/orphaned)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        orphaned = [
            {"task_id": f"orphan-{i}", "status": "pending", "created_at": old_ts}
            for i in range(50)
        ]

        monkeypatch.setattr(ts_mod, "_team_tasks_summary", lambda: orphaned)
        monkeypatch.setattr(ts_mod, "_read_spawn_guard_live_count", lambda: 0)

        result = ts_mod._agents_summary(snapshot=None)
        assert result["in_flight_count"] == 0, (
            "old non-terminal tasks (> 20min) must not count as in-flight, "
            f"got {result['in_flight_count']}"
        )
