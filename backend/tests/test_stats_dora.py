"""Unit tests for backend/rpc/stats_dora.py

Coverage:
  (a) empty/no-data → applicable=False, no crash
  (b) synthetic snapshot → correct field passthrough
  (c) asserts compute_snapshot is called (monkeypatched) rather than
      recomputing DORA/KPI independently
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# (a) Empty / no-data case — applicable=False, no crash
# ---------------------------------------------------------------------------

class TestHandleEmpty(unittest.TestCase):
    def test_no_release_data_returns_applicable_false(self):
        """When compute_snapshot returns zero deploy_frequency and negative
        lead_time, handle() must return applicable=False without crashing."""
        empty_snap = {
            "date": "2099-01-01",
            "deploy_frequency_per_day": 0.0,
            "lead_time_minutes_p50": -1.0,
            "change_failure_rate_pct": "n/a",
            "velocity_last_24h": 0,
            "velocity_all_time_per_day": 0.0,
            "cycle_time_median_hours": None,
        }
        with patch("backend.analytics_engineer.compute_snapshot", return_value=empty_snap):
            from backend.rpc.stats_dora import handle
            result = handle({})

        self.assertFalse(result["applicable"])
        # Must still return the shape (passthrough fields present)
        self.assertIn("deploy_frequency_per_day", result)
        self.assertIn("change_failure_rate_pct", result)

    def test_exception_in_compute_snapshot_returns_applicable_false(self):
        """If compute_snapshot raises, handle() must catch and return applicable=False."""
        with patch("backend.analytics_engineer.compute_snapshot", side_effect=RuntimeError("oops")):
            from backend.rpc.stats_dora import handle
            result = handle({})

        self.assertFalse(result["applicable"])

    def test_no_crash_with_empty_params(self):
        """handle({}) must not raise — params is allowed to be empty."""
        empty_snap = {
            "date": "2099-01-01",
            "deploy_frequency_per_day": 0.0,
            "lead_time_minutes_p50": -1.0,
            "change_failure_rate_pct": "n/a",
            "velocity_last_24h": 0,
            "velocity_all_time_per_day": 0.0,
            "cycle_time_median_hours": None,
        }
        with patch("backend.analytics_engineer.compute_snapshot", return_value=empty_snap):
            from backend.rpc.stats_dora import handle
            result = handle({})  # must not raise

        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# (b) Synthetic snapshot → correct field passthrough
# ---------------------------------------------------------------------------

class TestHandlePassthrough(unittest.TestCase):
    def _make_snap(self, **overrides) -> dict:
        base = {
            "date": "2099-06-01",
            "deploy_frequency_per_day": 38.14,
            "lead_time_minutes_p50": 8.01,
            "change_failure_rate_pct": "n/a",
            "velocity_last_24h": 5,
            "velocity_all_time_per_day": 9.31,
            "cycle_time_median_hours": 0.42,
        }
        base.update(overrides)
        return base

    def test_deploy_frequency_passthrough(self):
        with patch("backend.analytics_engineer.compute_snapshot", return_value=self._make_snap()):
            from backend.rpc.stats_dora import handle
            result = handle({})
        self.assertAlmostEqual(result["deploy_frequency_per_day"], 38.14)

    def test_lead_time_passthrough(self):
        with patch("backend.analytics_engineer.compute_snapshot", return_value=self._make_snap()):
            from backend.rpc.stats_dora import handle
            result = handle({})
        self.assertAlmostEqual(result["lead_time_minutes_p50"], 8.01)

    def test_cfr_verbatim_na_string(self):
        """change_failure_rate_pct must be passed through verbatim as 'n/a'."""
        with patch("backend.analytics_engineer.compute_snapshot", return_value=self._make_snap()):
            from backend.rpc.stats_dora import handle
            result = handle({})
        self.assertEqual(result["change_failure_rate_pct"], "n/a")

    def test_cfr_verbatim_numeric_string(self):
        """change_failure_rate_pct must be passed through verbatim as a numeric string."""
        snap = self._make_snap(change_failure_rate_pct="3.7")
        with patch("backend.analytics_engineer.compute_snapshot", return_value=snap):
            from backend.rpc.stats_dora import handle
            result = handle({})
        self.assertEqual(result["change_failure_rate_pct"], "3.7")
        # Must NOT be coerced to a float
        self.assertIsInstance(result["change_failure_rate_pct"], str)

    def test_velocity_passthrough(self):
        with patch("backend.analytics_engineer.compute_snapshot", return_value=self._make_snap()):
            from backend.rpc.stats_dora import handle
            result = handle({})
        self.assertAlmostEqual(result["velocity_all_time_per_day"], 9.31)

    def test_cycle_time_passthrough(self):
        with patch("backend.analytics_engineer.compute_snapshot", return_value=self._make_snap()):
            from backend.rpc.stats_dora import handle
            result = handle({})
        self.assertAlmostEqual(result["cycle_time_median_hours"], 0.42)

    def test_applicable_true_when_deploy_freq_positive(self):
        with patch("backend.analytics_engineer.compute_snapshot", return_value=self._make_snap()):
            from backend.rpc.stats_dora import handle
            result = handle({})
        self.assertTrue(result["applicable"])

    def test_window_start_passthrough(self):
        with patch("backend.analytics_engineer.compute_snapshot", return_value=self._make_snap()):
            from backend.rpc.stats_dora import handle
            result = handle({})
        self.assertEqual(result["window_start"], "2099-06-01")

    def test_live_sample_values(self):
        """Replicate the PM's verified live sample values from the spec."""
        snap = self._make_snap(
            deploy_frequency_per_day=38.14,
            lead_time_minutes_p50=8.01,
            change_failure_rate_pct="n/a",
            velocity_all_time_per_day=9.31,
            cycle_time_median_hours=0.42,
        )
        with patch("backend.analytics_engineer.compute_snapshot", return_value=snap):
            from backend.rpc.stats_dora import handle
            result = handle({})

        self.assertTrue(result["applicable"])
        self.assertAlmostEqual(result["deploy_frequency_per_day"], 38.14, places=2)
        self.assertAlmostEqual(result["lead_time_minutes_p50"], 8.01, places=2)
        self.assertEqual(result["change_failure_rate_pct"], "n/a")
        self.assertAlmostEqual(result["velocity_all_time_per_day"], 9.31, places=2)
        self.assertAlmostEqual(result["cycle_time_median_hours"], 0.42, places=2)


