"""
Tests for backend/health_monitor.py

Covers:
  - get_loop_metrics: reading/parsing loop-metrics.jsonl (missing file, valid entries,
    malformed lines, dual field-name support, idle rate computation)
  - check_loop_health: status thresholds (healthy/warning/error), env-var override,
    no-log-files path
  - get_loop_health_dashboard: status mapping, idle detection, timestamp tie-breaking
  - create_alert_issue: duplicate suppression, body construction
  - CLI: check and alert subcommands, exit codes

Isolation: all file reads use tmp_path; no state is read from or written to
~/.autonomous-forever-state/ or the real .autonomous-team/ directory.
_get_latest_loop_run_mtime is monkeypatched for check_loop_health tests to avoid
touching the repo's .autonomous-team/loop-runs/ directory.

Run with:
    python3 -m pytest backend/tests/test_health_monitor.py -v
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.health_monitor as hm_mod
from backend.health_monitor import (
    _ALERT_TITLE_PREFIX,
    _DEFAULT_THRESHOLD_MINUTES,
    _STALE_WARNING_MINUTES,
    check_loop_health,
    create_alert_issue,
    get_loop_health_dashboard,
    get_loop_metrics,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_minus(minutes: float) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)


def _write_metrics(path: Path, entries: list[dict]) -> None:
    """Write a loop-metrics.jsonl file with the given entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# get_loop_metrics — file missing / empty
# ---------------------------------------------------------------------------


