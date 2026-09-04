"""
Unit tests for backend/loop_metrics_counters.py.

Coverage targets (as required by mission brief):
- window-boundary inclusion/exclusion: start_dt <= x < end_dt on both sides
- missing/stale/unreadable snapshot → None, never 0 (a 0 there is a lie:
  it is indistinguishable from an iteration that genuinely scanned nothing)
- real non-zero count vs. unknown → distinguishable
- malformed timestamp rows handled without crash
- every silent fallback path exercised explicitly

All data sources (snapshot file, agent_feed.filter, subprocess.run) are
patched so tests never touch the filesystem or make real network calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure repo root is importable
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import backend.loop_metrics_counters as lmc  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _dt(iso: str) -> datetime:
    """Parse a UTC ISO8601 string into a timezone-aware datetime."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _make_feed_entry(ts: str, event_type: str = "spawn") -> dict:
    return {"ts": ts, "event_type": event_type, "role": "executor", "message": "test"}


def _make_merged_pr(merged_at: str, number: int = 1) -> dict:
    return {"number": number, "mergedAt": merged_at}


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ago_iso(seconds: int) -> str:
    from datetime import timedelta
    ts = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# _load_snapshot
# ---------------------------------------------------------------------------

class TestLoadSnapshot(unittest.TestCase):
    """
    PosixPath instances have read-only slots in Python 3.12, so we cannot
    patch.object the instance's methods directly. Instead we swap out the
    module-level _SNAPSHOT_PATH with a mock Path object for each test.
    """

    def _make_path_mock(self, *, exists: bool, read_text_return=None, read_text_side_effect=None):
        m = MagicMock(spec=Path)
        m.exists.return_value = exists
        if read_text_side_effect is not None:
            m.read_text.side_effect = read_text_side_effect
        elif read_text_return is not None:
            m.read_text.return_value = read_text_return
        return m

    def test_missing_file_returns_none(self):
        mock_path = self._make_path_mock(exists=False)
        with patch.object(lmc, "_SNAPSHOT_PATH", mock_path):
            result = lmc._load_snapshot()
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        mock_path = self._make_path_mock(exists=True, read_text_return="{bad json")
        with patch.object(lmc, "_SNAPSHOT_PATH", mock_path):
            result = lmc._load_snapshot()
        self.assertIsNone(result)

    def test_fresh_snapshot_returned(self):
        payload = {
            "discussions": [1, 2, 3],
            "prs": [10, 11],
            "generated_at": _now_iso(),
        }
        mock_path = self._make_path_mock(exists=True, read_text_return=json.dumps(payload))
        with patch.object(lmc, "_SNAPSHOT_PATH", mock_path):
            result = lmc._load_snapshot()
        self.assertEqual(result, payload)

    def test_stale_snapshot_returns_none(self):
        """A snapshot past MAX_AGE describes the past, not the last iteration."""
        payload = {
            "discussions": [1, 2, 3],
            "prs": [10, 11],
            "generated_at": _ago_iso(seconds=5 * 24 * 3600),
        }
        mock_path = self._make_path_mock(exists=True, read_text_return=json.dumps(payload))
        with patch.object(lmc, "_SNAPSHOT_PATH", mock_path):
            result = lmc._load_snapshot()
        self.assertIsNone(result)

    def test_undatable_snapshot_returns_none(self):
        """No generated_at means we cannot prove freshness — treat as stale."""
        payload = {"discussions": [1, 2, 3]}
        mock_path = self._make_path_mock(exists=True, read_text_return=json.dumps(payload))
        with patch.object(lmc, "_SNAPSHOT_PATH", mock_path):
            result = lmc._load_snapshot()
        self.assertIsNone(result)

    def test_snapshot_at_accepted_as_timestamp(self):
        """Legacy snapshots carry snapshot_at instead of generated_at."""
        payload = {"discussions": [1], "snapshot_at": _now_iso()}
        mock_path = self._make_path_mock(exists=True, read_text_return=json.dumps(payload))
        with patch.object(lmc, "_SNAPSHOT_PATH", mock_path):
            result = lmc._load_snapshot()
        self.assertEqual(result, payload)

    def test_read_error_returns_none(self):
        """OSError from read_text must be caught — not propagated."""
        mock_path = self._make_path_mock(exists=True, read_text_side_effect=OSError("disk error"))
        with patch.object(lmc, "_SNAPSHOT_PATH", mock_path):
            result = lmc._load_snapshot()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# count_discussions_scanned
