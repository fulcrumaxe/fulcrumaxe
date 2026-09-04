"""Tests for the loop-health mtime signal in health_monitor.check_loop_health().

Uses tmp_path to create fake loop-runs/<project>/*.log files at varying mtimes,
verifying the 30/60-minute status thresholds.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import health_monitor


def _make_loop_run_log(tmp_path: Path, age_seconds: float) -> Path:
    """Create a fake loop-run log at tmp_path/loop-runs/project/loop.log.

    Sets the file mtime to ``now - age_seconds``.
    """
    log_dir = tmp_path / "loop-runs" / "project-x"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "loop.log"
    log_file.write_text("SUMMARY: pass\n")
    # Set mtime to now - age_seconds
    target_mtime = time.time() - age_seconds
    import os
    os.utime(str(log_file), (target_mtime, target_mtime))
    return log_file


class TestCheckLoopHealthMtime:
    """check_loop_health() reads loop-runs/*/*.log mtime."""

    def test_healthy_when_recent(self, tmp_path):
        """File modified 5 minutes ago → healthy=True, status='healthy'."""
        _make_loop_run_log(tmp_path, age_seconds=5 * 60)

        loop_runs_dir = tmp_path / "loop-runs"
        with patch.object(health_monitor, "_LOOP_RUNS_DIR", loop_runs_dir):
            result = health_monitor.check_loop_health(threshold_minutes=30)

        assert result["healthy"] is True
        assert result["status"] == "healthy"
        assert result["lastRunAt"] is not None
        assert result["age_minutes"] < 30

    def test_warning_at_45_minutes(self, tmp_path):
        """File modified 45 minutes ago → healthy=False, status='warning'."""
        _make_loop_run_log(tmp_path, age_seconds=45 * 60)

        loop_runs_dir = tmp_path / "loop-runs"
        with patch.object(health_monitor, "_LOOP_RUNS_DIR", loop_runs_dir):
            result = health_monitor.check_loop_health(threshold_minutes=30)

        assert result["healthy"] is False
        assert result["status"] == "warning"

    def test_error_when_stale_over_60_minutes(self, tmp_path):
        """File modified 90 minutes ago → healthy=False, status='error'."""
        _make_loop_run_log(tmp_path, age_seconds=90 * 60)

        loop_runs_dir = tmp_path / "loop-runs"
        with patch.object(health_monitor, "_LOOP_RUNS_DIR", loop_runs_dir):
            result = health_monitor.check_loop_health(threshold_minutes=30)

        assert result["healthy"] is False
        assert result["status"] == "error"

    def test_error_when_no_files(self, tmp_path):
        """Empty loop-runs dir → healthy=False, status='error', lastRunAt=None."""
        loop_runs_dir = tmp_path / "loop-runs"
        loop_runs_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(health_monitor, "_LOOP_RUNS_DIR", loop_runs_dir):
            result = health_monitor.check_loop_health(threshold_minutes=30)

        assert result["healthy"] is False
        assert result["status"] == "error"
        assert result["lastRunAt"] is None
        assert "no loop-runs logs found" in result.get("reason", "")

    def test_uses_max_mtime_across_multiple_logs(self, tmp_path):
        """When multiple log files exist, the most recent mtime is used."""
        _make_loop_run_log(tmp_path, age_seconds=90 * 60)  # stale
        # Create a second project with a fresh log
        fresh_dir = tmp_path / "loop-runs" / "project-fresh"
        fresh_dir.mkdir(parents=True, exist_ok=True)
        fresh_log = fresh_dir / "loop.log"
        fresh_log.write_text("SUMMARY: pass\n")
        import os
        fresh_mtime = time.time() - 5 * 60  # 5 minutes ago
        os.utime(str(fresh_log), (fresh_mtime, fresh_mtime))

        loop_runs_dir = tmp_path / "loop-runs"
        with patch.object(health_monitor, "_LOOP_RUNS_DIR", loop_runs_dir):
            result = health_monitor.check_loop_health(threshold_minutes=30)

        # Should be healthy because the freshest file is only 5 min old
        assert result["healthy"] is True
        assert result["status"] == "healthy"

    def test_lastRunAt_is_iso_string(self, tmp_path):
        """lastRunAt must be a valid ISO 8601 string when files exist."""
        from datetime import datetime
        _make_loop_run_log(tmp_path, age_seconds=2 * 60)

        loop_runs_dir = tmp_path / "loop-runs"
        with patch.object(health_monitor, "_LOOP_RUNS_DIR", loop_runs_dir):
            result = health_monitor.check_loop_health(threshold_minutes=30)

        assert result["lastRunAt"] is not None
        # Should be parseable as ISO datetime
        dt = datetime.fromisoformat(result["lastRunAt"].replace("Z", "+00:00"))
        assert dt is not None


class TestGetLoopHealthDashboard:
    """get_loop_health_dashboard() uses the mtime signal for status."""

    def test_status_ok_when_recent(self, tmp_path):
        _make_loop_run_log(tmp_path, age_seconds=5 * 60)

        loop_runs_dir = tmp_path / "loop-runs"
        with patch.object(health_monitor, "_LOOP_RUNS_DIR", loop_runs_dir):
            result = health_monitor.get_loop_health_dashboard()

        assert result["status"] == "ok"
        assert result["lastRun"] != ""

    def test_status_error_when_no_files(self, tmp_path):
        loop_runs_dir = tmp_path / "loop-runs"
        loop_runs_dir.mkdir(parents=True, exist_ok=True)

        # Also pass a nonexistent metrics_path so get_loop_metrics() returns nulls —
        # otherwise the real .autonomous-team/loop-metrics.jsonl data would be read,
        # causing get_loop_health_dashboard() to pick up a fresh metrics timestamp
        # and return "ok" instead of "error".
        with patch.object(health_monitor, "_LOOP_RUNS_DIR", loop_runs_dir):
            result = health_monitor.get_loop_health_dashboard(
                metrics_path=tmp_path / "nonexistent-metrics.jsonl"
            )

        assert result["status"] == "error"
