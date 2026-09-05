"""Tests for backend/subscription_usage.py

Covers:
  - empty / missing project dir → 0%
  - synthetic JSONL with known token sums → expected %
  - over-quota (>100%) is not clipped
  - window edge: 4h59m included, 5h01m excluded
  - malformed lines tolerated
  - --plan arg overrides config
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Adjust import path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import subscription_usage as su


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    timestamp: datetime,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> str:
    """Build a JSONL entry line."""
    entry = {
        "timestamp": timestamp.isoformat(),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }
    return json.dumps(entry)


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCurrentUsage:
    """Tests for subscription_usage.current_usage()."""

    def test_empty_projects_dir_returns_zero(self, tmp_path):
        """Missing ~/.claude/projects/ → 0 tokens, 0%."""
        missing_dir = tmp_path / "nonexistent"
        with patch.object(su, "_projects_dir", return_value=missing_dir):
            result = su.current_usage(plan="max-20x")

        assert result["tokens_used"] == 0
        assert result["percent"] == 0.0
        assert result["plan"] == "max-20x"
        assert "window_start" in result
        assert "window_end" in result

    def test_empty_projects_dir_exists_but_no_files(self, tmp_path):
        """~/.claude/projects/ exists but is empty → 0%."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.current_usage(plan="max-20x")

        assert result["tokens_used"] == 0
        assert result["percent"] == 0.0

    def test_synthetic_tokens_expected_percent(self, tmp_path):
        """Synthetic JSONL with known sums → correct % against plan quota."""
        now = datetime.now(timezone.utc)
        # Use max-20x plan: quota = 4,400,000
        # We'll add entries summing to 440,000 tokens → expect 10%
        entry1 = _make_entry(now - timedelta(hours=1), input_tokens=200_000, output_tokens=100_000)
        entry2 = _make_entry(now - timedelta(hours=2), input_tokens=100_000, output_tokens=40_000)
        # total = 200k+100k + 100k+40k = 440k → 10% of 4.4M

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "proj-abc" / "session.jsonl", [entry1, entry2])

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.current_usage(plan="max-20x", now=now)

        assert result["tokens_used"] == 440_000
        assert abs(result["percent"] - 10.0) < 0.01
        assert result["tokens_quota"] == 4_400_000

    def test_over_quota_not_clipped(self, tmp_path):
        """Usage >100% is reported as-is, not clipped to 100."""
        now = datetime.now(timezone.utc)
        # max-20x quota = 4,400,000 — use 5,000,000 tokens (>113%)
        entry = _make_entry(now - timedelta(minutes=30), input_tokens=3_000_000, output_tokens=2_000_000)

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "proj-x" / "t.jsonl", [entry])

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.current_usage(plan="max-20x", now=now)

        assert result["tokens_used"] == 5_000_000
        assert result["percent"] > 100.0

    def test_window_edge_4h59m_included(self, tmp_path):
        """Entry 4h59m ago is within the 5h window → counted."""
        now = datetime.now(timezone.utc)
        ts = now - timedelta(hours=4, minutes=59)
        entry = _make_entry(ts, input_tokens=1000, output_tokens=500)

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [entry])

        # Patch _get_blackboard to None so the real blackboard's stored reset
        # time doesn't interfere — last_5h_reset would otherwise return a recent
        # cached value and exclude the 4h59m entry.
        with patch.object(su, "_projects_dir", return_value=projects_dir), \
                patch.object(su, "_get_blackboard", None):
            result = su.current_usage(plan="max-20x", now=now)

        assert result["tokens_used"] == 1500

    def test_window_edge_5h01m_excluded(self, tmp_path):
        """Entry 5h01m ago is outside the 5h window → not counted."""
        now = datetime.now(timezone.utc)
        ts = now - timedelta(hours=5, minutes=1)
        entry = _make_entry(ts, input_tokens=1000, output_tokens=500)

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [entry])

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.current_usage(plan="max-20x", now=now)

        assert result["tokens_used"] == 0

    def test_malformed_lines_tolerated(self, tmp_path):
        """Bad JSON lines, missing fields, wrong types → skipped gracefully."""
        now = datetime.now(timezone.utc)
        good_entry = _make_entry(now - timedelta(hours=1), input_tokens=100, output_tokens=50)

        bad_lines = [
            "{not valid json",
            '{"timestamp": "not-a-timestamp", "usage": {"input_tokens": 99}}',
            '{"usage": {"input_tokens": 10, "output_tokens": 5}}',  # missing timestamp
            "null",
            "",
            "   ",
            '{"timestamp": "' + (now - timedelta(hours=1)).isoformat() + '", "usage": "wrong-type"}',
            good_entry,
        ]

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "mixed.jsonl", bad_lines)

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.current_usage(plan="max-20x", now=now)

        # Only the good_entry should count: 100 + 50 = 150
        assert result["tokens_used"] == 150

    def test_cache_tokens_excluded(self, tmp_path):
        """cache_read_input_tokens and cache_creation_input_tokens are NOT counted."""
        now = datetime.now(timezone.utc)
        entry = _make_entry(
            now - timedelta(hours=1),
            input_tokens=1000,
            output_tokens=500,
            cache_read=99_999,
            cache_creation=99_999,
        )

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [entry])

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.current_usage(plan="max-20x", now=now)

        # Only input + output counted — cache excluded
        assert result["tokens_used"] == 1500

    def test_plan_arg_overrides_config(self, tmp_path):
        """--plan arg (or plan= kwarg) overrides whatever is in config."""
        now = datetime.now(timezone.utc)
        entry = _make_entry(now - timedelta(hours=1), input_tokens=110_000, output_tokens=110_000)

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "p" / "s.jsonl", [entry])

        # Config says max-20x (quota=4.4M), but we pass plan="pro" (quota=220k)
        with patch.object(su, "_projects_dir", return_value=projects_dir):
            with patch.object(su, "_load_config", return_value={"subscription": {"plan": "max-20x"}}):
                result = su.current_usage(plan="pro", now=now)

        assert result["plan"] == "pro"
        assert result["tokens_quota"] == 220_000
        # 220k tokens / 220k quota → 100%
        assert abs(result["percent"] - 100.0) < 0.01

    def test_unknown_plan_falls_back_to_max_20x(self, tmp_path):
        """Unknown plan name falls back to max-20x quota."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.current_usage(plan="ultra-9000x")

        assert result["tokens_quota"] == 4_400_000
        assert result["plan"] == "ultra-9000x"  # name kept as-is

    def test_result_has_all_required_keys(self, tmp_path):
        """current_usage() always returns all required keys."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(su, "_projects_dir", return_value=projects_dir):
            result = su.current_usage(plan="max-20x")

        required_keys = {
            "percent", "tokens_used", "tokens_quota",
            "window_start", "window_end", "plan", "window_hours",
        }
        assert required_keys <= set(result.keys())

    def test_multi_file_aggregation(self, tmp_path):
        """Tokens are summed across multiple JSONL files."""
        now = datetime.now(timezone.utc)
        e1 = _make_entry(now - timedelta(hours=1), input_tokens=100, output_tokens=50)
        e2 = _make_entry(now - timedelta(hours=2), input_tokens=200, output_tokens=75)
        e3 = _make_entry(now - timedelta(hours=3), input_tokens=50, output_tokens=25)

        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir / "proj-a" / "session1.jsonl", [e1])
        _write_jsonl(projects_dir / "proj-b" / "session2.jsonl", [e2])
        _write_jsonl(projects_dir / "proj-c" / "nested" / "session3.jsonl", [e3])

        # Patch _get_blackboard to None so the real blackboard's stored reset
        # time doesn't shrink the window and exclude the 3h-old entry.
        with patch.object(su, "_projects_dir", return_value=projects_dir), \
                patch.object(su, "_get_blackboard", None):
            result = su.current_usage(plan="max-20x", now=now)

        assert result["tokens_used"] == (100 + 50 + 200 + 75 + 50 + 25)  # 500


