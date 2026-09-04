"""
Unit tests for the three JSON-RPC KPI methods added to server.py:

  kpi.history        — merged-PRs-per-day list
  kpi.cycle_time     — cycle-time histogram
  cost.by_discussion — top-N discussions by spend

Tests exercise both the kpi_engine functions directly and the server.py
RPC dispatch layer (including -32602 on bad inputs).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Allow imports from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# kpi_engine tests
# ---------------------------------------------------------------------------

class TestKpiEngineHistory(unittest.TestCase):
    def test_raises_on_zero_days(self):
        from backend.kpi_engine import history
        with self.assertRaises(ValueError):
            history(0)

    def test_raises_on_negative_days(self):
        from backend.kpi_engine import history
        with self.assertRaises(ValueError):
            history(-5)

    def test_returns_list(self):
        from backend.kpi_engine import history
        # history() now uses --pretty=format:%cd\t%s and counts only squash-merge
        # commits whose subject matches "#N: title" or ends with "(#N)".
        fake_output = (
            "2026-05-01\t#1: add feature\n"
            "2026-05-01\t#2: fix bug (#99)\n"
            "2026-05-02\t#3: refactor\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = fake_output
            result = history(7)
        self.assertIsInstance(result, list)
        self.assertTrue(all("date" in r and "count" in r for r in result))

    def test_aggregates_counts(self):
        from backend.kpi_engine import history
        # Each line is "date\tsubject"; only subjects matching _PR_SUBJECT are counted.
        fake_output = (
            "2026-05-01\t#10: implement dashboard fix\n"
            "2026-05-01\tsquash merge of fix (#11)\n"
            "2026-05-02\t#12: cleanup\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = fake_output
            result = history(7)
        by_date = {r["date"]: r["count"] for r in result}
        self.assertEqual(by_date.get("2026-05-01"), 2)
        self.assertEqual(by_date.get("2026-05-02"), 1)

    def test_empty_git_output(self):
        from backend.kpi_engine import history
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            result = history(30)
        self.assertEqual(result, [])


class TestKpiEngineCycleTime(unittest.TestCase):
    def test_returns_four_buckets(self):
        from backend.kpi_engine import cycle_time_histogram
        with patch("backend.kpi_engine.load_registry", return_value=[]):
            result = cycle_time_histogram()
        self.assertEqual(len(result), 4)
        buckets = [r["bucket"] for r in result]
        self.assertEqual(buckets, ["0-2h", "2-6h", "6-24h", "24h+"])

    def test_correct_bucketing(self):
        from backend.kpi_engine import cycle_time_histogram
        from datetime import datetime, timezone, timedelta

        now = datetime.now(tz=timezone.utc)
        discussions = [
            # 1h cycle → 0-2h
            {
                "status": "DONE",
                "created_at": (now - timedelta(hours=1, days=1)).isoformat(),
                "closed_at": (now - timedelta(days=1)).isoformat(),
                "number": 1,
            },
            # 4h cycle → 2-6h
            {
                "status": "DONE",
                "created_at": (now - timedelta(hours=4, days=1)).isoformat(),
                "closed_at": (now - timedelta(days=1)).isoformat(),
                "number": 2,
            },
            # 12h cycle → 6-24h
            {
                "status": "DONE",
                "created_at": (now - timedelta(hours=12, days=1)).isoformat(),
                "closed_at": (now - timedelta(days=1)).isoformat(),
                "number": 3,
            },
            # 30h cycle → 24h+
            {
                "status": "DONE",
                "created_at": (now - timedelta(hours=30, days=2)).isoformat(),
                "closed_at": (now - timedelta(days=2)).isoformat(),
                "number": 4,
            },
        ]
        with patch("backend.kpi_engine.load_registry", return_value=discussions):
            result = cycle_time_histogram()
        by_bucket = {r["bucket"]: r["count"] for r in result}
        self.assertEqual(by_bucket["0-2h"], 1)
        self.assertEqual(by_bucket["2-6h"], 1)
        self.assertEqual(by_bucket["6-24h"], 1)
        self.assertEqual(by_bucket["24h+"], 1)


# ---------------------------------------------------------------------------
# server.py RPC dispatch tests
# ---------------------------------------------------------------------------

class TestServerRpcDispatch(unittest.TestCase):
    """Test that RPC handlers are registered and validate params correctly."""

    def test_kpi_history_registered(self):
        from backend.server import _RPC_METHODS
        self.assertIn("kpi.history", _RPC_METHODS)

    def test_kpi_cycle_time_registered(self):
        from backend.server import _RPC_METHODS
        self.assertIn("kpi.cycle_time", _RPC_METHODS)

    def test_cost_by_discussion_registered(self):
        from backend.server import _RPC_METHODS
        self.assertIn("cost.by_discussion", _RPC_METHODS)

    def test_kpi_history_bad_days_raises_32602(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["kpi.history"]
        with self.assertRaises(Exception) as ctx:
            handler({"days": 0})
        exc = ctx.exception
        self.assertEqual(getattr(exc, "rpc_code", None), -32602)

    def test_kpi_history_negative_days_raises_32602(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["kpi.history"]
        with self.assertRaises(Exception) as ctx:
            handler({"days": -1})
        exc = ctx.exception
        self.assertEqual(getattr(exc, "rpc_code", None), -32602)

    def test_cost_by_discussion_bad_top_raises_32602(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["cost.by_discussion"]
        with self.assertRaises(Exception) as ctx:
            handler({"top": 0})
        exc = ctx.exception
        self.assertEqual(getattr(exc, "rpc_code", None), -32602)

    def test_kpi_history_uses_fixture_when_env_set(self):
        from backend.server import _RPC_METHODS
        handler = _RPC_METHODS["kpi.history"]

        fixture = {"kpi_history": [{"date": "2026-01-01", "count": 99}]}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as fh:
            json.dump(fixture, fh)
            fh_path = Path(fh.name)

        try:
            with patch.dict(os.environ, {"AF_E2E_FIXTURES": "1"}):
                # Patch the fixture path that server.py computes
                fixture_path_attr = REPO_ROOT / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
                # We can't easily patch a local Path object, so write the fixture there
                fixture_path_attr.parent.mkdir(parents=True, exist_ok=True)
                original_content = fixture_path_attr.read_text() if fixture_path_attr.exists() else None
                fixture_path_attr.write_text(json.dumps(fixture))
                try:
                    result = handler({"days": 7})
                finally:
                    if original_content is not None:
                        fixture_path_attr.write_text(original_content)
                    elif fixture_path_attr.exists():
                        fixture_path_attr.unlink()
            self.assertEqual(result, fixture["kpi_history"])
        finally:
            fh_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
