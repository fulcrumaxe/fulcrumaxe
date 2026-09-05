"""Tests for weekly cap extensions in backend/subscription_usage.py

Covers:
  - reset-day parsing (_WEEKDAY_MAP, _parse_reset_time)
  - last_weekly_reset / next_weekly_reset across the reset boundary
  - time_to_reset formatting
  - model-filter token splitting (_sum_tokens_by_model)
  - weekly_usage() output shape and basic math
  - CLI --weekly output shape (via subprocess or direct call)
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Adjust import path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import subscription_usage as su


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    timestamp: datetime,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "",
) -> str:
    """Build a JSONL entry line, optionally with a model field."""
    entry: dict = {
        "timestamp": timestamp.isoformat(),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
    if model:
        entry["model"] = model
    return json.dumps(entry)


def _make_message_entry(
    timestamp: datetime,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "",
) -> str:
    """Build a JSONL entry that nests usage under 'message' (Claude Code transcript shape)."""
    msg: dict = {
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    }
    if model:
        msg["model"] = model
    entry = {
        "timestamp": timestamp.isoformat(),
        "message": msg,
    }
    return json.dumps(entry)


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Weekday map / reset-day parsing
# ---------------------------------------------------------------------------

class TestWeekdayMap:
    def test_thursday_maps_to_3(self):
        assert su._WEEKDAY_MAP["thursday"] == 3

    def test_short_aliases_exist(self):
        assert su._WEEKDAY_MAP["thu"] == 3
        assert su._WEEKDAY_MAP["mon"] == 0
        assert su._WEEKDAY_MAP["fri"] == 4

    def test_all_seven_days_covered(self):
        # At least one entry per weekday 0–6
        covered = set(su._WEEKDAY_MAP.values())
        assert covered == {0, 1, 2, 3, 4, 5, 6}


class TestParseResetTime:
    def test_standard_format(self):
        assert su._parse_reset_time("06:00") == (6, 0)
        assert su._parse_reset_time("14:30") == (14, 30)

    def test_single_digit_hour(self):
        h, m = su._parse_reset_time("6:00")
        assert h == 6
        assert m == 0

    def test_fallback_on_garbage(self):
        assert su._parse_reset_time("not-a-time") == (6, 0)
        assert su._parse_reset_time("") == (6, 0)

    def test_clamps_hour_and_minute(self):
        h, m = su._parse_reset_time("99:99")
        assert h == 23
        assert m == 59


# ---------------------------------------------------------------------------
# last_weekly_reset / next_weekly_reset
# ---------------------------------------------------------------------------

class TestLastWeeklyReset:
    # Use a fixed "now": Wednesday 2026-05-13 at 10:00 UTC
    # America/New_York offset = -5h → local = Wednesday 05:00
    # Last Thursday 06:00 ET = Thursday 2026-05-07 11:00 UTC
    NOW_UTC = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)

    def test_returns_last_thursday_before_now(self):
        last = su.last_weekly_reset("thursday", "06:00", "America/New_York", now=self.NOW_UTC)
        # Should be Thursday 2026-05-07 at 06:00 ET = 11:00 UTC
        assert last.weekday() == 3  # Thursday
        assert last.hour == 11  # 06:00 ET = 11:00 UTC (UTC-5)
        assert last < self.NOW_UTC

    def test_last_reset_is_before_now(self):
        for day in ["monday", "wednesday", "friday", "sunday"]:
            last = su.last_weekly_reset(day, "06:00", "UTC", now=self.NOW_UTC)
            assert last < self.NOW_UTC, f"last_reset for {day} should be before now"

    def test_same_day_after_reset_time_uses_today(self):
        # It's Thursday 2026-05-14 at 15:00 UTC.
        # Reset is Thursday 06:00 UTC → today IS the reset day and we're past it.
        now = datetime(2026, 5, 14, 15, 0, 0, tzinfo=timezone.utc)  # Thursday
        last = su.last_weekly_reset("thursday", "06:00", "UTC", now=now)
        assert last.weekday() == 3
        # Should be today (2026-05-14) at 06:00, not last week
        assert last.year == 2026 and last.month == 5 and last.day == 14

    def test_same_day_before_reset_time_uses_last_week(self):
        # It's Thursday 2026-05-14 at 04:00 UTC.
        # Reset is Thursday 06:00 UTC → we haven't reached today's reset yet.
        now = datetime(2026, 5, 14, 4, 0, 0, tzinfo=timezone.utc)  # Thursday
        last = su.last_weekly_reset("thursday", "06:00", "UTC", now=now)
        assert last.weekday() == 3
        # Should be last Thursday 2026-05-07
        assert last.year == 2026 and last.month == 5 and last.day == 7

    def test_window_spans_at_most_7_days(self):
        now = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
        last = su.last_weekly_reset("thursday", "06:00", "UTC", now=now)
        delta = now - last
        assert delta.total_seconds() <= 7 * 24 * 3600 + 60  # at most 7 days + 1 min tolerance


class TestNextWeeklyReset:
    NOW_UTC = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)

    def test_next_reset_is_after_now(self):
        nxt = su.next_weekly_reset("thursday", "06:00", "UTC", now=self.NOW_UTC)
        assert nxt > self.NOW_UTC

    def test_next_minus_last_equals_7_days(self):
        last = su.last_weekly_reset("thursday", "06:00", "UTC", now=self.NOW_UTC)
        nxt = su.next_weekly_reset("thursday", "06:00", "UTC", now=self.NOW_UTC)
        delta = nxt - last
        # Should be exactly 7 days
        assert abs(delta.total_seconds() - 7 * 24 * 3600) < 1


class TestTimeToReset:
    def test_format_hours_and_minutes(self):
        # 2026-05-13 10:00 UTC, next Thursday reset at ~11:00 UTC (one day ahead)
        now = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
        result = su.time_to_reset("thursday", "06:00", "UTC", now=now)
        # Should produce "Xh Ym" format
        assert "h " in result
        assert result.endswith("m")

    def test_returns_string(self):
        now = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
        r = su.time_to_reset("monday", "12:00", "UTC", now=now)
        assert isinstance(r, str)


# ---------------------------------------------------------------------------
# Model-filter token splitting
# ---------------------------------------------------------------------------

class TestSumTokensByModel:
    def test_sonnet_tokens_counted_separately(self, tmp_path):
        now = datetime.now(timezone.utc)
        e_sonnet = _make_entry(now - timedelta(hours=1), input_tokens=1000, output_tokens=500, model="claude-sonnet-4-5")
        e_opus = _make_entry(now - timedelta(hours=1), input_tokens=200, output_tokens=100, model="claude-opus-4-5")
        e_unknown = _make_entry(now - timedelta(hours=1), input_tokens=50, output_tokens=50)

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [e_sonnet, e_opus, e_unknown])

        start = now - timedelta(hours=5)
        files = su._find_jsonl_files(projects_dir)
        result = su._sum_tokens_by_model(files, start, now)

        # Sonnet entry: 1500 + unknown attributed to sonnet: 100 = 1600
        assert result["sonnet_tokens"] == 1600
        assert result["opus_tokens"] == 300

    def test_message_nested_model_field(self, tmp_path):
        now = datetime.now(timezone.utc)
        e = _make_message_entry(now - timedelta(hours=1), input_tokens=400, output_tokens=200, model="claude-opus-latest")

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [e])

        start = now - timedelta(hours=5)
        files = su._find_jsonl_files(projects_dir)
        result = su._sum_tokens_by_model(files, start, now)

        assert result["opus_tokens"] == 600
        assert result["sonnet_tokens"] == 0

    def test_outside_window_excluded(self, tmp_path):
        now = datetime.now(timezone.utc)
        old_entry = _make_entry(now - timedelta(hours=10), input_tokens=9999, output_tokens=9999, model="claude-sonnet-4")

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [old_entry])

        start = now - timedelta(hours=5)
        files = su._find_jsonl_files(projects_dir)
        result = su._sum_tokens_by_model(files, start, now)

        assert result["sonnet_tokens"] == 0
        assert result["opus_tokens"] == 0

    def test_empty_projects_dir(self, tmp_path):
        missing = tmp_path / "nonexistent"
        files = su._find_jsonl_files(missing)
        now = datetime.now(timezone.utc)
        result = su._sum_tokens_by_model(files, now - timedelta(hours=5), now)
        assert result["sonnet_tokens"] == 0
        assert result["opus_tokens"] == 0


# ---------------------------------------------------------------------------
# weekly_usage() public API
# ---------------------------------------------------------------------------

class TestWeeklyUsage:
    def test_output_shape(self, tmp_path):
        """weekly_usage() returns all required keys."""
        projects_dir = tmp_path / "empty_projects"
        projects_dir.mkdir()
        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.weekly_usage(plan="max-20x")

        required_keys = {
            "weekly_pct_sonnet", "weekly_pct_opus",
            "sonnet_hours_used", "opus_hours_used",
            "sonnet_hours_quota", "opus_hours_quota",
            "sonnet_tokens", "opus_tokens",
            "time_to_reset", "window_start", "window_end",
            "plan", "_note",
        }
        assert required_keys <= set(result.keys())

    def test_zero_tokens_gives_zero_percent(self, tmp_path):
        projects_dir = tmp_path / "empty"
        projects_dir.mkdir()
        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.weekly_usage(plan="max-20x")
        assert result["weekly_pct_sonnet"] == 0.0
        assert result["weekly_pct_opus"] == 0.0

    def test_sonnet_hours_math(self, tmp_path):
        """4M Sonnet tokens → 1.0 Sonnet-hour → 100%/240h ≈ 0.42%."""
        now = datetime.now(timezone.utc)
        # Put entry inside the weekly window (within last 7 days)
        ts = now - timedelta(days=1)
        e = _make_entry(ts, input_tokens=2_000_000, output_tokens=2_000_000, model="claude-sonnet-4-5")

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [e])

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            with patch.object(su, "_config_weekly_reset", return_value=("thursday", "06:00", "UTC")):
                with patch.object(su, "last_weekly_reset", return_value=now - timedelta(days=3)):
                    result = su.weekly_usage(plan="max-20x", now=now)

        # 4M tokens / 4M tokens-per-hour = 1 hour
        assert abs(result["sonnet_hours_used"] - 1.0) < 0.01
        # 1h / 240h quota * 100 ≈ 0.42%
        expected_pct = 1.0 / 240 * 100
        assert abs(result["weekly_pct_sonnet"] - expected_pct) < 0.01

    def test_opus_hours_math(self, tmp_path):
        """800K Opus tokens → 1.0 Opus-hour → 100%/24h ≈ 4.17%."""
        now = datetime.now(timezone.utc)
        ts = now - timedelta(days=1)
        e = _make_entry(ts, input_tokens=400_000, output_tokens=400_000, model="claude-opus-4-5")

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [e])

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            with patch.object(su, "_config_weekly_reset", return_value=("thursday", "06:00", "UTC")):
                with patch.object(su, "last_weekly_reset", return_value=now - timedelta(days=3)):
                    result = su.weekly_usage(plan="max-20x", now=now)

        assert abs(result["opus_hours_used"] - 1.0) < 0.01
        expected_pct = 1.0 / 24 * 100
        assert abs(result["weekly_pct_opus"] - expected_pct) < 0.01

    def test_window_start_equals_last_reset(self, tmp_path):
        projects_dir = tmp_path / "empty2"
        projects_dir.mkdir()
        now = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
        fixed_reset = datetime(2026, 5, 10, 11, 0, 0, tzinfo=timezone.utc)

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            with patch.object(su, "last_weekly_reset", return_value=fixed_reset):
                with patch.object(su, "_config_weekly_reset", return_value=("thursday", "06:00", "UTC")):
                    result = su.weekly_usage(plan="max-20x", now=now)

        assert result["window_start"] == fixed_reset.isoformat()

    def test_plan_sonnet_quota_from_plans_file(self, tmp_path):
        """weekly_usage() reads sonnet_hours_quota from the plans file."""
        custom_plans = {
            "plans": {
                "test-plan": {
                    "window_hours": 5,
                    "tokens_quota": 1_000_000,
                    "weekly": {"sonnet_hours_quota": 50, "opus_hours_quota": 5},
                }
            }
        }
        plans_file = tmp_path / "plans.json"
        plans_file.write_text(json.dumps(custom_plans))
        projects_dir = tmp_path / "empty3"
        projects_dir.mkdir()

        with patch.object(su, "PLANS_FILE", plans_file):
            with patch.object(su, "_projects_dir", return_value=projects_dir):
                result = su.weekly_usage(plan="test-plan")

        assert result["sonnet_hours_quota"] == 50
        assert result["opus_hours_quota"] == 5

    def test_note_field_present(self, tmp_path):
        projects_dir = tmp_path / "empty4"
        projects_dir.mkdir()
        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.weekly_usage()
        assert "_note" in result
        assert "Community-derived" in result["_note"] or "community" in result["_note"].lower()


# ---------------------------------------------------------------------------
# CLI --weekly output
# ---------------------------------------------------------------------------

class TestCLIWeekly:
    REPO_ROOT = Path(__file__).resolve().parent.parent

    def _run_weekly_json(self, extra_args: list[str] | None = None) -> dict:
        cmd = [
            sys.executable,
            str(self.REPO_ROOT / "backend" / "subscription_usage.py"),
            "--weekly", "--json",
        ]
        if extra_args:
            cmd += extra_args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, dict)
        return data

    def test_cli_weekly_json_produces_numeric_sonnet_pct(self):
        data = self._run_weekly_json()
        assert "weekly_pct_sonnet" in data
        assert isinstance(data["weekly_pct_sonnet"], (int, float))

    def test_cli_weekly_json_produces_numeric_opus_pct(self):
        data = self._run_weekly_json()
        assert "weekly_pct_opus" in data
        assert isinstance(data["weekly_pct_opus"], (int, float))

    def test_cli_weekly_json_has_time_to_reset(self):
        data = self._run_weekly_json()
        ttr = data.get("time_to_reset", "")
        assert "h " in ttr and ttr.endswith("m"), f"Unexpected time_to_reset: {ttr!r}"

    def test_cli_weekly_human_output(self):
        cmd = [
            sys.executable,
            str(self.REPO_ROOT / "backend" / "subscription_usage.py"),
            "--weekly",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        out = result.stdout
        assert "sonnet" in out.lower()
        assert "opus" in out.lower()
        assert "reset in" in out.lower()

    def test_cli_weekly_plan_override(self):
        data = self._run_weekly_json(["--plan", "pro"])
        assert data["plan"] == "pro"
        assert data["sonnet_hours_quota"] == 40