class TestPlanLoading:
    """Tests for plan table loading fallback logic."""

    def test_builtin_fallback_when_plans_file_missing(self, tmp_path):
        """If subscription-plans.json is absent, built-in defaults are used."""
        with patch.object(su, "PLANS_FILE", tmp_path / "nonexistent.json"):
            plans = su._load_plans()

        assert "max-20x" in plans
        assert plans["max-20x"]["tokens_quota"] == 4_400_000

    def test_plans_file_overrides_builtins(self, tmp_path):
        """Custom subscription-plans.json values take precedence."""
        custom_plans = {
            "plans": {
                "custom-plan": {"window_hours": 3, "tokens_quota": 999_999},
            }
        }
        plans_file = tmp_path / "subscription-plans.json"
        plans_file.write_text(json.dumps(custom_plans))

        with patch.object(su, "PLANS_FILE", plans_file):
            plans = su._load_plans()

        assert "custom-plan" in plans
        assert plans["custom-plan"]["tokens_quota"] == 999_999

    def test_malformed_plans_file_falls_back_to_builtins(self, tmp_path):
        """Malformed JSON in plans file → fall back to built-ins."""
        plans_file = tmp_path / "subscription-plans.json"
        plans_file.write_text("{not valid json")

        with patch.object(su, "PLANS_FILE", plans_file):
            plans = su._load_plans()

        assert "max-20x" in plans