class TestGetLoopMetricsMissingOrEmpty:
    def test_missing_file_returns_nulls(self, tmp_path):
        result = get_loop_metrics(metrics_path=tmp_path / "nonexistent.jsonl")
        assert result["loop_last_run"] is None
        assert result["loop_duration_s"] is None
        assert result["loop_idle_rate"] is None
        assert result["malformed_lines"] == 0

    def test_empty_file_returns_nulls(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        p.write_text("", encoding="utf-8")
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_last_run"] is None
        assert result["loop_duration_s"] is None
        assert result["loop_idle_rate"] is None
        assert result["malformed_lines"] == 0

    def test_whitespace_only_file_returns_nulls(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        p.write_text("   \n\n  \n", encoding="utf-8")
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_last_run"] is None
        assert result["malformed_lines"] == 0


# ---------------------------------------------------------------------------
# get_loop_metrics — valid entries, field-name variants
# ---------------------------------------------------------------------------


class TestGetLoopMetricsValidEntries:
    def test_reads_last_entry_for_last_run(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        ts1 = "2026-05-01T10:00:00Z"
        ts2 = "2026-05-01T11:00:00Z"
        _write_metrics(p, [
            {"timestamp": ts1, "duration_seconds": 120, "idle": False},
            {"timestamp": ts2, "duration_seconds": 90, "idle": False},
        ])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_last_run"] == ts2
        assert result["loop_duration_s"] == 90

    def test_supports_ts_field_name(self, tmp_path):
        """Interactive /loop runs use 'ts' not 'timestamp'."""
        p = tmp_path / "metrics.jsonl"
        ts = "2026-05-10T08:00:00Z"
        _write_metrics(p, [{"ts": ts, "duration_s": 55, "idle": False}])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_last_run"] == ts
        assert result["loop_duration_s"] == 55

    def test_supports_duration_s_field_name(self, tmp_path):
        """'duration_s' is the interactive /loop format."""
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [{"timestamp": "2026-05-10T09:00:00Z", "duration_s": 77}])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_duration_s"] == 77

    def test_missing_duration_returns_none(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [{"timestamp": "2026-05-10T09:00:00Z"}])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_duration_s"] is None

    def test_missing_timestamp_returns_none_for_last_run(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [{"duration_seconds": 30, "idle": False}])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_last_run"] is None


# ---------------------------------------------------------------------------
# get_loop_metrics — idle rate computation
# ---------------------------------------------------------------------------


class TestGetLoopMetricsIdleRate:
    def test_no_idle_entries_gives_zero(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [
            {"timestamp": "2026-05-10T01:00:00Z", "idle": False},
            {"timestamp": "2026-05-10T02:00:00Z", "idle": False},
        ])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_idle_rate"] == 0.0

    def test_all_idle_gives_one(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [
            {"timestamp": "2026-05-10T01:00:00Z", "idle": True},
            {"timestamp": "2026-05-10T02:00:00Z", "idle": True},
        ])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_idle_rate"] == 1.0

    def test_half_idle_gives_half(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [
            {"timestamp": "2026-05-10T01:00:00Z", "idle": True},
            {"timestamp": "2026-05-10T02:00:00Z", "idle": False},
        ])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_idle_rate"] == 0.5

    def test_idle_rate_uses_last_n_entries(self, tmp_path):
        """idle_rate is computed over last n_entries (default 10)."""
        p = tmp_path / "metrics.jsonl"
        # 20 entries: first 10 idle, last 10 non-idle
        entries = (
            [{"timestamp": f"2026-05-10T0{i}:00:00Z", "idle": True} for i in range(10)] +
            [{"timestamp": f"2026-05-10T1{i}:00:00Z", "idle": False} for i in range(10)]
        )
        _write_metrics(p, entries)
        result = get_loop_metrics(metrics_path=p)
        # Last 10 are all non-idle
        assert result["loop_idle_rate"] == 0.0

    def test_idle_field_absent_counts_as_false(self, tmp_path):
        """Entry without 'idle' key is treated as non-idle."""
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [{"timestamp": "2026-05-10T01:00:00Z"}])
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_idle_rate"] == 0.0


# ---------------------------------------------------------------------------
# get_loop_metrics — malformed lines
# ---------------------------------------------------------------------------


class TestGetLoopMetricsMalformed:
    def test_malformed_lines_counted(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        with p.open("w") as fh:
            fh.write("not-json\n")
            fh.write(json.dumps({"timestamp": "2026-05-10T01:00:00Z"}) + "\n")
            fh.write("also-bad\n")
        result = get_loop_metrics(metrics_path=p)
        assert result["malformed_lines"] == 2

    def test_all_malformed_returns_null_fields(self, tmp_path):
        p = tmp_path / "metrics.jsonl"
        p.write_text("bad1\nbad2\nbad3\n", encoding="utf-8")
        result = get_loop_metrics(metrics_path=p)
        assert result["loop_last_run"] is None
        assert result["loop_duration_s"] is None
        assert result["loop_idle_rate"] is None
        assert result["malformed_lines"] == 3

    def test_non_dict_json_is_malformed(self, tmp_path):
        """A JSON array is valid JSON but not a dict — counts as malformed."""
        p = tmp_path / "metrics.jsonl"
        with p.open("w") as fh:
            fh.write("[1, 2, 3]\n")
            fh.write(json.dumps({"timestamp": "2026-05-10T09:00:00Z"}) + "\n")
        result = get_loop_metrics(metrics_path=p)
        assert result["malformed_lines"] == 1
        assert result["loop_last_run"] == "2026-05-10T09:00:00Z"

    def test_malformed_does_not_raise(self, tmp_path):
        """Malformed lines are skipped without raising."""
        p = tmp_path / "metrics.jsonl"
        p.write_text("{{broken\n", encoding="utf-8")
        result = get_loop_metrics(metrics_path=p)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# check_loop_health — no log files
# ---------------------------------------------------------------------------


class TestCheckLoopHealthNoFiles:
    def test_no_files_returns_error_status(self, monkeypatch):
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: None)
        result = check_loop_health()
        assert result["healthy"] is False
        assert result["status"] == "error"
        assert result["lastRunAt"] is None
        assert result["last_run"] is None
        assert result["age_minutes"] is None
        assert "no loop-runs logs found" in result["reason"]

    def test_no_files_includes_threshold(self, monkeypatch):
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: None)
        result = check_loop_health(threshold_minutes=45)
        assert result["threshold_minutes"] == 45


# ---------------------------------------------------------------------------
# check_loop_health — status thresholds
# ---------------------------------------------------------------------------