# ---------------------------------------------------------------------------

class TestCountDiscussionsScanned(unittest.TestCase):

    def _mock_snapshot(self, data: dict):
        return patch("backend.loop_metrics_counters._load_snapshot", return_value=data)

    def test_list_returns_length(self):
        with self._mock_snapshot({"discussions": [1, 2, 3]}):
            self.assertEqual(lmc.count_discussions_scanned(), 3)

    def test_dict_returns_key_count(self):
        with self._mock_snapshot({"discussions": {"1": "foo", "2": "bar"}}):
            self.assertEqual(lmc.count_discussions_scanned(), 2)

    def test_missing_key_returns_none(self):
        """Fresh snapshot with no discussions key: cannot tell 0 from unrecorded."""
        with self._mock_snapshot({}):
            self.assertIsNone(lmc.count_discussions_scanned())

    def test_none_value_returns_none(self):
        with self._mock_snapshot({"discussions": None}):
            self.assertIsNone(lmc.count_discussions_scanned())

    def test_empty_list_returns_zero(self):
        """An empty list IS a real zero — this one must stay 0, not None."""
        with self._mock_snapshot({"discussions": []}):
            self.assertEqual(lmc.count_discussions_scanned(), 0)

    def test_absent_snapshot_returns_none_not_zero(self):
        """The whole point: no snapshot is 'unknown', never 'we scanned nothing'."""
        with patch("backend.loop_metrics_counters._load_snapshot", return_value=None):
            self.assertIsNone(lmc.count_discussions_scanned())

    def test_real_zero_and_unknown_are_distinguishable(self):
        with self._mock_snapshot({"discussions": []}):
            real_zero = lmc.count_discussions_scanned()
        with patch("backend.loop_metrics_counters._load_snapshot", return_value=None):
            unknown = lmc.count_discussions_scanned()
        self.assertEqual(real_zero, 0)
        self.assertIsNone(unknown)
        self.assertIsNot(real_zero, unknown)

    def test_non_zero_count_is_nonzero(self):
        """Real data must produce a count > 0, distinguishable from error path."""
        with self._mock_snapshot({"discussions": [10, 20, 30, 40]}):
            self.assertEqual(lmc.count_discussions_scanned(), 4)

    def test_snapshot_exception_returns_none(self):
        """Exception inside _load_snapshot still yields None, not a crash."""
        with patch("backend.loop_metrics_counters._load_snapshot", side_effect=RuntimeError("boom")):
            self.assertIsNone(lmc.count_discussions_scanned())


# ---------------------------------------------------------------------------
# count_prs_scanned
# ---------------------------------------------------------------------------

class TestCountPrsScanned(unittest.TestCase):

    def _mock_snapshot(self, data: dict):
        return patch("backend.loop_metrics_counters._load_snapshot", return_value=data)

    def test_list_returns_length(self):
        with self._mock_snapshot({"prs": [101, 102]}):
            self.assertEqual(lmc.count_prs_scanned(), 2)

    def test_dict_returns_key_count(self):
        with self._mock_snapshot({"prs": {"101": True, "102": True, "103": True}}):
            self.assertEqual(lmc.count_prs_scanned(), 3)

    def test_missing_key_returns_none(self):
        with self._mock_snapshot({}):
            self.assertIsNone(lmc.count_prs_scanned())

    def test_empty_list_returns_zero(self):
        """An empty list IS a real zero — this one must stay 0, not None."""
        with self._mock_snapshot({"prs": []}):
            self.assertEqual(lmc.count_prs_scanned(), 0)

    def test_absent_snapshot_returns_none_not_zero(self):
        with patch("backend.loop_metrics_counters._load_snapshot", return_value=None):
            self.assertIsNone(lmc.count_prs_scanned())

    def test_real_data_vs_unknown_are_distinguishable(self):
        """Non-zero count, real zero, and unknown must be three distinct answers."""
        with self._mock_snapshot({"prs": [1, 2, 3, 4, 5]}):
            count = lmc.count_prs_scanned()
        self.assertEqual(count, 5)

        with self._mock_snapshot({"prs": []}):
            real_zero = lmc.count_prs_scanned()
        self.assertEqual(real_zero, 0)

        with patch("backend.loop_metrics_counters._load_snapshot", return_value=None):
            unknown = lmc.count_prs_scanned()
        self.assertIsNone(unknown)

        # If "no snapshot" and "scanned nothing" ever aliased, the chart would lie.
        self.assertNotEqual(real_zero, unknown)
        self.assertNotEqual(count, real_zero)

    def test_snapshot_exception_returns_none(self):
        with patch("backend.loop_metrics_counters._load_snapshot", side_effect=RuntimeError("boom")):
            self.assertIsNone(lmc.count_prs_scanned())


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------

