"""Behavioral tests for backend/stats/cosmetic_blocks.py and backend/stats/metric_order.py.

cosmetic_blocks.py — reads JSONL log files from a hook-events directory and
returns hourly / 24-hour block counts.  Tests use a tmp directory via the
hook_events_dir param so no real state is touched.

metric_order.py — pure ordering helper.  METRIC_ORDER constant + sort_metrics().
No I/O; no isolation needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make sure repo root is importable regardless of cwd.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.stats.cosmetic_blocks import blocks_per_hour, total_blocks_24h
from backend.stats.metric_order import METRIC_ORDER, sort_metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    """Format a datetime as the JSONL ts field expected by cosmetic_blocks."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_events(events_dir: Path, day: str, entries: list[dict]) -> None:
    """Write a cosmetic-blocks JSONL file for the given day string (YYYY-MM-DD)."""
    log = events_dir / f"cosmetic-blocks-{day}.jsonl"
    with log.open("w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# blocks_per_hour — empty / no files
# ---------------------------------------------------------------------------

class TestBlocksPerHourEmpty:
    def test_no_files_returns_empty_list(self, tmp_path):
        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert result == []

    def test_empty_jsonl_returns_empty_list(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        (tmp_path / f"cosmetic-blocks-{today}.jsonl").write_text("")
        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert result == []

    def test_unrelated_files_are_ignored(self, tmp_path):
        (tmp_path / "other-events-2026-01-01.jsonl").write_text(
            json.dumps({"ts": "2026-01-01T10:00:00Z"}) + "\n"
        )
        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# blocks_per_hour — basic counting
# ---------------------------------------------------------------------------

class TestBlocksPerHourCounting:
    def test_single_event_appears_once(self, tmp_path):
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        _write_events(tmp_path, today, [{"ts": _iso(now)}])

        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert len(result) == 1
        assert result[0]["count"] == 1

    def test_two_events_same_hour_aggregated(self, tmp_path):
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        _write_events(tmp_path, today, [{"ts": _iso(now)}, {"ts": _iso(now)}])

        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert len(result) == 1
        assert result[0]["count"] == 2

    def test_events_in_different_hours_produce_separate_buckets(self, tmp_path):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        earlier = now - timedelta(hours=1)
        today = now.date().isoformat()
        _write_events(tmp_path, today, [{"ts": _iso(now)}, {"ts": _iso(earlier)}])

        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert len(result) == 2
        counts = {r["count"] for r in result}
        assert counts == {1}

    def test_result_is_sorted_chronologically(self, tmp_path):
        base = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
        today = base.date().isoformat()
        entries = [
            {"ts": _iso(base - timedelta(hours=i))}
            for i in range(5)
        ]
        _write_events(tmp_path, today, entries)

        result = blocks_per_hour(hook_events_dir=tmp_path)
        hours = [r["hour_iso"] for r in result]
        assert hours == sorted(hours)

    def test_hour_bucket_key_format(self, tmp_path):
        # Relative to "now" rather than a hardcoded calendar date — a fixed
        # literal ages out of the since_days=30 window a month after it's
        # written and starts failing regardless of any real code change
        # (caught while working D#2282: this test was failing on an
        # unrelated PR touching a different function in this same file).
        ts = (datetime.now(timezone.utc) - timedelta(days=5)).replace(
            minute=33, second=17, microsecond=0
        )
        day = ts.date().isoformat()
        _write_events(tmp_path, day, [{"ts": _iso(ts)}])

        result = blocks_per_hour(hook_events_dir=tmp_path, since_days=30)
        assert len(result) == 1
        expected_hour_iso = ts.replace(minute=0, second=0).strftime("%Y-%m-%dT%H:00:00Z")
        assert result[0]["hour_iso"] == expected_hour_iso

    def test_events_spanning_multiple_days(self, tmp_path):
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        _write_events(tmp_path, now.date().isoformat(), [{"ts": _iso(now)}])
        _write_events(tmp_path, yesterday.date().isoformat(), [{"ts": _iso(yesterday)}])

        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert len(result) == 2
        total = sum(r["count"] for r in result)
        assert total == 2


# ---------------------------------------------------------------------------
# blocks_per_hour — cutoff / retention
# ---------------------------------------------------------------------------

class TestBlocksPerHourCutoff:
    def test_old_events_outside_retention_are_excluded(self, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=10)
        _write_events(tmp_path, old.date().isoformat(), [{"ts": _iso(old)}])

        result = blocks_per_hour(hook_events_dir=tmp_path, since_days=7)
        assert result == []

    def test_event_exactly_at_cutoff_boundary_is_included(self, tmp_path):
        # 6 days and 23 hours ago — inside the 7-day window
        ts = datetime.now(timezone.utc) - timedelta(days=6, hours=23)
        _write_events(tmp_path, ts.date().isoformat(), [{"ts": _iso(ts)}])

        result = blocks_per_hour(hook_events_dir=tmp_path, since_days=7)
        assert len(result) == 1

    def test_since_days_parameter_limits_window(self, tmp_path):
        three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
        _write_events(tmp_path, three_days_ago.date().isoformat(), [{"ts": _iso(three_days_ago)}])

        # With a 2-day window the 3-day-old event should be excluded
        result = blocks_per_hour(hook_events_dir=tmp_path, since_days=2)
        assert result == []

        # With a 4-day window it should be included
        result = blocks_per_hour(hook_events_dir=tmp_path, since_days=4)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# blocks_per_hour — malformed input tolerance
# ---------------------------------------------------------------------------

class TestBlocksPerHourMalformed:
    def test_blank_lines_are_skipped(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        log = tmp_path / f"cosmetic-blocks-{today}.jsonl"
        log.write_text("\n\n\n")
        assert blocks_per_hour(hook_events_dir=tmp_path) == []

    def test_invalid_json_lines_are_skipped(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        now = datetime.now(timezone.utc)
        log = tmp_path / f"cosmetic-blocks-{today}.jsonl"
        log.write_text("not-json\n" + json.dumps({"ts": _iso(now)}) + "\n")

        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert len(result) == 1
        assert result[0]["count"] == 1

    def test_missing_ts_field_skipped(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        now = datetime.now(timezone.utc)
        _write_events(tmp_path, today, [
            {"no_ts": "here"},
            {"ts": _iso(now)},
        ])

        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert result[0]["count"] == 1

    def test_unparseable_ts_is_skipped(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        now = datetime.now(timezone.utc)
        _write_events(tmp_path, today, [
            {"ts": "not-a-timestamp"},
            {"ts": _iso(now)},
        ])

        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert result[0]["count"] == 1

    def test_empty_ts_string_is_skipped(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        now = datetime.now(timezone.utc)
        _write_events(tmp_path, today, [
            {"ts": ""},
            {"ts": _iso(now)},
        ])

        result = blocks_per_hour(hook_events_dir=tmp_path)
        assert result[0]["count"] == 1


# ---------------------------------------------------------------------------
# total_blocks_24h — basic
# ---------------------------------------------------------------------------

class TestTotalBlocks24h:
    def test_no_files_returns_zero(self, tmp_path):
        assert total_blocks_24h(hook_events_dir=tmp_path) == 0

    def test_single_recent_event_counts_one(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_events(tmp_path, now.date().isoformat(), [{"ts": _iso(now)}])
        assert total_blocks_24h(hook_events_dir=tmp_path) == 1

    def test_multiple_recent_events_summed(self, tmp_path):
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        _write_events(tmp_path, today, [{"ts": _iso(now)}] * 5)
        assert total_blocks_24h(hook_events_dir=tmp_path) == 5

    def test_old_event_beyond_24h_excluded(self, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        _write_events(tmp_path, old.date().isoformat(), [{"ts": _iso(old)}])
        assert total_blocks_24h(hook_events_dir=tmp_path) == 0

    def test_events_straddling_midnight_both_counted(self, tmp_path):
        now = datetime.now(timezone.utc)
        # Use an event from 23 hours ago — always in the 24h window.
        # Write it to the appropriate calendar-day file.  If both events land
        # on the same calendar day (e.g. it is currently 23:30 UTC), write
        # them together so _write_events doesn't silently overwrite the file.
        earlier = now - timedelta(hours=23)
        today_str = now.date().isoformat()
        yesterday_str = earlier.date().isoformat()
        if today_str == yesterday_str:
            _write_events(tmp_path, today_str, [{"ts": _iso(now)}, {"ts": _iso(earlier)}])
        else:
            _write_events(tmp_path, today_str, [{"ts": _iso(now)}])
            _write_events(tmp_path, yesterday_str, [{"ts": _iso(earlier)}])
        assert total_blocks_24h(hook_events_dir=tmp_path) == 2

    def test_malformed_entries_do_not_raise(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        now = datetime.now(timezone.utc)
        log = tmp_path / f"cosmetic-blocks-{today}.jsonl"
        log.write_text("bad-json\n" + json.dumps({"ts": _iso(now)}) + "\n")
        assert total_blocks_24h(hook_events_dir=tmp_path) == 1

    def test_event_just_inside_24h_window_included(self, tmp_path):
        ts = datetime.now(timezone.utc) - timedelta(hours=23, minutes=59)
        _write_events(tmp_path, ts.date().isoformat(), [{"ts": _iso(ts)}])
        assert total_blocks_24h(hook_events_dir=tmp_path) == 1


# ---------------------------------------------------------------------------
# METRIC_ORDER — constant sanity checks
# ---------------------------------------------------------------------------

class TestMetricOrderConstant:
    def test_is_nonempty_list(self):
        assert isinstance(METRIC_ORDER, list)
        assert len(METRIC_ORDER) > 0

    def test_all_entries_are_strings(self):
        assert all(isinstance(m, str) for m in METRIC_ORDER)

    def test_no_duplicates(self):
        assert len(METRIC_ORDER) == len(set(METRIC_ORDER))

    def test_known_core_metrics_present(self):
        core = {
            "loop_iteration_duration_seconds",
            "time_to_merge_seconds",
            "fix_cycle_count",
            "cost_per_merged_pr_usd",
        }
        assert core.issubset(set(METRIC_ORDER))

    def test_cost_attribution_unresolved_count_present(self):
        """D#2282: the suppression counter has a display position, same as
        the metric it stands in for when the resolver isn't agent_run."""
        assert "cost_attribution_unresolved_count" in METRIC_ORDER


# ---------------------------------------------------------------------------
# sort_metrics — ordering behaviour
# ---------------------------------------------------------------------------

class TestSortMetrics:
    def test_empty_list_returns_empty(self):
        assert sort_metrics([]) == []

    def test_single_known_metric_returned(self):
        m = [{"name": "fix_cycle_count", "value": 3}]
        result = sort_metrics(m)
        assert len(result) == 1
        assert result[0]["name"] == "fix_cycle_count"

    def test_known_metrics_follow_canonical_order(self):
        metrics = [
            {"name": "fix_cycle_count"},
            {"name": "loop_iteration_duration_seconds"},
            {"name": "cost_per_merged_pr_usd"},
        ]
        result = sort_metrics(metrics)
        names = [r["name"] for r in result]
        # loop_iteration_duration_seconds is first in METRIC_ORDER
        assert names[0] == "loop_iteration_duration_seconds"
        assert names[1] == "fix_cycle_count"
        assert names[2] == "cost_per_merged_pr_usd"

    def test_unknown_metric_appended_after_known(self):
        metrics = [
            {"name": "zzz_unknown"},
            {"name": "fix_cycle_count"},
        ]
        result = sort_metrics(metrics)
        names = [r["name"] for r in result]
        assert names[0] == "fix_cycle_count"
        assert names[-1] == "zzz_unknown"

    def test_multiple_unknown_metrics_sorted_alphabetically(self):
        metrics = [
            {"name": "zebra_metric"},
            {"name": "alpha_metric"},
            {"name": "mid_metric"},
        ]
        result = sort_metrics(metrics)
        names = [r["name"] for r in result]
        assert names == ["alpha_metric", "mid_metric", "zebra_metric"]

    def test_unknown_metrics_after_all_known(self):
        metrics = [
            {"name": "aaa_unknown"},
            {"name": "loop_iteration_duration_seconds"},
            {"name": "fix_cycle_count"},
            {"name": "bbb_unknown"},
        ]
        result = sort_metrics(metrics)
        known_names = set(METRIC_ORDER)
        in_order = [r["name"] for r in result if r["name"] in known_names]
        after_order = [r["name"] for r in result if r["name"] not in known_names]

        # Known metrics come first, unknowns at the end
        known_indices = [i for i, r in enumerate(result) if r["name"] in known_names]
        unknown_indices = [i for i, r in enumerate(result) if r["name"] not in known_names]
        assert max(known_indices) < min(unknown_indices)
        # Unknowns are alphabetical
        assert after_order == sorted(after_order)

    def test_all_known_metrics_returned_in_presence_order(self):
        # Build input in reverse METRIC_ORDER
        metrics = [{"name": n} for n in reversed(METRIC_ORDER)]
        result = sort_metrics(metrics)
        names = [r["name"] for r in result]
        assert names == METRIC_ORDER

    def test_duplicate_names_last_wins(self):
        # dict comprehension keeps the last occurrence; sort_metrics uses by_name
        metrics = [
            {"name": "fix_cycle_count", "value": 1},
            {"name": "fix_cycle_count", "value": 2},
        ]
        result = sort_metrics(metrics)
        # Should appear exactly once
        assert len([r for r in result if r["name"] == "fix_cycle_count"]) == 1
        assert result[0]["value"] == 2

    def test_metric_dict_passthrough_preserves_extra_fields(self):
        metrics = [{"name": "fix_cycle_count", "value": 42, "unit": "count"}]
        result = sort_metrics(metrics)
        assert result[0]["value"] == 42
        assert result[0]["unit"] == "count"

    def test_subset_of_known_metrics_only_those_returned(self):
        # Only 2 of 13 known metrics; unknown ones should not appear
        metrics = [
            {"name": "fix_cycle_count"},
            {"name": "orphan_worktree_rate"},
        ]
        result = sort_metrics(metrics)
        names = [r["name"] for r in result]
        assert set(names) == {"fix_cycle_count", "orphan_worktree_rate"}
        # fix_cycle_count comes before orphan_worktree_rate in METRIC_ORDER
        assert names.index("fix_cycle_count") < names.index("orphan_worktree_rate")
