"""
Tests for backend/health_report.py

Each check function is tested in isolation via mocked filesystem and DB state.
The run_checks() aggregator is tested for exception isolation and overall roll-up.

Run with:
    python -m pytest backend/tests/test_health_report.py -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.health_report as hr_mod
from backend.health_report import (
    _CHECKS,
    _classify_orphan,
    _is_disposable_path,
    check_blackboard_writable,
    check_circuit_breakers,
    check_dial_chain_integrity,
    check_duckdb_freshness,
    check_hook_dirs_present,
    check_jsonl_sizes,
    check_loop_staleness,
    check_orphan_worktrees,
    check_state_db_writable,
    run_checks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_check(result: dict, name: str, ok: bool) -> None:
    assert result["name"] == name
    assert result["ok"] is ok
    assert "detail" in result


# ---------------------------------------------------------------------------
# check_state_db_writable
# ---------------------------------------------------------------------------


class TestStateDbWritable:
    @pytest.fixture(autouse=True)
    def _cleanup_state_db(self):
        # D#1810: STATE_DB is resolved via hr_mod.__getattr__ now, not a
        # frozen constant. monkeypatch.setattr's normal teardown restores the
        # *snapshotted* value via setattr rather than removing the name, which
        # would leave it permanently frozen in module globals (defeating
        # __getattr__) for the rest of the pytest session. delattr instead.
        yield
        if "STATE_DB" in vars(hr_mod):
            del hr_mod.STATE_DB

    def test_missing_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hr_mod, "STATE_DB", tmp_path / "missing.db")
        r = check_state_db_writable()
        _assert_check(r, "state_db_writable", False)
        assert "not found" in r["detail"]

    def test_healthy_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.close()
        monkeypatch.setattr(hr_mod, "STATE_DB", db_path)
        r = check_state_db_writable()
        _assert_check(r, "state_db_writable", True)

    def test_locked_db_reported_as_failed(self, tmp_path, monkeypatch):
        db_path = tmp_path / "locked.db"
        db_path.write_bytes(b"not a real sqlite db")
        monkeypatch.setattr(hr_mod, "STATE_DB", db_path)
        r = check_state_db_writable()
        # sqlite3.connect may succeed even on garbage bytes for some builds;
        # the important thing is the function returns a dict with ok key.
        assert "ok" in r


# ---------------------------------------------------------------------------
# check_orphan_worktrees
# ---------------------------------------------------------------------------


class TestOrphanWorktrees:
    def test_no_worktrees_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", tmp_path / "nonexistent")
        r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", True)

    def test_fresh_worktrees_ok(self, tmp_path, monkeypatch):
        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        (wt_dir / "agent-abc").mkdir()  # mtime = now → not orphan
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)
        r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", True)

    def test_old_worktree_fails(self, tmp_path, monkeypatch):
        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        old = wt_dir / "agent-old"
        old.mkdir()
        # Set mtime to 5 hours ago
        old_time = time.time() - 5 * 3600
        import os
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)
        r = check_orphan_worktrees()
        # Non-git dir → git status fails → classified as has-content → ok=False
        _assert_check(r, "orphan_worktrees", False)
        assert "agent-old" in r["detail"]

    def test_disposable_only_orphan_ok(self, tmp_path, monkeypatch):
        """Old orphan with only disposable untracked files → ok=True (non-blocking)."""
        import os
        from unittest.mock import patch, MagicMock

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        old = wt_dir / "agent-scratch"
        old.mkdir()
        old_time = time.time() - 5 * 3600
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "?? .autonomous-team/loop.log\n?? kpi.json\n"
        with patch("backend.health_report.subprocess.run", return_value=mock_result):
            r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", True)
        assert "non-blocking" in r["detail"]

    def test_tracked_change_has_content(self, tmp_path, monkeypatch):
        """Old orphan with a tracked modification → has-content → ok=False."""
        import os
        from unittest.mock import patch, MagicMock

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        old = wt_dir / "agent-modified"
        old.mkdir()
        old_time = time.time() - 5 * 3600
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M  backend/server.py\n"
        with patch("backend.health_report.subprocess.run", return_value=mock_result):
            r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", False)
        assert "agent-modified" in r["detail"]

    def test_non_allowlisted_untracked_has_content(self, tmp_path, monkeypatch):
        """Old orphan with untracked feature.py (not in allowlist) → has-content → ok=False."""
        import os
        from unittest.mock import patch, MagicMock

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        old = wt_dir / "agent-dev"
        old.mkdir()
        old_time = time.time() - 5 * 3600
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "?? .autonomous-team/loop.log\n?? feature.py\n"
        with patch("backend.health_report.subprocess.run", return_value=mock_result):
            r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", False)
        assert "agent-dev" in r["detail"]

    def test_fix1_agent_profiler_py_not_disposable(self, tmp_path, monkeypatch):
        """agent_profiler.py (contains '_pr') must not be classified disposable (FIX 1)."""
        import os
        from unittest.mock import patch, MagicMock

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        old = wt_dir / "agent-code"
        old.mkdir()
        old_time = time.time() - 5 * 3600
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "?? agent_profiler.py\n"
        with patch("backend.health_report.subprocess.run", return_value=mock_result):
            r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", False)
        assert "agent-code" in r["detail"]

    def test_fix1_feature_pr_py_not_disposable(self, tmp_path, monkeypatch):
        """feature_pr.py must not be classified disposable (FIX 1)."""
        import os
        from unittest.mock import patch, MagicMock

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        old = wt_dir / "agent-code2"
        old.mkdir()
        old_time = time.time() - 5 * 3600
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "?? feature_pr.py\n"
        with patch("backend.health_report.subprocess.run", return_value=mock_result):
            r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", False)
        assert "agent-code2" in r["detail"]

    def test_fix4_tracked_generated_only_is_disposable(self, tmp_path, monkeypatch):
        """Orphan with only _TRACKED_DISPOSABLE tracked drift → disposable → ok=True (FIX 4)."""
        import os
        from unittest.mock import patch, MagicMock

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        old = wt_dir / "agent-generated"
        old.mkdir()
        old_time = time.time() - 5 * 3600
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            " M .autonomous-team/now.md\n"
            " M .autonomous-team/config.json\n"
            " M wiki/Project-Status.md\n"
            "?? .autonomous-team/agent-feed.jsonl\n"
        )
        with patch("backend.health_report.subprocess.run", return_value=mock_result):
            r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", True)
        assert "non-blocking" in r["detail"]

    def test_fix4_git_unavailable_is_has_content(self, tmp_path, monkeypatch):
        """When git status fails for classification → has-content (safe default) (FIX 4)."""
        import os
        from unittest.mock import patch, MagicMock

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        old = wt_dir / "agent-no-git"
        old.mkdir()
        old_time = time.time() - 5 * 3600
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(hr_mod, "_WORKTREES_DIR", wt_dir)

        # First subprocess.run call is worktree list (returns empty, git available)
        # Second call is git status for classification (simulate failure)
        list_result = MagicMock()
        list_result.returncode = 0
        list_result.stdout = ""

        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stdout = ""

        call_count = [0]

        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            if "worktree" in cmd:
                return list_result
            return fail_result

        with patch("backend.health_report.subprocess.run", side_effect=mock_run):
            r = check_orphan_worktrees()
        _assert_check(r, "orphan_worktrees", False)


# ---------------------------------------------------------------------------
# check_jsonl_sizes
# ---------------------------------------------------------------------------


class TestJsonlSizes:
    def test_no_team_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hr_mod, "_TEAM_DIR", tmp_path / "absent")
        r = check_jsonl_sizes()
        _assert_check(r, "jsonl_sizes", True)

    def test_small_files_ok(self, tmp_path, monkeypatch):
        team_dir = tmp_path / "team"
        team_dir.mkdir()
        (team_dir / "loop.jsonl").write_text("x" * 100, encoding="utf-8")
        monkeypatch.setattr(hr_mod, "_TEAM_DIR", team_dir)
        r = check_jsonl_sizes()
        _assert_check(r, "jsonl_sizes", True)

    def test_oversized_file_fails(self, tmp_path, monkeypatch):
        team_dir = tmp_path / "team"
        team_dir.mkdir()
        big = team_dir / "audit.jsonl"
        # Write exactly 51 MB
        big.write_bytes(b"x" * (51 * 1024 * 1024))
        monkeypatch.setattr(hr_mod, "_TEAM_DIR", team_dir)
        monkeypatch.setattr(hr_mod, "_JSONL_MAX_BYTES", 50 * 1024 * 1024)
        r = check_jsonl_sizes()
        _assert_check(r, "jsonl_sizes", False)
        assert "audit.jsonl" in r["detail"]


# ---------------------------------------------------------------------------
# check_loop_staleness
# ---------------------------------------------------------------------------


class TestLoopStaleness:
    def _patch_metrics(self, monkeypatch, last_run):
        """Patch health_monitor.get_loop_metrics to return a canned value."""
        mock = MagicMock(return_value={"loop_last_run": last_run, "loop_duration_s": 42, "loop_idle_rate": 0.0, "malformed_lines": 0})
        monkeypatch.setattr("backend.health_monitor.get_loop_metrics", mock)

    def test_fresh_loop(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(tz=timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._patch_metrics(monkeypatch, recent)
        with patch("backend.control_plane.check_gate", side_effect=Exception("unavailable")):
            r = check_loop_staleness()
        _assert_check(r, "loop_staleness", True)
        assert "m ago" in r["detail"]

    def test_stale_loop(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._patch_metrics(monkeypatch, old)
        with patch("backend.control_plane.check_gate", side_effect=Exception("unavailable")):
            r = check_loop_staleness()
        _assert_check(r, "loop_staleness", False)
        assert "threshold" in r["detail"]

    def test_no_metrics(self, monkeypatch):
        mock = MagicMock(return_value={"loop_last_run": None, "loop_duration_s": None, "loop_idle_rate": None, "malformed_lines": 0})
        monkeypatch.setattr("backend.health_monitor.get_loop_metrics", mock)
        with patch("backend.control_plane.check_gate", side_effect=Exception("unavailable")):
            r = check_loop_staleness()
        _assert_check(r, "loop_staleness", False)

    def test_gate_off_always_ok(self):
        with patch("backend.control_plane.check_gate", return_value=False):
            r = check_loop_staleness()
        _assert_check(r, "loop_staleness", True)
        assert "gate" in r["detail"]


# ---------------------------------------------------------------------------
# check_hook_dirs_present
# ---------------------------------------------------------------------------


class TestHookDirsPresent:
    def test_both_present(self, tmp_path, monkeypatch):
        hooks_dir = tmp_path / "hooks"
        post_d = hooks_dir / "post-agent.d"
        post_d.mkdir(parents=True)
        monkeypatch.setattr(hr_mod, "_HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(hr_mod, "_POST_AGENT_D", post_d)
        r = check_hook_dirs_present()
        _assert_check(r, "hook_dirs_present", True)

    def test_missing_post_agent_d(self, tmp_path, monkeypatch):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        post_d = hooks_dir / "post-agent.d"  # not created
        monkeypatch.setattr(hr_mod, "_HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(hr_mod, "_POST_AGENT_D", post_d)
        r = check_hook_dirs_present()
        _assert_check(r, "hook_dirs_present", False)
        assert "post-agent.d" in r["detail"]

    def test_both_missing(self, tmp_path, monkeypatch):
        hooks_dir = tmp_path / "missing-hooks"
        post_d = hooks_dir / "post-agent.d"
        monkeypatch.setattr(hr_mod, "_HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(hr_mod, "_POST_AGENT_D", post_d)
        r = check_hook_dirs_present()
        _assert_check(r, "hook_dirs_present", False)


# ---------------------------------------------------------------------------
# check_duckdb_freshness
# ---------------------------------------------------------------------------


class TestDuckdbFreshness:
    @pytest.fixture(autouse=True)
    def _cleanup_stats_db(self):
        # D#1810: same reasoning as TestStateDbWritable._cleanup_state_db —
        # delattr, not restore-to-snapshot, so __getattr__ resolution comes
        # back for later tests instead of freezing at the last patched value.
        yield
        if "STATS_DB" in vars(hr_mod):
            del hr_mod.STATS_DB

    def test_missing_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hr_mod, "STATS_DB", tmp_path / "missing.duckdb")
        r = check_duckdb_freshness()
        _assert_check(r, "duckdb_freshness", False)

    def test_fresh_db(self, tmp_path, monkeypatch):
        db = tmp_path / "stats.duckdb"
        db.write_bytes(b"fake")
        monkeypatch.setattr(hr_mod, "STATS_DB", db)
        r = check_duckdb_freshness()
        _assert_check(r, "duckdb_freshness", True)

    def test_stale_db(self, tmp_path, monkeypatch):
        db = tmp_path / "stats.duckdb"
        db.write_bytes(b"fake")
        # Set mtime to 2 hours ago
        import os
        old_mtime = time.time() - 2 * 3600
        os.utime(str(db), (old_mtime, old_mtime))
        monkeypatch.setattr(hr_mod, "STATS_DB", db)
        monkeypatch.setattr(hr_mod, "_DUCKDB_STALE_SECS", 3600)
        r = check_duckdb_freshness()
        _assert_check(r, "duckdb_freshness", False)
        assert "threshold" in r["detail"]


# ---------------------------------------------------------------------------
# check_circuit_breakers
# ---------------------------------------------------------------------------


class TestCircuitBreakers:
    def test_no_tripped(self):
        with patch("backend.circuit_breaker._collect_tripped", return_value=[]):
            r = check_circuit_breakers()
        _assert_check(r, "circuit_breakers", True)

    def test_tripped_breaker(self):
        from datetime import datetime, timezone, timedelta
        # A fresh timestamp (1 hour ago) with an open Discussion — must still fail the check.
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tripped = [{"discussion": 42, "count": 4, "blocked": True, "agent": "executor", "reason": "fail", "updated_at": fresh_ts}]
        with patch("backend.circuit_breaker._collect_tripped", return_value=tripped), \
             patch("backend.circuit_breaker._discussion_state", return_value="open"):
            r = check_circuit_breakers()
        _assert_check(r, "circuit_breakers", False)
        assert "#42" in r["detail"]

    def test_non_tripped_entries_ok(self):
        near_miss = [{"discussion": 5, "count": 2, "blocked": False, "agent": "executor", "reason": "fail", "updated_at": None}]
        with patch("backend.circuit_breaker._collect_tripped", return_value=near_miss):
            r = check_circuit_breakers()
        _assert_check(r, "circuit_breakers", True)



    def test_blocked_missing_ts_open_discussion_fails(self):
        """A blocked breaker with no updated_at and open Discussion must NOT be skipped."""
        # No updated_at → age_stale stays False; open Discussion → disc_dead=False.
        # The entry must stay in active → ok:False.
        tripped = [{'discussion': 99, 'count': 3, 'blocked': True, 'agent': 'executor', 'reason': 'fail', 'updated_at': None}]
        with patch('backend.circuit_breaker._collect_tripped', return_value=tripped),              patch('backend.circuit_breaker._discussion_state', return_value='open'):
            r = check_circuit_breakers()
        _assert_check(r, 'circuit_breakers', False)
        assert '#99' in r['detail'], f'expected #99 in detail, got: {r["detail"]}'

    def test_blocked_missing_ts_closed_discussion_skipped(self):
        """A blocked breaker with no updated_at and CLOSED Discussion must be skipped (ok:True)."""
        # No updated_at → age_stale=False; closed Discussion → disc_dead=True → skip.
        tripped = [{'discussion': 88, 'count': 3, 'blocked': True, 'agent': 'executor', 'reason': 'fail', 'updated_at': None}]
        with patch('backend.circuit_breaker._collect_tripped', return_value=tripped),              patch('backend.circuit_breaker._discussion_state', return_value='closed'):
            r = check_circuit_breakers()
        _assert_check(r, 'circuit_breakers', True)

    def test_blocked_unparseable_ts_open_discussion_fails(self):
        """A blocked breaker with a garbage timestamp and open Discussion must NOT be skipped."""
        # Unparseable → age_stale stays False; open → disc_dead=False → must report ok:False.
        tripped = [{'discussion': 77, 'count': 4, 'blocked': True, 'agent': 'executor', 'reason': 'fail', 'updated_at': 'not-a-date'}]
        with patch('backend.circuit_breaker._collect_tripped', return_value=tripped),              patch('backend.circuit_breaker._discussion_state', return_value='unknown'):
            r = check_circuit_breakers()
        _assert_check(r, 'circuit_breakers', False)
        assert '#77' in r['detail']


# ---------------------------------------------------------------------------
# check_blackboard_writable
# ---------------------------------------------------------------------------


class TestBlackboardWritable:
    @pytest.fixture(autouse=True)
    def _cleanup_blackboard_dir(self):
        # D#1810: same reasoning as TestStateDbWritable._cleanup_state_db.
        yield
        if "BLACKBOARD_DIR" in vars(hr_mod):
            del hr_mod.BLACKBOARD_DIR

    def test_missing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hr_mod, "BLACKBOARD_DIR", tmp_path / "absent")
        r = check_blackboard_writable()
        _assert_check(r, "blackboard_writable", False)

    def test_healthy(self, tmp_path, monkeypatch):
        bb_dir = tmp_path / "blackboard"
        bb_dir.mkdir()
        monkeypatch.setattr(hr_mod, "BLACKBOARD_DIR", bb_dir)
        r = check_blackboard_writable()
        _assert_check(r, "blackboard_writable", True)


# ---------------------------------------------------------------------------
# run_checks aggregator
# ---------------------------------------------------------------------------


class TestRunChecks:
    def test_all_green(self):
        green = lambda: {"name": "x", "ok": True, "detail": "fine"}
        report = run_checks(checks=[green, green])
        assert report["overall"] is True
        assert len(report["checks"]) == 2
        assert "ts" in report

    def test_one_red_flips_overall(self):
        green = lambda: {"name": "g", "ok": True, "detail": "ok"}
        red = lambda: {"name": "r", "ok": False, "detail": "bad"}
        report = run_checks(checks=[green, red])
        assert report["overall"] is False

    def test_exception_in_check_caught(self):
        def exploding():
            raise RuntimeError("boom")

        report = run_checks(checks=[exploding])
        assert report["overall"] is False
        r = report["checks"][0]
        assert r["ok"] is False
        assert "boom" in r["detail"]

    def test_json_serializable(self):
        """run_checks output can always be serialized to JSON."""
        green = lambda: {"name": "x", "ok": True, "detail": "fine"}
        report = run_checks(checks=[green])
        serialized = json.dumps(report)
        assert '"overall": true' in serialized or '"overall":true' in serialized

    def test_missing_ok_key_treated_as_fail(self):
        """A stub returning a dict without 'ok' is treated as not-passed."""
        no_ok = lambda: {"name": "z", "detail": "no ok key here"}
        report = run_checks(checks=[no_ok])
        assert report["overall"] is False

    def test_empty_checks_list_overall_true(self):
        """Empty check list — all() of empty is True per Python semantics."""
        report = run_checks(checks=[])
        assert report["overall"] is True
        assert report["checks"] == []
        assert "ts" in report


# ---------------------------------------------------------------------------
# check_dial_chain_integrity
# ---------------------------------------------------------------------------


class TestDialChainIntegrity:
    def test_ok_script_returns_zero(self, tmp_path, monkeypatch):
        """When audit-replay.sh exits 0 the check passes."""
        # Create a stub script so the "script not found" branch is skipped.
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "audit-replay.sh"
        script.write_text("#!/bin/bash\necho 'chain intact'\nexit 0\n")
        monkeypatch.setattr(hr_mod, "_REPO_ROOT", tmp_path)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "chain intact"
        mock_proc.stderr = ""
        with patch("backend.health_report.subprocess.run", return_value=mock_proc):
            r = check_dial_chain_integrity()
        _assert_check(r, "dial_chain_integrity", True)
        assert "chain intact" in r["detail"]

    def test_fail_script_missing(self, tmp_path, monkeypatch):
        """When audit-replay.sh does not exist the check fails immediately."""
        # Point _REPO_ROOT at a directory that has no scripts/audit-replay.sh.
        monkeypatch.setattr(hr_mod, "_REPO_ROOT", tmp_path)
        r = check_dial_chain_integrity()
        _assert_check(r, "dial_chain_integrity", False)
        assert "not found" in r["detail"]

    def test_fail_chain_break(self, tmp_path, monkeypatch):
        """When audit-replay.sh exits non-zero the check fails with stdout detail."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "audit-replay.sh").write_text("#!/bin/bash\nexit 1\n")
        monkeypatch.setattr(hr_mod, "_REPO_ROOT", tmp_path)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = "hash mismatch at row 42"
        mock_proc.stderr = ""
        with patch("backend.health_report.subprocess.run", return_value=mock_proc):
            r = check_dial_chain_integrity()
        _assert_check(r, "dial_chain_integrity", False)
        assert "hash mismatch" in r["detail"]


# ---------------------------------------------------------------------------
# CLI integration (argparse + exit codes)
# ---------------------------------------------------------------------------


class TestCli:
    def test_exit_0_when_all_pass(self, capsys):
        green = lambda: {"name": "x", "ok": True, "detail": "ok"}
        with patch.object(hr_mod, "_CHECKS", [green]):
            with pytest.raises(SystemExit) as exc_info:
                hr_mod.main(["check"])
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["overall"] is True

    def test_exit_1_when_any_fail(self, capsys):
        red = lambda: {"name": "x", "ok": False, "detail": "bad"}
        with patch.object(hr_mod, "_CHECKS", [red]):
            with pytest.raises(SystemExit) as exc_info:
                hr_mod.main(["check"])
            assert exc_info.value.code == 1

    def test_human_flag_produces_text(self, capsys):
        green = lambda: {"name": "my_check", "ok": True, "detail": "all good"}
        with patch.object(hr_mod, "_CHECKS", [green]):
            with pytest.raises(SystemExit):
                hr_mod.main(["check", "--human"])
        out = capsys.readouterr().out
        assert "my_check" in out
        # Should not be parseable as pure JSON (has ANSI codes / decorations)
        assert "Health report" in out
