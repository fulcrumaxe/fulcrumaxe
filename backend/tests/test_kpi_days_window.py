"""
Tests that kpi.cycle_time and cost.by_discussion RPC handlers respect the
`days` parameter — different windows yield different filtered results.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# kpi_engine.cycle_time_histogram respects days param
# ---------------------------------------------------------------------------

class TestCycleTimeHistogramDaysWindow(unittest.TestCase):
    """cycle_time_histogram(days=N) filters out PRs closed before the cutoff."""

    def _make_discussions(self):
        now = datetime.now(tz=timezone.utc)
        return [
            # Closed 10 days ago — inside 30d window, outside 7d window
            {
                "status": "DONE",
                "number": 1,
                "created_at": (now - timedelta(hours=2, days=10)).isoformat(),
                "closed_at": (now - timedelta(days=10)).isoformat(),
            },
            # Closed 3 days ago — inside both 30d and 7d windows
            {
                "status": "DONE",
                "number": 2,
                "created_at": (now - timedelta(hours=4, days=3)).isoformat(),
                "closed_at": (now - timedelta(days=3)).isoformat(),
            },
        ]

    def test_wide_window_includes_more(self):
        from backend.kpi_engine import cycle_time_histogram
        discussions = self._make_discussions()
        with patch("backend.kpi_engine.load_registry", return_value=discussions):
            result_30 = cycle_time_histogram(days=30)
        total_30 = sum(r["count"] for r in result_30)
        self.assertEqual(total_30, 2, "30-day window should include both PRs")

    def test_narrow_window_excludes_old_entries(self):
        from backend.kpi_engine import cycle_time_histogram
        discussions = self._make_discussions()
        with patch("backend.kpi_engine.load_registry", return_value=discussions):
            result_7 = cycle_time_histogram(days=7)
        total_7 = sum(r["count"] for r in result_7)
        self.assertEqual(total_7, 1, "7-day window should include only the recent PR")

    def test_different_days_yield_different_counts(self):
        from backend.kpi_engine import cycle_time_histogram
        discussions = self._make_discussions()
        with patch("backend.kpi_engine.load_registry", return_value=discussions):
            result_30 = cycle_time_histogram(days=30)
            result_7 = cycle_time_histogram(days=7)
        total_30 = sum(r["count"] for r in result_30)
        total_7 = sum(r["count"] for r in result_7)
        self.assertGreater(total_30, total_7, "wider window must return more entries than narrow window")

    def test_invalid_days_raises(self):
        from backend.kpi_engine import cycle_time_histogram
        with self.assertRaises(ValueError):
            cycle_time_histogram(days=0)

    def test_default_days_is_90(self):
        """Calling without days uses 90-day window (backward compatible)."""
        from backend.kpi_engine import cycle_time_histogram
        now = datetime.now(tz=timezone.utc)
        # PR closed 60 days ago — inside 90d default, would be outside 30d
        discussions = [
            {
                "status": "DONE",
                "number": 99,
                "created_at": (now - timedelta(hours=1, days=60)).isoformat(),
                "closed_at": (now - timedelta(days=60)).isoformat(),
            }
        ]
        with patch("backend.kpi_engine.load_registry", return_value=discussions):
            result_default = cycle_time_histogram()
        total = sum(r["count"] for r in result_default)
        self.assertEqual(total, 1, "default 90-day window should include 60-day-old PR")


# ---------------------------------------------------------------------------
# RPC handler kpi.cycle_time passes days to kpi_engine
# ---------------------------------------------------------------------------

class TestRpcKpiCycleTimeDays(unittest.TestCase):
    def test_days_param_wired_through_rpc(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["kpi.cycle_time"]

        calls = []

        def fake_cth(days=90, repo_root=None):
            calls.append(days)
            return [
                {"bucket": "0-2h", "count": 0},
                {"bucket": "2-6h", "count": 0},
                {"bucket": "6-24h", "count": 0},
                {"bucket": "24h+", "count": 0},
            ]

        with patch("backend.kpi_engine.cycle_time_histogram", fake_cth):
            # Also patch the import inside the handler
            import backend.kpi_engine as _kpi_mod
            original = _kpi_mod.cycle_time_histogram
            _kpi_mod.cycle_time_histogram = fake_cth
            try:
                handler({"days": 7})
                handler({"days": 30})
            finally:
                _kpi_mod.cycle_time_histogram = original

        self.assertEqual(calls, [7, 30], "handler must pass days to cycle_time_histogram")

    def test_cycle_time_bad_days_raises_32602(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["kpi.cycle_time"]
        with self.assertRaises(Exception) as ctx:
            handler({"days": 0})
        self.assertEqual(getattr(ctx.exception, "rpc_code", None), -32602)

    def test_cycle_time_default_days_is_90(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["kpi.cycle_time"]
        calls = []

        import backend.kpi_engine as _kpi_mod

        def fake_cth(days=90, repo_root=None):
            calls.append(days)
            return [{"bucket": "0-2h", "count": 0}, {"bucket": "2-6h", "count": 0},
                    {"bucket": "6-24h", "count": 0}, {"bucket": "24h+", "count": 0}]

        original = _kpi_mod.cycle_time_histogram
        _kpi_mod.cycle_time_histogram = fake_cth
        try:
            handler({})
        finally:
            _kpi_mod.cycle_time_histogram = original

        self.assertEqual(calls, [90])


# ---------------------------------------------------------------------------
# RPC handler cost.by_discussion respects days window
# ---------------------------------------------------------------------------

class TestRpcCostByDiscussionDays(unittest.TestCase):
    """cost.by_discussion should filter agent records by finished timestamp."""

    def _make_mock_session_cost(self, now):
        """Return a mock get_session_cost() result with agents at different times."""
        return {
            "by_agent": [
                # discussion 1 — finished 5 days ago (inside 7d, inside 30d)
                {
                    "discussion": 1,
                    "input": 1000,
                    "output": 500,
                    "cost_usd": 0.05,
                    "finished": (now - timedelta(days=5)).isoformat().replace("+00:00", "Z"),
                },
                # discussion 2 — finished 20 days ago (outside 7d, inside 30d)
                {
                    "discussion": 2,
                    "input": 2000,
                    "output": 1000,
                    "cost_usd": 0.10,
                    "finished": (now - timedelta(days=20)).isoformat().replace("+00:00", "Z"),
                },
            ],
            "by_discussion": [],
        }

    def test_narrow_window_excludes_old_entries(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["cost.by_discussion"]
        now = datetime.now(tz=timezone.utc)
        mock_cost = self._make_mock_session_cost(now)

        with patch("backend.cost_tracker.CostTracker") as MockCT:
            MockCT.return_value.get_session_cost.return_value = mock_cost
            result = handler({"top": 10, "days": 7})

        disc_ids = [e["discussion"] for e in result]
        self.assertIn(1, disc_ids, "discussion 1 (5 days ago) should be in 7-day result")
        self.assertNotIn(2, disc_ids, "discussion 2 (20 days ago) should be excluded from 7-day result")

    def test_wide_window_includes_more(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["cost.by_discussion"]
        now = datetime.now(tz=timezone.utc)
        mock_cost = self._make_mock_session_cost(now)

        with patch("backend.cost_tracker.CostTracker") as MockCT:
            MockCT.return_value.get_session_cost.return_value = mock_cost
            result = handler({"top": 10, "days": 30})

        disc_ids = [e["discussion"] for e in result]
        self.assertIn(1, disc_ids)
        self.assertIn(2, disc_ids, "discussion 2 (20 days ago) should be in 30-day result")

    def test_different_days_yield_different_results(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["cost.by_discussion"]
        now = datetime.now(tz=timezone.utc)
        mock_cost = self._make_mock_session_cost(now)

        with patch("backend.cost_tracker.CostTracker") as MockCT:
            MockCT.return_value.get_session_cost.return_value = mock_cost
            result_7 = handler({"top": 10, "days": 7})
            result_30 = handler({"top": 10, "days": 30})

        self.assertNotEqual(
            len(result_7), len(result_30),
            "7-day and 30-day windows must return different counts when data spans the gap"
        )

    def test_bad_days_raises_32602(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["cost.by_discussion"]
        with self.assertRaises(Exception) as ctx:
            handler({"top": 10, "days": 0})
        self.assertEqual(getattr(ctx.exception, "rpc_code", None), -32602)

    def test_default_days_is_90(self):
        """Calling without days uses 90-day window."""
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["cost.by_discussion"]
        now = datetime.now(tz=timezone.utc)
        # agent finished 80 days ago — inside 90d default
        mock_cost = {
            "by_agent": [
                {
                    "discussion": 999,
                    "input": 100,
                    "output": 50,
                    "cost_usd": 0.01,
                    "finished": (now - timedelta(days=80)).isoformat().replace("+00:00", "Z"),
                }
            ],
            "by_discussion": [],
        }
        with patch("backend.cost_tracker.CostTracker") as MockCT:
            MockCT.return_value.get_session_cost.return_value = mock_cost
            result = handler({"top": 10})

        disc_ids = [e["discussion"] for e in result]
        self.assertIn(999, disc_ids, "default 90-day window should include 80-day-old entry")


if __name__ == "__main__":
    unittest.main()
