"""
Unit tests for the pure compute functions in backend/kpi_engine.py.

Each test builds its inputs inline — no live registry, no loop-metrics file,
no kpi.json write.  Time-relative functions patch ``backend.kpi_engine._now_utc``
to a fixed UTC instant so consecutive runs are byte-identical.

Out of scope (covered by test_kpi_rpc.py): history(), cycle_time_histogram(),
compute_all(), show(), main(), load_registry(), load_loop_metrics().
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Allow imports from repo root regardless of test runner cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Fixed "now" used by all time-relative tests.
_FIXED_NOW = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    """Return ISO-8601 string suitable for kpi_engine._parse_iso."""
    return dt.isoformat()


# ---------------------------------------------------------------------------
# extract_actual_hours_from_body
# ---------------------------------------------------------------------------

class TestExtractActualHoursFromBody(unittest.TestCase):
    def _call(self, body: str):
        from backend.kpi_engine import extract_actual_hours_from_body
        return extract_actual_hours_from_body(body)

    def test_valid_completion_block_returns_float(self):
        """AC-1: valid COMPLETION block with actual_hours: 4.5 → 4.5."""
        body = (
            "Some preamble\n"
            "<!-- COMPLETION -->\n"
            "actual_hours: 4.5\n"
            "<!-- /COMPLETION -->\n"
            "Some epilogue"
        )
        self.assertEqual(self._call(body), 4.5)

    def test_no_completion_block_returns_none(self):
        """AC-2: body with no COMPLETION block → None."""
        self.assertIsNone(self._call("No special block here."))

    def test_completion_block_without_actual_hours_returns_none(self):
        """AC-3: COMPLETION block present but no actual_hours line → None."""
        body = (
            "<!-- COMPLETION -->\n"
            "estimated_hours: 2.0\n"
            "<!-- /COMPLETION -->"
        )
        self.assertIsNone(self._call(body))

    def test_malformed_value_returns_none_no_exception(self):
        """AC-4: actual_hours: abc → None, no exception."""
        body = (
            "<!-- COMPLETION -->\n"
            "actual_hours: abc\n"
            "<!-- /COMPLETION -->"
        )
        self.assertIsNone(self._call(body))


# ---------------------------------------------------------------------------
# compute_velocity
# ---------------------------------------------------------------------------

class TestComputeVelocity(unittest.TestCase):
    def _call(self, discussions):
        from backend.kpi_engine import compute_velocity
        with patch("backend.kpi_engine._now_utc", return_value=_FIXED_NOW):
            return compute_velocity(discussions)

    def test_empty_list_returns_zeroes(self):
        """AC-5: empty list → all zeros."""
        result = self._call([])
        self.assertEqual(result, {"last_24h": 0, "all_time_per_day": 0.0, "total_done": 0})

    def test_24h_window_and_total_done(self):
        """AC-6: N DONE where M within last 24h → last_24h==M, total_done==N."""
        # 2 within last 24h, 1 older (48h ago)
        discussions = [
            {
                "status": "DONE",
                "closed_at": _iso(_FIXED_NOW - timedelta(hours=1)),
            },
            {
                "status": "DONE",
                "closed_at": _iso(_FIXED_NOW - timedelta(hours=12)),
            },
            {
                "status": "DONE",
                "closed_at": _iso(_FIXED_NOW - timedelta(hours=48)),
            },
        ]
        result = self._call(discussions)
        self.assertEqual(result["last_24h"], 2)
        self.assertEqual(result["total_done"], 3)
        self.assertIsInstance(result["all_time_per_day"], float)

    def test_non_done_and_missing_closed_at_excluded(self):
        """AC-7: non-DONE or missing closed_at not counted in last_24h, no raise."""
        discussions = [
            # DISCUSSING → excluded from done list entirely
            {"status": "DISCUSSING", "closed_at": _iso(_FIXED_NOW - timedelta(hours=1))},
            # DONE but closed_at is None → counted in total_done, not in last_24h
            {"status": "DONE", "closed_at": None},
            # DONE but no closed_at key → counted in total_done, not in last_24h
            {"status": "DONE"},
        ]
        result = self._call(discussions)
        self.assertEqual(result["last_24h"], 0)
        # Only the two DONE rows appear in total_done
        self.assertEqual(result["total_done"], 2)

    def test_non_done_excluded_from_total(self):
        """Non-DONE discussions excluded from total_done count."""
        discussions = [
            {"status": "DONE", "closed_at": _iso(_FIXED_NOW - timedelta(hours=2))},
            {"status": "SPEC_READY", "closed_at": _iso(_FIXED_NOW - timedelta(hours=1))},
        ]
        result = self._call(discussions)
        self.assertEqual(result["total_done"], 1)
        self.assertEqual(result["last_24h"], 1)


# ---------------------------------------------------------------------------
# compute_estimation_accuracy
# ---------------------------------------------------------------------------

class TestComputeEstimationAccuracy(unittest.TestCase):
    def _call(self, discussions):
        from backend.kpi_engine import compute_estimation_accuracy
        return compute_estimation_accuracy(discussions)

    def test_empty_returns_zero_tasks_none_stats(self):
        """AC-8: empty list → tasks_with_estimates==0, mean/pct None."""
        result = self._call([])
        self.assertEqual(result["tasks_with_estimates"], 0)
        self.assertIsNone(result["mean_absolute_error_hours"])
        self.assertIsNone(result["within_1_5x_pct"])

    def test_happy_path_frontmatter_and_completion(self):
        """AC-9: frontmatter.estimated_hours + completion.actual_hours pair."""
        discussions = [
            {
                "status": "DONE",
                "frontmatter": {"estimated_hours": 4.0},
                "completion": {"actual_hours": 5.0},
            },
            {
                "status": "DONE",
                "frontmatter": {"estimated_hours": 3.0},
                "completion": {"actual_hours": 3.0},
            },
        ]
        result = self._call(discussions)
        self.assertEqual(result["tasks_with_estimates"], 2)
        self.assertIsNotNone(result["mean_absolute_error_hours"])
        # errors: |4-5|=1, |3-3|=0 → mean=0.5
        self.assertAlmostEqual(result["mean_absolute_error_hours"], 0.5, places=3)
        # within 1.5x: 5 <= 4*1.5=6 (yes), 3 <= 3*1.5=4.5 (yes) → 100%
        self.assertAlmostEqual(result["within_1_5x_pct"], 100.0, places=1)

    def test_fallback_to_body_completion_block(self):
        """AC-9 (body fallback): actual_hours sourced from COMPLETION body block."""
        body = (
            "<!-- COMPLETION -->\n"
            "actual_hours: 6.0\n"
            "<!-- /COMPLETION -->"
        )
        discussions = [
            {
                "status": "DONE",
                "estimated_hours": 4.0,
                "body": body,
            }
        ]
        result = self._call(discussions)
        self.assertEqual(result["tasks_with_estimates"], 1)
        # |4 - 6| = 2
        self.assertAlmostEqual(result["mean_absolute_error_hours"], 2.0, places=3)

    def test_within_1_5x_pct_in_range(self):
        """AC-9: within_1_5x_pct is in [0, 100]."""
        discussions = [
            {
                "frontmatter": {"estimated_hours": 2.0},
                "completion": {"actual_hours": 10.0},  # way over → not within 1.5x
            }
        ]
        result = self._call(discussions)
        self.assertGreaterEqual(result["within_1_5x_pct"], 0.0)
        self.assertLessEqual(result["within_1_5x_pct"], 100.0)

    def test_zero_or_negative_estimated_skipped(self):
        """AC-10: estimated_hours <= 0 skipped; non-numeric skipped; no raise."""
        discussions = [
            {"estimated_hours": 0, "actual_hours": 3.0},
            {"estimated_hours": -1.0, "actual_hours": 2.0},
            {"estimated_hours": "n/a", "actual_hours": 2.0},
            {"estimated_hours": 2.0, "actual_hours": "bad"},
        ]
        result = self._call(discussions)
        self.assertEqual(result["tasks_with_estimates"], 0)
        self.assertIsNone(result["mean_absolute_error_hours"])


# ---------------------------------------------------------------------------
# compute_pr_cycle_time
# ---------------------------------------------------------------------------

class TestComputePrCycleTime(unittest.TestCase):
    def _call(self, discussions):
        from backend.kpi_engine import compute_pr_cycle_time
        return compute_pr_cycle_time(discussions)

    def test_empty_returns_none_stats(self):
        """AC-11: empty list → mean/median None, total_measured==0."""
        result = self._call([])
        self.assertEqual(result, {"mean_hours": None, "median_hours": None, "total_measured": 0})

    def test_hand_computed_mean_median(self):
        """AC-12: two DONE discussions of 2h and 4h → mean 3.0, median 3.0."""
        base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
        discussions = [
            {
                "status": "DONE",
                "created_at": _iso(base),
                "closed_at": _iso(base + timedelta(hours=2)),
            },
            {
                "status": "DONE",
                "created_at": _iso(base),
                "closed_at": _iso(base + timedelta(hours=4)),
            },
        ]
        result = self._call(discussions)
        self.assertEqual(result["total_measured"], 2)
        self.assertAlmostEqual(result["mean_hours"], 3.0, places=2)
        self.assertAlmostEqual(result["median_hours"], 3.0, places=2)

    def test_closed_lte_created_excluded(self):
        """AC-13: closed_at <= created_at excluded; non-DONE excluded."""
        base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
        discussions = [
            # closed == created → excluded
            {
                "status": "DONE",
                "created_at": _iso(base),
                "closed_at": _iso(base),
            },
            # not DONE → excluded
            {
                "status": "DISCUSSING",
                "created_at": _iso(base),
                "closed_at": _iso(base + timedelta(hours=5)),
            },
            # valid
            {
                "status": "DONE",
                "created_at": _iso(base),
                "closed_at": _iso(base + timedelta(hours=6)),
            },
        ]
        result = self._call(discussions)
        self.assertEqual(result["total_measured"], 1)
        self.assertAlmostEqual(result["mean_hours"], 6.0, places=2)


# ---------------------------------------------------------------------------
# compute_idle_rate
# ---------------------------------------------------------------------------

class TestComputeIdleRate(unittest.TestCase):
    def _call(self, metrics):
        from backend.kpi_engine import compute_idle_rate
        with patch("backend.kpi_engine._now_utc", return_value=_FIXED_NOW):
            return compute_idle_rate(metrics)

    def test_empty_returns_none_stats(self):
        """AC-14: empty list → last_24h_pct/all_time_pct None, total_iterations==0."""
        result = self._call([])
        self.assertEqual(result, {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0})

    def test_all_time_pct_exact(self):
        """AC-15: 2 of 4 idle → all_time_pct==50.0, total_iterations==4."""
        metrics = [
            {"idle": True,  "timestamp": _iso(_FIXED_NOW - timedelta(hours=3))},
            {"idle": True,  "timestamp": _iso(_FIXED_NOW - timedelta(hours=5))},
            {"idle": False, "timestamp": _iso(_FIXED_NOW - timedelta(hours=7))},
            {"idle": False, "timestamp": _iso(_FIXED_NOW - timedelta(hours=9))},
        ]
        result = self._call(metrics)
        self.assertEqual(result["total_iterations"], 4)
        self.assertAlmostEqual(result["all_time_pct"], 50.0, places=1)

    def test_last_24h_pct_deterministic(self):
        """AC-15: with patched _now_utc, last_24h_pct is deterministic."""
        # 2 rows within last 24h (1 idle), 2 rows older (both idle)
        metrics = [
            {"idle": True,  "timestamp": _iso(_FIXED_NOW - timedelta(hours=1))},
            {"idle": False, "timestamp": _iso(_FIXED_NOW - timedelta(hours=23))},
            {"idle": True,  "timestamp": _iso(_FIXED_NOW - timedelta(hours=25))},
            {"idle": True,  "timestamp": _iso(_FIXED_NOW - timedelta(hours=30))},
        ]
        result = self._call(metrics)
        # recent (within 24h) = rows 0 and 1 → 1 idle / 2 = 50%
        self.assertAlmostEqual(result["last_24h_pct"], 50.0, places=1)
        self.assertAlmostEqual(result["all_time_pct"], 75.0, places=1)
        self.assertEqual(result["total_iterations"], 4)


# ---------------------------------------------------------------------------
# compute_estimation_metrics
# ---------------------------------------------------------------------------

class TestComputeEstimationMetrics(unittest.TestCase):
    def _call(self, discussions, min_samples=5):
        from backend.kpi_engine import compute_estimation_metrics
        with patch("backend.kpi_engine._now_utc", return_value=_FIXED_NOW):
            return compute_estimation_metrics(discussions, min_samples=min_samples)

    def test_empty_returns_zero_and_nones(self):
        """AC-18: empty list → tasks_with_estimates==0, accuracy/bias/complexity None."""
        result = self._call([])
        self.assertEqual(result["tasks_with_estimates"], 0)
        self.assertIsNone(result["accuracy"])
        self.assertIsNone(result["bias"])
        self.assertIsNone(result["complexity_velocity"])

    def test_below_min_samples_accuracy_none(self):
        """AC-16: fewer than min_samples → accuracy is None but counts populated."""
        discussions = [
            {
                "status": "DONE",
                "frontmatter": {"estimated_hours": 3.0},
                "completion": {"actual_hours": 4.0},
            },
            {
                "status": "DONE",
                "frontmatter": {"estimated_hours": 2.0},
                "completion": {"actual_hours": 2.0},
            },
        ]
        result = self._call(discussions, min_samples=5)
        self.assertEqual(result["tasks_with_estimates"], 2)
        self.assertEqual(result["total_measured"], 2)
        self.assertIsNone(result["accuracy"])

    def test_at_min_samples_accuracy_is_float_in_range(self):
        """AC-17: >= min_samples → accuracy is float in [0,1], bias has correct sign."""
        # actual > estimated → bias should be positive
        discussions = [
            {
                "status": "DONE",
                "frontmatter": {"estimated_hours": 2.0},
                "completion": {"actual_hours": 4.0},  # actual > estimated → positive bias
            },
            {
                "status": "DONE",
                "frontmatter": {"estimated_hours": 3.0},
                "completion": {"actual_hours": 5.0},  # actual > estimated → positive bias
            },
        ]
        result = self._call(discussions, min_samples=2)
        self.assertIsNotNone(result["accuracy"])
        self.assertGreaterEqual(result["accuracy"], 0.0)
        self.assertLessEqual(result["accuracy"], 1.0)
        # bias = mean(actual - estimated): (4-2)+(5-3)/2 = 2.0
        self.assertIsNotNone(result["bias"])
        self.assertGreater(result["bias"], 0.0, "actual > estimated → positive bias")

    def test_bias_sign_convention(self):
        """AC-17: actual < estimated → negative bias (over-estimated)."""
        discussions = [
            {
                "status": "DONE",
                "frontmatter": {"estimated_hours": 10.0},
                "completion": {"actual_hours": 2.0},  # under-delivered → negative bias
            },
        ]
        result = self._call(discussions, min_samples=1)
        self.assertIsNotNone(result["bias"])
        self.assertLess(result["bias"], 0.0, "actual < estimated → negative bias")

    def test_min_samples_field_reflected(self):
        """min_samples param is echoed back in the result dict."""
        result = self._call([], min_samples=7)
        self.assertEqual(result["min_samples"], 7)


# ---------------------------------------------------------------------------
# Suite-wide: no live state needed
# ---------------------------------------------------------------------------

class TestSuiteIsolation(unittest.TestCase):
    """AC-19 / AC-20: Suite runs without live registry or loop-metrics file."""

    def test_does_not_call_compute_all(self):
        """No test in this module invokes compute_all (just a structural check)."""
        # compute_all is not imported in this module; verify it's absent
        import backend.tests.test_kpi_engine as self_module
        self.assertFalse(
            hasattr(self_module, "compute_all"),
            "test module must not import compute_all",
        )

    def test_compute_velocity_no_registry_needed(self):
        """Passing an empty list runs without any file reads."""
        from backend.kpi_engine import compute_velocity
        with patch("backend.kpi_engine._now_utc", return_value=_FIXED_NOW):
            result = compute_velocity([])
        self.assertEqual(result["total_done"], 0)

    def test_compute_idle_rate_no_metrics_needed(self):
        """Passing an empty list runs without any file reads."""
        from backend.kpi_engine import compute_idle_rate
        with patch("backend.kpi_engine._now_utc", return_value=_FIXED_NOW):
            result = compute_idle_rate([])
        self.assertEqual(result["total_iterations"], 0)


if __name__ == "__main__":
    unittest.main()
