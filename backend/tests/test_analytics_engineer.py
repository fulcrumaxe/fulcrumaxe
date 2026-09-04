"""
Tests for backend/analytics_engineer.py.

Coverage:
  (a) empty-releases: no crash, emits snapshot with n/a / zero values
  (b) change-failure-rate: synthetic release + bug discussion within 24h → CFR > 0
  (c) trailing-window cutoff: release outside 7d window is excluded
  (d) module reuse: compute_snapshot uses compute_dora_snapshot + kpi_engine, not independent logic
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# (a) Empty releases — no crash, snapshot emits
# ---------------------------------------------------------------------------

class TestEmptyReleases(unittest.TestCase):
    def test_snapshot_no_releases_no_crash(self):
        """compute_snapshot with no release files must not crash."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_releases = Path(tmpdir) / "releases"
            fake_releases.mkdir()

            with (
                patch("backend.analytics_engineer._RELEASES_DIR", fake_releases),
                patch("backend.analytics_engineer.compute_dora_snapshot") as mock_dora,
                patch("backend.analytics_engineer.load_registry", return_value=[]),
            ):
                mock_dora.return_value = {
                    "deploy_frequency_per_day": 0.0,
                    "lead_time_minutes_p50": -1.0,
                    "change_failure_rate_pct": -1.0,
                }
                from backend.analytics_engineer import compute_snapshot
                snap = compute_snapshot(today="2099-01-01")

        self.assertEqual(snap["date"], "2099-01-01")
        self.assertEqual(snap["deploy_frequency_per_day"], 0.0)
        # CFR should be "n/a" when there are no releases
        self.assertEqual(snap["change_failure_rate_pct"], "n/a")

    def test_emit_snapshot_creates_file(self):
        """emit_snapshot must create wiki/analytics/<date>.md."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        try:
            analytics_dir = Path(tmpdir) / "wiki" / "analytics"
            with patch("backend.analytics_engineer._ANALYTICS_DIR", analytics_dir):
                from backend.analytics_engineer import emit_snapshot
                snap = {
                    "date": "2099-01-01",
                    "deploy_frequency_per_day": 0.0,
                    "lead_time_minutes_p50": -1.0,
                    "change_failure_rate_pct": "n/a",
                    "velocity_last_24h": 0,
                    "velocity_all_time_per_day": 0.0,
                    "cycle_time_median_hours": None,
                }
                out = emit_snapshot(snap)

            self.assertTrue(out.exists())
            content = out.read_text()
            self.assertIn("2099-01-01", content)
            self.assertIn("Deploy frequency", content)
            self.assertIn("n/a", content)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b) Change failure rate from synthetic bug-filing data
# ---------------------------------------------------------------------------

class TestChangeFailureRate(unittest.TestCase):
    def _make_release(self, merged_offset_seconds: float = 0) -> dict:
        """Return a synthetic release record merged at now + offset."""
        now = datetime.now(timezone.utc)
        merged_at = now - timedelta(seconds=merged_offset_seconds)
        return {
            "id": "2099-01-01-001",
            "merged_at": merged_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def test_cfr_bug_within_24h_counts_as_failure(self):
        """A Bug discussion filed within 24h of a release → CFR > 0."""
        releases = [self._make_release(merged_offset_seconds=3600)]  # 1h ago

        # Bug discussion created 30 minutes after the release
        bug_created = datetime.now(timezone.utc) - timedelta(minutes=30)

        gh_response = {
            "data": {
                "repository": {
                    "discussions": {
                        "nodes": [
                            {
                                "title": "[Bug] something broke",
                                "createdAt": bug_created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            }
                        ]
                    }
                }
            }
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(gh_response)

        with patch("subprocess.run", return_value=mock_result):
            from backend.analytics_engineer import _compute_cfr
            cfr = _compute_cfr(releases)

        self.assertEqual(cfr, "100.0")  # 1/1 releases failed

    def test_cfr_bug_outside_24h_not_counted(self):
        """A Bug discussion filed >24h after a release does not count."""
        releases = [self._make_release(merged_offset_seconds=3 * 3600)]  # 3h ago

        # Bug discussion created 2 days before the release
        bug_created = datetime.now(timezone.utc) - timedelta(days=2)

        gh_response = {
            "data": {
                "repository": {
                    "discussions": {
                        "nodes": [
                            {
                                "title": "[Bug] old bug",
                                "createdAt": bug_created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            }
                        ]
                    }
                }
            }
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(gh_response)

        with patch("subprocess.run", return_value=mock_result):
            from backend.analytics_engineer import _compute_cfr
            cfr = _compute_cfr(releases)

        self.assertEqual(cfr, "0.0")  # bug was before release, not after

    def test_cfr_gh_cli_failure_returns_na(self):
        """If gh CLI fails, CFR gracefully returns 'n/a'."""
        releases = [self._make_release()]

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            from backend.analytics_engineer import _compute_cfr
            cfr = _compute_cfr(releases)

        self.assertEqual(cfr, "n/a")


# ---------------------------------------------------------------------------
# (c) Trailing-window cutoff — release outside 7d is excluded
# ---------------------------------------------------------------------------

class TestTrailingWindow(unittest.TestCase):
    def test_old_release_excluded(self):
        """A release older than 7 days must NOT appear in the window."""
        now = datetime.now(timezone.utc)
        old_merged = now - timedelta(days=8)

        fake_data = {
            "id": "old-release",
            "merged_at": old_merged.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            releases_dir = Path(tmpdir) / "releases"
            releases_dir.mkdir()
            (releases_dir / "old.json").write_text(json.dumps(fake_data))

            with patch("backend.analytics_engineer._RELEASES_DIR", releases_dir):
                from backend.analytics_engineer import _load_recent_releases
                cutoff_ts = now.timestamp() - 7 * 24 * 3600
                releases = _load_recent_releases(cutoff_ts)

        self.assertEqual(releases, [], "Release older than 7d must be excluded")

    def test_recent_release_included(self):
        """A release within the 7-day window is included."""
        now = datetime.now(timezone.utc)
        recent_merged = now - timedelta(days=3)

        fake_data = {
            "id": "recent-release",
            "merged_at": recent_merged.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            releases_dir = Path(tmpdir) / "releases"
            releases_dir.mkdir()
            (releases_dir / "recent.json").write_text(json.dumps(fake_data))

            with patch("backend.analytics_engineer._RELEASES_DIR", releases_dir):
                from backend.analytics_engineer import _load_recent_releases
                cutoff_ts = now.timestamp() - 7 * 24 * 3600
                releases = _load_recent_releases(cutoff_ts)

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["id"], "recent-release")


# ---------------------------------------------------------------------------
# (d) Module reuse — compute_snapshot delegates to compute_dora_snapshot + kpi_engine
# ---------------------------------------------------------------------------

class TestModuleReuse(unittest.TestCase):
    def test_compute_snapshot_calls_compute_dora_snapshot(self):
        """compute_snapshot must call compute_dora_snapshot (not recompute independently)."""
        with (
            patch("backend.analytics_engineer.compute_dora_snapshot") as mock_dora,
            patch("backend.analytics_engineer.load_registry", return_value=[]),
            patch("backend.analytics_engineer._RELEASES_DIR", Path("/nonexistent")),
        ):
            mock_dora.return_value = {
                "deploy_frequency_per_day": 3.14,
                "lead_time_minutes_p50": 42.0,
                "change_failure_rate_pct": -1.0,
            }
            from backend.analytics_engineer import compute_snapshot
            snap = compute_snapshot(today="2099-01-01")

        mock_dora.assert_called_once()
        self.assertEqual(snap["deploy_frequency_per_day"], 3.14)
        self.assertEqual(snap["lead_time_minutes_p50"], 42.0)

    def test_compute_snapshot_calls_kpi_engine_functions(self):
        """compute_snapshot must call load_registry + compute_velocity + compute_pr_cycle_time."""
        with (
            patch("backend.analytics_engineer.compute_dora_snapshot") as mock_dora,
            patch("backend.analytics_engineer.load_registry") as mock_reg,
            patch("backend.analytics_engineer.compute_velocity") as mock_vel,
            patch("backend.analytics_engineer.compute_pr_cycle_time") as mock_ct,
            patch("backend.analytics_engineer._RELEASES_DIR", Path("/nonexistent")),
        ):
            mock_dora.return_value = {
                "deploy_frequency_per_day": 0.0,
                "lead_time_minutes_p50": -1.0,
                "change_failure_rate_pct": -1.0,
            }
            mock_reg.return_value = []
            mock_vel.return_value = {"last_24h": 5, "all_time_per_day": 2.5, "total_done": 50}
            mock_ct.return_value = {"mean_hours": 3.0, "median_hours": 2.5, "total_measured": 10}

            from backend.analytics_engineer import compute_snapshot
            snap = compute_snapshot(today="2099-01-01")

        mock_reg.assert_called_once()
        mock_vel.assert_called_once()
        mock_ct.assert_called_once()
        self.assertEqual(snap["velocity_last_24h"], 5)
        self.assertEqual(snap["cycle_time_median_hours"], 2.5)

    def test_analytics_engineer_gate_registered(self):
        """gates.analytics_engineer must default to True in control_plane._DEFAULT_GATES."""
        from backend.control_plane import _DEFAULT_GATES
        assert _DEFAULT_GATES.get("analytics_engineer") is True, (
            "gates.analytics_engineer must default to True in _DEFAULT_GATES"
        )


if __name__ == "__main__":
    unittest.main()
