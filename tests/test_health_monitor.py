"""
Tests for backend/health_monitor.py — check_loop_health() function.

After Discussion #459: check_loop_health() reads loop-runs/*/*.log mtime
(not loop-metrics.jsonl).  These tests create fake loop-run log files and
patch _LOOP_RUNS_DIR so the function reads from tmp_path instead.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import health_monitor
from backend.health_monitor import check_loop_health, get_loop_metrics, get_loop_health_dashboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_loop_run_log(base_dir: Path, age_seconds: float) -> Path:
    """Create a fake loop-run log under base_dir/loop-runs/project/*.log."""
    log_dir = base_dir / "loop-runs" / "project-x"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "loop.log"
    log_file.write_text("SUMMARY: pass\n")
    target_mtime = time.time() - age_seconds
    os.utime(str(log_file), (target_mtime, target_mtime))
    return log_file


def _write_metrics(path: Path, ts: datetime) -> None:
    """Write a single metrics entry with the given timestamp (used by get_loop_metrics tests)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": 10,
        "actions": [],
    }
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def _write_metrics_multi(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# check_loop_health — mtime-based tests
# ---------------------------------------------------------------------------

class TestCheckLoopHealth:
    def test_healthy_recent_run(self, tmp_path: Path) -> None:
        _make_loop_run_log(tmp_path, age_seconds=5 * 60)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", tmp_path / "loop-runs")
            result = check_loop_health(threshold_minutes=30)

        assert result["healthy"] is True
        assert result["age_minutes"] is not None
        assert result["age_minutes"] < 30
        assert result["threshold_minutes"] == 30
        assert result["last_run"] is not None

    def test_stale_run_returns_unhealthy(self, tmp_path: Path) -> None:
        _make_loop_run_log(tmp_path, age_seconds=90 * 60)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", tmp_path / "loop-runs")
            result = check_loop_health(threshold_minutes=30)

        assert result["healthy"] is False
        assert result["age_minutes"] is not None
        assert result["age_minutes"] > 30
        assert result["threshold_minutes"] == 30

    def test_missing_file_returns_unhealthy(self, tmp_path: Path) -> None:
        # No loop-runs dir at all → no files found
        empty_dir = tmp_path / "loop-runs"
        empty_dir.mkdir(parents=True, exist_ok=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", empty_dir)
            result = check_loop_health(threshold_minutes=30)

        assert result["healthy"] is False
        assert result["last_run"] is None
        assert result["age_minutes"] is None
        assert "reason" in result
        assert result["threshold_minutes"] == 30

    def test_empty_file_returns_unhealthy(self, tmp_path: Path) -> None:
        # loop-runs dir exists but has no .log files
        empty_dir = tmp_path / "loop-runs"
        empty_dir.mkdir(parents=True, exist_ok=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", empty_dir)
            result = check_loop_health(threshold_minutes=30)

        assert result["healthy"] is False
        assert "reason" in result

    def test_custom_threshold_used(self, tmp_path: Path) -> None:
        # 10 minutes old
        _make_loop_run_log(tmp_path, age_seconds=10 * 60)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", tmp_path / "loop-runs")

            # With threshold=5 → stale
            result_stale = check_loop_health(threshold_minutes=5)
            assert result_stale["healthy"] is False
            assert result_stale["threshold_minutes"] == 5

            # With threshold=30 → healthy
            result_ok = check_loop_health(threshold_minutes=30)
            assert result_ok["healthy"] is True
            assert result_ok["threshold_minutes"] == 30

    def test_multiple_lines_uses_last(self, tmp_path: Path) -> None:
        """When multiple log files exist, the freshest mtime wins."""
        _make_loop_run_log(tmp_path, age_seconds=90 * 60)  # stale

        # Add a fresh log in another sub-project
        fresh_dir = tmp_path / "loop-runs" / "project-y"
        fresh_dir.mkdir(parents=True, exist_ok=True)
        fresh_log = fresh_dir / "loop.log"
        fresh_log.write_text("SUMMARY: pass\n")
        fresh_mtime = time.time() - 5 * 60
        os.utime(str(fresh_log), (fresh_mtime, fresh_mtime))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", tmp_path / "loop-runs")
            result = check_loop_health(threshold_minutes=30)

        assert result["healthy"] is True

    def test_env_var_threshold(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """AF_LOOP_STALE_MINUTES env var sets the default threshold."""
        _make_loop_run_log(tmp_path, age_seconds=10 * 60)
        monkeypatch.setattr(health_monitor, "_LOOP_RUNS_DIR", tmp_path / "loop-runs")

        monkeypatch.setenv("AF_LOOP_STALE_MINUTES", "5")
        result = check_loop_health()
        assert result["healthy"] is False
        assert result["threshold_minutes"] == 5

        monkeypatch.setenv("AF_LOOP_STALE_MINUTES", "60")
        result = check_loop_health()
        assert result["healthy"] is True
        assert result["threshold_minutes"] == 60


# ---------------------------------------------------------------------------
# get_loop_metrics — unchanged (still reads loop-metrics.jsonl)
# ---------------------------------------------------------------------------

class TestGetLoopMetrics:
    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        metrics = tmp_path / "missing.jsonl"
        result = get_loop_metrics(metrics_path=metrics)
        assert result["loop_last_run"] is None
        assert result["loop_duration_s"] is None
        assert result["loop_idle_rate"] is None

    def test_returns_last_run_and_duration(self, tmp_path: Path) -> None:
        metrics = tmp_path / "loop-metrics.jsonl"
        ts = datetime.now(tz=timezone.utc)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_metrics_multi(metrics, [
            {"timestamp": ts_str, "duration_seconds": 42, "idle": False},
        ])
        result = get_loop_metrics(metrics_path=metrics)
        assert result["loop_last_run"] == ts_str
        assert result["loop_duration_s"] == 42
        assert result["loop_idle_rate"] == 0.0

    def test_idle_rate_computed_from_last_n_entries(self, tmp_path: Path) -> None:
        metrics = tmp_path / "loop-metrics.jsonl"
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [
            {"timestamp": ts, "duration_seconds": 30, "idle": True},
            {"timestamp": ts, "duration_seconds": 30, "idle": False},
            {"timestamp": ts, "duration_seconds": 30, "idle": True},
            {"timestamp": ts, "duration_seconds": 30, "idle": False},
        ]
        _write_metrics_multi(metrics, entries)
        result = get_loop_metrics(n_entries=4, metrics_path=metrics)
        assert result["loop_idle_rate"] == 0.5

    def test_idle_rate_all_idle(self, tmp_path: Path) -> None:
        metrics = tmp_path / "loop-metrics.jsonl"
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [{"timestamp": ts, "duration_seconds": 10, "idle": True}] * 3
        _write_metrics_multi(metrics, entries)
        result = get_loop_metrics(n_entries=10, metrics_path=metrics)
        assert result["loop_idle_rate"] == 1.0

    def test_empty_file_returns_nones(self, tmp_path: Path) -> None:
        metrics = tmp_path / "loop-metrics.jsonl"
        metrics.write_text("", encoding="utf-8")
        result = get_loop_metrics(metrics_path=metrics)
        assert result["loop_last_run"] is None
        assert result["loop_duration_s"] is None
        assert result["loop_idle_rate"] is None


# ---------------------------------------------------------------------------
# get_loop_health_dashboard — uses mtime signal for status
# ---------------------------------------------------------------------------

class TestGetLoopHealthDashboard:
    def test_returns_expected_keys(self, tmp_path: Path) -> None:
        """Recent log file → status 'ok', lastRun from mtime, duration from metrics."""
        _make_loop_run_log(tmp_path, age_seconds=2 * 60)

        # Also write a metrics file so duration is populated
        metrics = tmp_path / "loop-metrics.jsonl"
        ts_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_metrics_multi(metrics, [
            {"timestamp": ts_str, "duration_seconds": 55, "idle": False},
        ])

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", tmp_path / "loop-runs")
            result = get_loop_health_dashboard(metrics_path=metrics)

        assert "lastRun" in result
        assert "duration" in result
        assert "status" in result
        assert result["status"] == "ok"
        assert result["duration"] == 55

    def test_status_error_when_no_file(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "loop-runs"
        empty_dir.mkdir(parents=True, exist_ok=True)
        metrics = tmp_path / "missing.jsonl"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", empty_dir)
            result = get_loop_health_dashboard(metrics_path=metrics)

        assert result["status"] == "error"
        assert result["lastRun"] == ""
        assert result["duration"] == 0

    def test_status_error_when_stale(self, tmp_path: Path) -> None:
        # 90 minutes old → error (> 60 min threshold)
        _make_loop_run_log(tmp_path, age_seconds=90 * 60)
        metrics = tmp_path / "loop-metrics.jsonl"
        old = datetime.now(tz=timezone.utc) - timedelta(hours=2)
        ts_str = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_metrics_multi(metrics, [
            {"timestamp": ts_str, "duration_seconds": 30, "idle": False},
        ])

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", tmp_path / "loop-runs")
            result = get_loop_health_dashboard(metrics_path=metrics)

        assert result["status"] == "error"

    def test_status_idle_when_all_idle(self, tmp_path: Path) -> None:
        # Recent log → healthy mtime; idle_rate=1.0 from metrics → status='idle'
        _make_loop_run_log(tmp_path, age_seconds=2 * 60)
        metrics = tmp_path / "loop-metrics.jsonl"
        ts_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [{"timestamp": ts_str, "duration_seconds": 10, "idle": True}] * 10
        _write_metrics_multi(metrics, entries)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(health_monitor, "_LOOP_RUNS_DIR", tmp_path / "loop-runs")
            result = get_loop_health_dashboard(metrics_path=metrics)

        assert result["status"] == "idle"


# ---------------------------------------------------------------------------
# get_loop_metrics — malformed-line tolerance (AC-9)
# ---------------------------------------------------------------------------

class TestGetLoopMetricsMalformedLines:
    """AC-9: malformed lines are skipped and counted; valid lines still work."""

    def test_single_malformed_line_does_not_poison_result(self, tmp_path: Path) -> None:
        metrics = tmp_path / "loop-metrics.jsonl"
        ts = "2026-05-12T08:00:00Z"
        metrics.write_text(
            "not valid json\n"
            + json.dumps({"timestamp": ts, "duration_s": 120}) + "\n",
            encoding="utf-8",
        )
        result = get_loop_metrics(metrics_path=metrics)
        assert result["loop_last_run"] == ts
        assert result["loop_duration_s"] == 120
        assert result["malformed_lines"] == 1

    def test_malformed_line_at_end_uses_last_valid(self, tmp_path: Path) -> None:
        metrics = tmp_path / "loop-metrics.jsonl"
        ts = "2026-05-12T09:00:00Z"
        metrics.write_text(
            json.dumps({"timestamp": ts, "duration_s": 60}) + "\n"
            + "{broken}\n",
            encoding="utf-8",
        )
        result = get_loop_metrics(metrics_path=metrics)
        # Last valid entry is the first line
        assert result["loop_last_run"] == ts
        assert result["malformed_lines"] == 1

    def test_all_malformed_returns_nones_with_count(self, tmp_path: Path) -> None:
        metrics = tmp_path / "loop-metrics.jsonl"
        metrics.write_text("bad line 1\nbad line 2\n", encoding="utf-8")
        result = get_loop_metrics(metrics_path=metrics)
        assert result["loop_last_run"] is None
        assert result["loop_duration_s"] is None
        assert result["malformed_lines"] == 2

    def test_no_malformed_lines_returns_zero(self, tmp_path: Path) -> None:
        metrics = tmp_path / "loop-metrics.jsonl"
        ts = "2026-05-12T10:00:00Z"
        metrics.write_text(
            json.dumps({"timestamp": ts, "duration_s": 30}) + "\n",
            encoding="utf-8",
        )
        result = get_loop_metrics(metrics_path=metrics)
        assert result["malformed_lines"] == 0

    def test_malformed_lines_count_in_full_pipeline(self, tmp_path: Path) -> None:
        """Valid + malformed mix: idle_rate computed from valid entries only."""
        metrics = tmp_path / "loop-metrics.jsonl"
        ts = "2026-05-12T11:00:00Z"
        lines = [
            json.dumps({"timestamp": ts, "duration_s": 30, "idle": True}),
            "{not json}",
            json.dumps({"timestamp": ts, "duration_s": 30, "idle": False}),
            "also broken",
            json.dumps({"timestamp": ts, "duration_s": 30, "idle": True}),
        ]
        metrics.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = get_loop_metrics(n_entries=10, metrics_path=metrics)
        # 2 malformed, 3 valid
        assert result["malformed_lines"] == 2
        # idle_rate = 2 idle out of 3 valid = 0.6667
        assert result["loop_idle_rate"] is not None
        assert abs(result["loop_idle_rate"] - round(2 / 3, 4)) < 0.001