class TestCheckLoopHealthThresholds:
    def _mock_mtime(self, monkeypatch, minutes_ago: float) -> None:
        mtime = time.time() - minutes_ago * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)

    def test_recent_run_is_healthy(self, monkeypatch):
        self._mock_mtime(monkeypatch, minutes_ago=5)
        result = check_loop_health(threshold_minutes=30)
        assert result["healthy"] is True
        assert result["status"] == "healthy"

    def test_run_at_threshold_is_healthy(self, monkeypatch):
        """Age exactly equal to threshold → healthy (≤ threshold is healthy)."""
        self._mock_mtime(monkeypatch, minutes_ago=30)
        result = check_loop_health(threshold_minutes=30)
        assert result["healthy"] is True
        assert result["status"] == "healthy"

    def test_run_just_over_threshold_is_warning(self, monkeypatch):
        """Age > threshold but ≤ 60 min → warning."""
        self._mock_mtime(monkeypatch, minutes_ago=45)
        result = check_loop_health(threshold_minutes=30)
        assert result["healthy"] is False
        assert result["status"] == "warning"

    def test_run_over_60_minutes_is_error(self, monkeypatch):
        """Age > 60 min → error regardless of threshold."""
        self._mock_mtime(monkeypatch, minutes_ago=90)
        result = check_loop_health(threshold_minutes=30)
        assert result["healthy"] is False
        assert result["status"] == "error"

    def test_age_minutes_is_present_and_approximate(self, monkeypatch):
        self._mock_mtime(monkeypatch, minutes_ago=10)
        result = check_loop_health(threshold_minutes=30)
        assert result["age_minutes"] is not None
        assert abs(result["age_minutes"] - 10) < 1  # within 1 min

    def test_last_run_iso_format(self, monkeypatch):
        self._mock_mtime(monkeypatch, minutes_ago=5)
        result = check_loop_health(threshold_minutes=30)
        last_run = result["lastRunAt"]
        assert last_run is not None
        # Should be parseable as ISO datetime
        dt = datetime.strptime(last_run, "%Y-%m-%dT%H:%M:%SZ")
        assert dt.year >= 2026

    def test_last_run_and_lastRunAt_are_same(self, monkeypatch):
        self._mock_mtime(monkeypatch, minutes_ago=5)
        result = check_loop_health(threshold_minutes=30)
        assert result["last_run"] == result["lastRunAt"]

    def test_threshold_minutes_present_in_result(self, monkeypatch):
        self._mock_mtime(monkeypatch, minutes_ago=5)
        result = check_loop_health(threshold_minutes=42)
        assert result["threshold_minutes"] == 42


# ---------------------------------------------------------------------------
# check_loop_health — env-var override
# ---------------------------------------------------------------------------


class TestCheckLoopHealthEnvVar:
    def test_env_var_overrides_default(self, monkeypatch):
        mtime = time.time() - 35 * 60  # 35 minutes ago
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        monkeypatch.setenv("AF_LOOP_STALE_MINUTES", "60")
        result = check_loop_health()
        # 35 min < 60 min threshold → healthy
        assert result["healthy"] is True
        assert result["threshold_minutes"] == 60

    def test_explicit_threshold_overrides_env_var(self, monkeypatch):
        mtime = time.time() - 35 * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        monkeypatch.setenv("AF_LOOP_STALE_MINUTES", "10")
        result = check_loop_health(threshold_minutes=60)
        # explicit 60 wins; 35 min < 60 → healthy
        assert result["healthy"] is True
        assert result["threshold_minutes"] == 60

    def test_default_threshold_is_30(self, monkeypatch):
        mtime = time.time() - 5 * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        monkeypatch.delenv("AF_LOOP_STALE_MINUTES", raising=False)
        result = check_loop_health()
        assert result["threshold_minutes"] == _DEFAULT_THRESHOLD_MINUTES


# ---------------------------------------------------------------------------
# get_loop_health_dashboard — status mapping
# ---------------------------------------------------------------------------


