"""Tests for dashboard_tui/screens/stats.py worker group isolation.

Verifies that refresh_data() spawns KPI and classifier workers in separate
groups so they can't cancel each other (root cause of D#723).
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, call, patch

import pytest

# Every test in this file builds a dashboard_tui StatsScreen, so the whole
# module is moot in a tree without dashboard_tui/ (an adopter clone legitimately
# has no TUI). The import is inside the test bodies, so without this the absence
# shows up as 5 failures rather than an honest skip.
pytest.importorskip("dashboard_tui", reason="dashboard_tui/ not present in this tree")


class TestRefreshDataWorkerGroups:
    """Workers must use distinct group names to prevent mutual cancellation."""

    def _make_screen(self):
        """Build a StatsScreen with run_worker mocked out."""
        from dashboard_tui.screens.stats import StatsScreen

        screen = StatsScreen.__new__(StatsScreen)
        screen.run_worker = MagicMock()
        return screen

    def test_kpi_worker_uses_kpi_group(self):
        """stats-kpi-load must be in group='kpi'."""
        screen = self._make_screen()
        screen.refresh_data()
        calls = screen.run_worker.call_args_list
        kpi_call = next(
            (c for c in calls if c.kwargs.get("name") == "stats-kpi-load"),
            None,
        )
        assert kpi_call is not None, "stats-kpi-load worker not spawned"
        assert kpi_call.kwargs.get("group") == "kpi", (
            f"Expected group='kpi', got {kpi_call.kwargs.get('group')!r}"
        )

    def test_clf_worker_uses_clf_group(self):
        """stats-clf-load must be in group='clf'."""
        screen = self._make_screen()
        screen.refresh_data()
        calls = screen.run_worker.call_args_list
        clf_call = next(
            (c for c in calls if c.kwargs.get("name") == "stats-clf-load"),
            None,
        )
        assert clf_call is not None, "stats-clf-load worker not spawned"
        assert clf_call.kwargs.get("group") == "clf", (
            f"Expected group='clf', got {clf_call.kwargs.get('group')!r}"
        )

    def test_groups_are_distinct(self):
        """KPI and classifier workers must not share the same group."""
        screen = self._make_screen()
        screen.refresh_data()
        calls = screen.run_worker.call_args_list
        groups = [c.kwargs.get("group") for c in calls]
        # Both groups must be set and different
        assert len(groups) == 2
        assert groups[0] != groups[1], (
            f"Workers share the same group {groups[0]!r} — they will cancel each other"
        )

    def test_both_workers_spawned(self):
        """refresh_data must kick off exactly two workers."""
        screen = self._make_screen()
        screen.refresh_data()
        assert screen.run_worker.call_count == 2

    def test_both_workers_exclusive(self):
        """Both workers should still be exclusive within their own group."""
        screen = self._make_screen()
        screen.refresh_data()
        for c in screen.run_worker.call_args_list:
            assert c.kwargs.get("exclusive") is True, (
                f"Worker {c.kwargs.get('name')!r} missing exclusive=True"
            )