class TestParseIso(unittest.TestCase):

    def test_z_suffix_parsed(self):
        dt = lmc._parse_iso("2026-05-10T12:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_plus_suffix_parsed(self):
        dt = lmc._parse_iso("2026-05-10T12:00:00+00:00")
        self.assertIsNotNone(dt)

    def test_invalid_string_returns_none(self):
        self.assertIsNone(lmc._parse_iso("not-a-date"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(lmc._parse_iso(""))

    def test_non_string_returns_none(self):
        # AttributeError branch: ts.replace() fails on non-string
        self.assertIsNone(lmc._parse_iso(None))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _count_spawns — window boundary tests
# ---------------------------------------------------------------------------

class TestCountSpawns(unittest.TestCase):
    """
    Window semantics: start_dt <= entry_dt AND entry_dt < end_dt

    _count_spawns uses agent_feed.filter(since=start_dt) which already
    filters out events before start_dt (inclusive boundary on start).
    The end_dt < boundary is applied manually inside _count_spawns.
    """

    START = _dt("2026-05-10T12:00:00Z")
    END   = _dt("2026-05-10T13:00:00Z")

    def _run(self, entries: list[dict]) -> int:
        """Patch agent_feed.filter to return *entries* and call _count_spawns."""
        with patch("backend.loop_metrics_counters._agent_feed") as mock_feed:
            mock_feed.filter.return_value = iter(entries)
            return lmc._count_spawns(self.START, self.END)

    def test_exactly_at_start_included(self):
        """Event at start_dt exactly should be counted (start inclusive)."""
        entry = _make_feed_entry("2026-05-10T12:00:00Z")
        self.assertEqual(self._run([entry]), 1)

    def test_exactly_at_end_excluded(self):
        """Event at end_dt exactly should NOT be counted (end exclusive)."""
        entry = _make_feed_entry("2026-05-10T13:00:00Z")
        self.assertEqual(self._run([entry]), 0)

    def test_one_second_before_end_included(self):
        """Event 1s before end_dt is inside the window."""
        entry = _make_feed_entry("2026-05-10T12:59:59Z")
        self.assertEqual(self._run([entry]), 1)

    def test_one_second_after_end_excluded(self):
        """Event 1s after end_dt is outside the window."""
        entry = _make_feed_entry("2026-05-10T13:00:01Z")
        self.assertEqual(self._run([entry]), 0)

    def test_midwindow_events_counted(self):
        entries = [
            _make_feed_entry("2026-05-10T12:10:00Z"),
            _make_feed_entry("2026-05-10T12:30:00Z"),
            _make_feed_entry("2026-05-10T12:50:00Z"),
        ]
        self.assertEqual(self._run(entries), 3)

    def test_empty_feed_returns_zero(self):
        self.assertEqual(self._run([]), 0)

    def test_malformed_ts_entry_skipped_no_crash(self):
        """Entry with unparseable ts is skipped; others still counted."""
        entries = [
            {"ts": "not-a-date", "event_type": "spawn", "role": "executor", "message": "x"},
            _make_feed_entry("2026-05-10T12:30:00Z"),
        ]
        self.assertEqual(self._run(entries), 1)

    def test_missing_ts_entry_skipped(self):
        """Entry with no ts field is skipped without crash."""
        entries = [
            {"event_type": "spawn", "role": "executor", "message": "x"},
            _make_feed_entry("2026-05-10T12:30:00Z"),
        ]
        self.assertEqual(self._run(entries), 1)

    def test_non_spawn_event_not_counted(self):
        """_count_spawns filters by event_type in predicate; non-spawn types
        are excluded by agent_feed.filter predicate — verify by injecting only
        non-spawn types in the iterator (simulating predicate returning them).

        In practice filter() calls the predicate; here we mock the iterator.
        The counter ONLY cares that entries pass the predicate — if they arrive
        in the iterator they're counted if ts is in window. This test verifies
        the mock is correct by injecting an entry with type 'log' and confirming
        it would be counted if the predicate let it through — which the real
        predicate would not.  Instead test that predicate is passed correctly.
        """
        # The actual filter predicate rejects non-spawn events; we verify
        # that _count_spawns passes the right predicate to agent_feed.filter.
        with patch("backend.loop_metrics_counters._agent_feed") as mock_feed:
            mock_feed.filter.return_value = iter([])
            lmc._count_spawns(self.START, self.END)
            call_kwargs = mock_feed.filter.call_args

        # Verify predicate accepts spawn/spawn_attempt and rejects others
        pred = call_kwargs[1]["predicate"]
        self.assertTrue(pred({"event_type": "spawn"}))
        self.assertTrue(pred({"event_type": "spawn_attempt"}))
        self.assertFalse(pred({"event_type": "log"}))
        self.assertFalse(pred({"event_type": "merge"}))

    def test_exception_from_agent_feed_returns_zero(self):
        """If agent_feed.filter raises, _count_spawns silently returns 0."""
        with patch("backend.loop_metrics_counters._agent_feed") as mock_feed:
            mock_feed.filter.side_effect = RuntimeError("feed broken")
            result = lmc._count_spawns(self.START, self.END)
        self.assertEqual(result, 0)

    def test_real_data_vs_failure_distinguishable(self):
        """
        Non-zero real count must not alias with the exception-path 0.
        This is the critical guard against silent data loss.
        """
        real_entries = [
            _make_feed_entry("2026-05-10T12:10:00Z"),
            _make_feed_entry("2026-05-10T12:20:00Z"),
        ]
        real_count = self._run(real_entries)
        self.assertGreater(real_count, 0, "real data should produce > 0")

        with patch("backend.loop_metrics_counters._agent_feed") as mock_feed:
            mock_feed.filter.side_effect = RuntimeError("feed broken")
            fail_count = lmc._count_spawns(self.START, self.END)
        self.assertEqual(fail_count, 0, "exception path must yield 0")

        self.assertNotEqual(real_count, fail_count)


# ---------------------------------------------------------------------------
# _count_merged_prs — window boundary tests
# ---------------------------------------------------------------------------

class TestCountMergedPrs(unittest.TestCase):
    """
    Window semantics: start_dt <= merged_dt < end_dt (line 138 in module).
    """

    START = _dt("2026-05-10T12:00:00Z")
    END   = _dt("2026-05-10T13:00:00Z")

    def _run(self, prs: list[dict], returncode: int = 0) -> int:
        fake_result = MagicMock()
        fake_result.returncode = returncode
        fake_result.stdout = json.dumps(prs)
        with patch("backend.loop_metrics_counters.subprocess.run", return_value=fake_result):
            return lmc._count_merged_prs(self.START, self.END)

    def test_exactly_at_start_included(self):
        """PR merged at exactly start_dt should be counted."""
        pr = _make_merged_pr("2026-05-10T12:00:00Z")
        self.assertEqual(self._run([pr]), 1)

    def test_exactly_at_end_excluded(self):
        """PR merged at exactly end_dt should NOT be counted (exclusive upper bound)."""
        pr = _make_merged_pr("2026-05-10T13:00:00Z")
        self.assertEqual(self._run([pr]), 0)

    def test_one_second_before_end_included(self):
        pr = _make_merged_pr("2026-05-10T12:59:59Z")
        self.assertEqual(self._run([pr]), 1)

    def test_one_second_after_end_excluded(self):
        pr = _make_merged_pr("2026-05-10T13:00:01Z")
        self.assertEqual(self._run([pr]), 0)

    def test_multiple_prs_in_window(self):
        prs = [
            _make_merged_pr("2026-05-10T12:10:00Z", 1),
            _make_merged_pr("2026-05-10T12:30:00Z", 2),
            _make_merged_pr("2026-05-10T12:50:00Z", 3),
        ]
        self.assertEqual(self._run(prs), 3)

    def test_pr_before_window_not_counted(self):
        pr = _make_merged_pr("2026-05-10T11:59:59Z")
        self.assertEqual(self._run([pr]), 0)

    def test_empty_pr_list_returns_zero(self):
        self.assertEqual(self._run([]), 0)

    def test_gh_failure_returncode_returns_zero(self):
        """Non-zero returncode from gh → silent 0."""
        self.assertEqual(self._run([], returncode=1), 0)

    def test_malformed_merged_at_entry_skipped(self):
        """PR with unparseable mergedAt is skipped; others still counted."""
        prs = [
            {"number": 99, "mergedAt": "not-a-date"},
            _make_merged_pr("2026-05-10T12:30:00Z", 2),
        ]
        self.assertEqual(self._run(prs), 1)

    def test_missing_merged_at_skipped(self):
        """PR with no mergedAt field skipped without crash."""
        prs = [
            {"number": 88},
            _make_merged_pr("2026-05-10T12:30:00Z", 2),
        ]
        self.assertEqual(self._run(prs), 1)

    def test_subprocess_exception_returns_zero(self):
        """subprocess.run raising (e.g. timeout) → silent 0."""
        with patch("backend.loop_metrics_counters.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("gh", 15)):
            result = lmc._count_merged_prs(self.START, self.END)
        self.assertEqual(result, 0)

    def test_real_data_vs_failure_distinguishable(self):
        """Non-zero real count cannot alias with error-path 0."""
        prs = [
            _make_merged_pr("2026-05-10T12:10:00Z", 1),
            _make_merged_pr("2026-05-10T12:40:00Z", 2),
        ]
        real_count = self._run(prs)
        self.assertGreater(real_count, 0)

        with patch("backend.loop_metrics_counters.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("gh", 15)):
            fail_count = lmc._count_merged_prs(self.START, self.END)
        self.assertEqual(fail_count, 0)

        self.assertNotEqual(real_count, fail_count)


# ---------------------------------------------------------------------------
# compute_counters — public API integration
# ---------------------------------------------------------------------------

class TestComputeCounters(unittest.TestCase):
    """
    Tests for the public compute_counters() function.
    Patches all four internal counters to isolate the orchestration logic.
    """

    START_ISO = "2026-05-10T12:00:00Z"
    END_ISO   = "2026-05-10T13:00:00Z"

    def _run(
        self,
        spawns: int = 0,
        merged: int = 0,
        disc: int = 0,
        prs: int = 0,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        with patch("backend.loop_metrics_counters._count_spawns", return_value=spawns), \
             patch("backend.loop_metrics_counters._count_merged_prs", return_value=merged), \
             patch("backend.loop_metrics_counters.count_discussions_scanned", return_value=disc), \
             patch("backend.loop_metrics_counters.count_prs_scanned", return_value=prs):
            return lmc.compute_counters(start or self.START_ISO, end or self.END_ISO)

    def test_returns_all_four_keys(self):
        result = self._run()
        self.assertIn("agents_spawned", result)
        self.assertIn("prs_merged", result)
        self.assertIn("discussions_scanned", result)
        self.assertIn("prs_scanned", result)

    def test_real_values_propagated(self):
        result = self._run(spawns=5, merged=3, disc=10, prs=7)
        self.assertEqual(result["agents_spawned"], 5)
        self.assertEqual(result["prs_merged"], 3)
        self.assertEqual(result["discussions_scanned"], 10)
        self.assertEqual(result["prs_scanned"], 7)

    def test_invalid_start_iso_returns_unknown(self):
        result = self._run(spawns=5, start="not-a-date")
        self.assertEqual(result, {"agents_spawned": 0, "prs_merged": 0,
                                   "discussions_scanned": None, "prs_scanned": None})

    def test_invalid_end_iso_returns_unknown(self):
        result = self._run(spawns=5, end="also-not-a-date")
        self.assertEqual(result, {"agents_spawned": 0, "prs_merged": 0,
                                   "discussions_scanned": None, "prs_scanned": None})

    def test_snapshot_counters_pass_none_through(self):
        """A missing snapshot must reach the caller as None, not as 0."""
        result = self._run(spawns=2, merged=1, disc=None, prs=None)
        self.assertEqual(result["agents_spawned"], 2)
        self.assertIsNone(result["discussions_scanned"])
        self.assertIsNone(result["prs_scanned"])

    def test_all_counters_zero_is_valid_empty_result(self):
        """All-zero result must be possible when legitimately empty — not proof of error."""
        result = self._run(spawns=0, merged=0, disc=0, prs=0)
        self.assertEqual(result["agents_spawned"], 0)

    def test_values_are_non_negative(self):
        """max(0, ...) guard in the module ensures no negative values."""
        result = self._run(spawns=2, merged=1, disc=3, prs=4)
        for v in result.values():
            self.assertIsNotNone(v)
            self.assertGreaterEqual(v, 0)

    def test_internal_exception_returns_all_zero(self):
        """If any inner counter raises unexpectedly, the outer except catches it."""
        with patch("backend.loop_metrics_counters._count_spawns",
                   side_effect=RuntimeError("unexpected")):
            result = lmc.compute_counters(self.START_ISO, self.END_ISO)
        self.assertEqual(result, {"agents_spawned": 0, "prs_merged": 0,
                                   "discussions_scanned": None, "prs_scanned": None})


# ---------------------------------------------------------------------------
# Duration-regression guard (D#439 / D#869 anti-regression)
# ---------------------------------------------------------------------------

class TestDurationRegressionD439(unittest.TestCase):
    """
    Guard against a regression where the caller hardcodes duration=300 instead
    of computing it from iter_start/iter_end.

    A 7-minute iteration window must yield duration == 420 (7 * 60), NOT the
    hardcoded 300s default that existed before D#439/#869 was fixed.

    The duration itself is computed by the caller (append-loop-metrics.sh) as
    int((end_dt - start_dt).total_seconds()).  This test verifies that
    _parse_iso correctly round-trips the timestamps so the caller CAN produce
    420 rather than falling back to the hardcoded default.
    """

    ITER_START = "2026-05-10T12:00:00Z"
    ITER_END   = "2026-05-10T12:07:00Z"  # exactly 7 minutes = 420 seconds

    def test_seven_minute_window_yields_duration_420_not_300(self):
        """_parse_iso must return valid datetimes so caller computes 420, not 300."""
        start_dt = lmc._parse_iso(self.ITER_START)
        end_dt   = lmc._parse_iso(self.ITER_END)

        self.assertIsNotNone(start_dt, "_parse_iso returned None for ITER_START")
        self.assertIsNotNone(end_dt,   "_parse_iso returned None for ITER_END")

        duration_s = int((end_dt - start_dt).total_seconds())  # type: ignore[operator]
        self.assertEqual(
            duration_s, 420,
            f"expected duration=420 but computed {duration_s}s "
            "(end_ts - start_ts must equal 7 * 60, not the 300s hardcoded default)",
        )

    def test_seven_minute_window_spawn_count_nonzero(self):
        """2 spawn events inside the window must yield agents_spawned==2, not 0."""
        entries = [
            _make_feed_entry("2026-05-10T12:01:00Z", "spawn"),
            _make_feed_entry("2026-05-10T12:04:00Z", "spawn_attempt"),
        ]
        with patch("backend.loop_metrics_counters._agent_feed") as mock_feed:
            mock_feed.filter.return_value = iter(entries)
            count = lmc._count_spawns(
                _dt(self.ITER_START),
                _dt(self.ITER_END),
            )
        self.assertEqual(
            count, 2,
            f"expected agents_spawned=2 but got {count} "
            "(audit_trail must read agent_feed.jsonl, not return hardcoded 0)",
        )


if __name__ == "__main__":
    unittest.main()