class TestGetLoopHealthDashboard:
    def _patch_sources(self, monkeypatch, metrics_path: Path, mtime_minutes_ago: float | None):
        """Patch _get_latest_loop_run_mtime to return a specific age."""
        if mtime_minutes_ago is None:
            monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: None)
        else:
            mtime = time.time() - mtime_minutes_ago * 60
            monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)

    def test_healthy_non_idle_maps_to_ok(self, tmp_path, monkeypatch):
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [{"timestamp": _iso(_now_minus(5)), "duration_seconds": 60, "idle": False}])
        self._patch_sources(monkeypatch, p, mtime_minutes_ago=None)
        result = get_loop_health_dashboard(metrics_path=p)
        assert result["status"] == "ok"

    def test_all_idle_maps_to_idle(self, tmp_path, monkeypatch):
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [{"timestamp": _iso(_now_minus(5)), "duration_seconds": 60, "idle": True}])
        self._patch_sources(monkeypatch, p, mtime_minutes_ago=None)
        result = get_loop_health_dashboard(metrics_path=p)
        assert result["status"] == "idle"

    def test_warning_maps_to_warning(self, tmp_path, monkeypatch):
        p = tmp_path / "metrics.jsonl"
        # 45 minutes ago → warning zone (30–60 min)
        _write_metrics(p, [{"timestamp": _iso(_now_minus(45)), "duration_seconds": 100, "idle": False}])
        self._patch_sources(monkeypatch, p, mtime_minutes_ago=None)
        result = get_loop_health_dashboard(metrics_path=p)
        assert result["status"] == "warning"

    def test_no_metrics_maps_to_error(self, tmp_path, monkeypatch):
        self._patch_sources(monkeypatch, None, mtime_minutes_ago=None)
        result = get_loop_health_dashboard(metrics_path=tmp_path / "nonexistent.jsonl")
        assert result["status"] == "error"

    def test_result_always_has_required_keys(self, tmp_path, monkeypatch):
        self._patch_sources(monkeypatch, None, mtime_minutes_ago=None)
        result = get_loop_health_dashboard(metrics_path=tmp_path / "nonexistent.jsonl")
        assert "lastRun" in result
        assert "status" in result
        assert "duration" in result

    def test_duration_from_metrics(self, tmp_path, monkeypatch):
        p = tmp_path / "metrics.jsonl"
        _write_metrics(p, [{"timestamp": _iso(_now_minus(5)), "duration_seconds": 123, "idle": False}])
        self._patch_sources(monkeypatch, p, mtime_minutes_ago=None)
        result = get_loop_health_dashboard(metrics_path=p)
        assert result["duration"] == 123

    def test_duration_defaults_to_zero_when_missing(self, tmp_path, monkeypatch):
        self._patch_sources(monkeypatch, None, mtime_minutes_ago=None)
        result = get_loop_health_dashboard(metrics_path=tmp_path / "none.jsonl")
        assert result["duration"] == 0

    def test_metrics_timestamp_wins_over_older_mtime(self, tmp_path, monkeypatch):
        """If metrics.jsonl last-entry timestamp is newer than loop-runs mtime, it wins."""
        p = tmp_path / "metrics.jsonl"
        # metrics says 5 min ago (fresh)
        fresh_ts = _iso(_now_minus(5))
        _write_metrics(p, [{"timestamp": fresh_ts, "duration_seconds": 60, "idle": False}])
        # But loop-runs mtime says 90 min ago (stale)
        stale_mtime = time.time() - 90 * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: stale_mtime)
        result = get_loop_health_dashboard(metrics_path=p)
        # metrics_epoch wins → fresh → ok
        assert result["status"] == "ok"

    def test_loop_runs_mtime_wins_when_newer(self, tmp_path, monkeypatch):
        """If loop-runs mtime is newer than metrics.jsonl timestamp, it wins."""
        p = tmp_path / "metrics.jsonl"
        # metrics says 90 min ago (stale)
        stale_ts = _iso(_now_minus(90))
        _write_metrics(p, [{"timestamp": stale_ts, "duration_seconds": 60, "idle": False}])
        # But loop-runs mtime says 5 min ago (fresh)
        fresh_mtime = time.time() - 5 * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: fresh_mtime)
        result = get_loop_health_dashboard(metrics_path=p)
        # loop_runs_epoch wins → delegates to check_loop_health → status="healthy" → ok
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# create_alert_issue — duplicate suppression
# ---------------------------------------------------------------------------