# ---------------------------------------------------------------------------
# (c) Reuse assertion — handler calls compute_snapshot, not independent logic
# ---------------------------------------------------------------------------

class TestHandleReusesComputeSnapshot(unittest.TestCase):
    def test_calls_compute_snapshot(self):
        """handle() must call analytics_engineer.compute_snapshot exactly once."""
        snap = {
            "date": "2099-01-01",
            "deploy_frequency_per_day": 1.0,
            "lead_time_minutes_p50": 5.0,
            "change_failure_rate_pct": "n/a",
            "velocity_last_24h": 0,
            "velocity_all_time_per_day": 1.0,
            "cycle_time_median_hours": 1.0,
        }
        with patch("backend.analytics_engineer.compute_snapshot", return_value=snap) as mock_cs:
            from backend.rpc.stats_dora import handle
            handle({})

        mock_cs.assert_called_once()

    def test_surfaces_compute_snapshot_output(self):
        """handle() must surface what compute_snapshot returned, not recompute."""
        sentinel_snap = {
            "date": "2099-12-31",
            "deploy_frequency_per_day": 99.9,
            "lead_time_minutes_p50": 1.23,
            "change_failure_rate_pct": "42.0",
            "velocity_last_24h": 3,
            "velocity_all_time_per_day": 7.77,
            "cycle_time_median_hours": 0.11,
        }
        with patch("backend.analytics_engineer.compute_snapshot", return_value=sentinel_snap):
            from backend.rpc.stats_dora import handle
            result = handle({})

        # Every sentinel value must appear verbatim in the result
        self.assertAlmostEqual(result["deploy_frequency_per_day"], 99.9)
        self.assertAlmostEqual(result["lead_time_minutes_p50"], 1.23)
        self.assertEqual(result["change_failure_rate_pct"], "42.0")
        self.assertAlmostEqual(result["velocity_all_time_per_day"], 7.77)
        self.assertAlmostEqual(result["cycle_time_median_hours"], 0.11)


if __name__ == "__main__":
    unittest.main()