class TestCreateAlertIssue:
    def _health_stale(self, age_minutes: float = 90.0) -> dict:
        return {
            "healthy": False,
            "status": "error",
            "last_run": "2026-05-20T01:00:00Z",
            "lastRunAt": "2026-05-20T01:00:00Z",
            "age_minutes": age_minutes,
            "threshold_minutes": 30,
        }

    def test_skips_when_duplicate_exists(self):
        with patch.object(hm_mod, "_open_stale_alert_exists", return_value=True):
            result = create_alert_issue(self._health_stale())
        assert result is None

    def test_creates_issue_when_no_duplicate(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "https://github.com/autonomous-agent-7/autonomous-forever/issues/999"
        with patch.object(hm_mod, "_open_stale_alert_exists", return_value=False), \
             patch("subprocess.run", return_value=mock_proc):
            result = create_alert_issue(self._health_stale())
        assert result is not None
        assert "999" in result

    def test_returns_none_on_subprocess_failure(self):
        import subprocess
        with patch.object(hm_mod, "_open_stale_alert_exists", return_value=False), \
             patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
            result = create_alert_issue(self._health_stale())
        assert result is None

    def test_title_includes_age(self):
        """The Issue title must include the age in minutes."""
        captured_args = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "https://github.com/autonomous-agent-7/autonomous-forever/issues/100\n"
            return mock

        with patch.object(hm_mod, "_open_stale_alert_exists", return_value=False), \
             patch("subprocess.run", side_effect=fake_run):
            create_alert_issue(self._health_stale(age_minutes=75.0))

        title_idx = captured_args.index("--title") if "--title" in captured_args else -1
        assert title_idx >= 0
        title = captured_args[title_idx + 1]
        assert "75" in title
        assert _ALERT_TITLE_PREFIX in title

    def test_no_reason_field_skips_reason_line(self):
        """Health dict without 'reason' key must not crash."""
        health = self._health_stale()
        health.pop("reason", None)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "https://github.com/autonomous-agent-7/autonomous-forever/issues/101\n"
        with patch.object(hm_mod, "_open_stale_alert_exists", return_value=False), \
             patch("subprocess.run", return_value=mock_proc):
            result = create_alert_issue(health)
        assert result is not None

    def test_unknown_age_handled(self):
        """age_minutes=None → 'unknown' in title without crash."""
        health = {
            "healthy": False,
            "status": "error",
            "last_run": None,
            "lastRunAt": None,
            "age_minutes": None,
            "threshold_minutes": 30,
        }
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "https://github.com/autonomous-agent-7/autonomous-forever/issues/102\n"
        with patch.object(hm_mod, "_open_stale_alert_exists", return_value=False), \
             patch("subprocess.run", return_value=mock_proc):
            result = create_alert_issue(health)
        assert result is not None


# ---------------------------------------------------------------------------
# _open_stale_alert_exists — matching logic
# ---------------------------------------------------------------------------


class TestOpenStaleAlertExists:
    def _run_check(self, issues: list[dict]) -> bool:
        payload = json.dumps(issues)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = payload
        with patch("subprocess.run", return_value=mock_proc):
            return hm_mod._open_stale_alert_exists(age_minutes=90)

    def test_matching_title_returns_true(self):
        issues = [{"title": f"{_ALERT_TITLE_PREFIX} — last run 90 minutes ago"}]
        assert self._run_check(issues) is True

    def test_non_matching_title_returns_false(self):
        issues = [{"title": "Some other needs-boss issue"}]
        assert self._run_check(issues) is False

    def test_empty_list_returns_false(self):
        assert self._run_check([]) is False

    def test_subprocess_error_returns_false(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
            result = hm_mod._open_stale_alert_exists(age_minutes=90)
        assert result is False

    def test_json_decode_error_returns_false(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "not-json"
        with patch("subprocess.run", return_value=mock_proc):
            result = hm_mod._open_stale_alert_exists(age_minutes=90)
        assert result is False


# ---------------------------------------------------------------------------
# CLI — check subcommand
# ---------------------------------------------------------------------------


class TestCLICheck:
    def test_exit_0_when_healthy(self, monkeypatch):
        mtime = time.time() - 5 * 60  # 5 minutes ago
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        with pytest.raises(SystemExit) as exc:
            main(["check"])
        assert exc.value.code == 0

    def test_exit_1_when_unhealthy(self, monkeypatch):
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: None)
        with pytest.raises(SystemExit) as exc:
            main(["check"])
        assert exc.value.code == 1

    def test_check_prints_json(self, monkeypatch, capsys):
        mtime = time.time() - 5 * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        with pytest.raises(SystemExit):
            main(["check"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "healthy" in data
        assert "status" in data

    def test_check_threshold_flag(self, monkeypatch):
        """--threshold overrides the default stale threshold."""
        mtime = time.time() - 35 * 60  # 35 min ago → unhealthy with 30 min default
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        with pytest.raises(SystemExit) as exc:
            main(["check", "--threshold", "60"])
        # 35 min < 60 min threshold → healthy → exit 0
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# CLI — alert subcommand
# ---------------------------------------------------------------------------


class TestCLIAlert:
    def test_alert_exit_0_when_healthy(self, monkeypatch):
        mtime = time.time() - 5 * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        with pytest.raises(SystemExit) as exc:
            main(["alert"])
        assert exc.value.code == 0

    def test_alert_exit_1_and_create_issue_when_stale(self, monkeypatch):
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: None)
        with patch.object(hm_mod, "create_alert_issue", return_value="https://github.com/.../issues/1") as mock_create:
            with pytest.raises(SystemExit) as exc:
                main(["alert"])
        assert exc.value.code == 1
        mock_create.assert_called_once()

    def test_alert_does_not_create_issue_when_healthy(self, monkeypatch):
        mtime = time.time() - 5 * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        with patch.object(hm_mod, "create_alert_issue") as mock_create:
            with pytest.raises(SystemExit):
                main(["alert"])
        mock_create.assert_not_called()

    def test_alert_prints_health_json(self, monkeypatch, capsys):
        mtime = time.time() - 5 * 60
        monkeypatch.setattr(hm_mod, "_get_latest_loop_run_mtime", lambda: mtime)
        with pytest.raises(SystemExit):
            main(["alert"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "healthy" in data


# ---------------------------------------------------------------------------
# _get_latest_loop_run_mtime — filesystem behavior (uses tmp dir)
# ---------------------------------------------------------------------------


class TestGetLatestLoopRunMtime:
    """Test _get_latest_loop_run_mtime by monkeypatching _LOOP_RUNS_DIR."""

    def test_returns_none_when_dir_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(hm_mod, "_LOOP_RUNS_DIR", tmp_path / "nonexistent")
        result = hm_mod._get_latest_loop_run_mtime()
        assert result is None

    def test_returns_none_when_no_log_files(self, monkeypatch, tmp_path):
        runs_dir = tmp_path / "loop-runs"
        runs_dir.mkdir()
        (runs_dir / "run-001").mkdir()  # dir but no .log files
        monkeypatch.setattr(hm_mod, "_LOOP_RUNS_DIR", runs_dir)
        result = hm_mod._get_latest_loop_run_mtime()
        assert result is None

    def test_returns_mtime_when_log_exists(self, monkeypatch, tmp_path):
        runs_dir = tmp_path / "loop-runs"
        run_dir = runs_dir / "run-001"
        run_dir.mkdir(parents=True)
        log_file = run_dir / "main.log"
        log_file.write_text("output", encoding="utf-8")
        monkeypatch.setattr(hm_mod, "_LOOP_RUNS_DIR", runs_dir)
        result = hm_mod._get_latest_loop_run_mtime()
        assert result is not None
        # Should be within the last few seconds
        assert abs(result - time.time()) < 10

    def test_returns_max_mtime_across_multiple_logs(self, monkeypatch, tmp_path):
        runs_dir = tmp_path / "loop-runs"
        run1 = runs_dir / "run-001"
        run2 = runs_dir / "run-002"
        run1.mkdir(parents=True)
        run2.mkdir(parents=True)
        old_log = run1 / "main.log"
        old_log.write_text("old", encoding="utf-8")
        new_log = run2 / "main.log"
        new_log.write_text("new", encoding="utf-8")

        # Make run1's log appear older
        import os
        old_mtime = time.time() - 3600
        os.utime(str(old_log), (old_mtime, old_mtime))

        monkeypatch.setattr(hm_mod, "_LOOP_RUNS_DIR", runs_dir)
        result = hm_mod._get_latest_loop_run_mtime()
        assert result is not None
        # Should be the mtime of new_log, not old_log
        assert abs(result - new_log.stat().st_mtime) < 1
