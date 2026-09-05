"""Tests for dashboard_tui/screens/loop_health.py helper functions."""

import pytest

# dashboard_tui/ is not present in every tree that runs this suite (an adopter
# clone legitimately has no TUI). Skip rather than raise at collection time: an
# uncaught ImportError here aborts the whole run for every other test file too.
pytest.importorskip(
    "dashboard_tui.screens.loop_health",
    reason="dashboard_tui/ not present in this tree",
)

from dashboard_tui.screens.loop_health import _spawned, _UNKNOWN  # noqa: E402


class TestSpawned:
    def test_zero_renders_as_zero(self):
        """agents_spawned=0 must show "0", not the unknown sentinel."""
        assert _spawned({"agents_spawned": 0}) == "0"

    def test_nonzero_renders_as_string(self):
        assert _spawned({"agents_spawned": 3}) == "3"

    def test_fallback_key_spawned(self):
        assert _spawned({"spawned": 0}) == "0"
        assert _spawned({"spawned": 5}) == "5"

    def test_fallback_key_spawn_count(self):
        assert _spawned({"spawn_count": 0}) == "0"
        assert _spawned({"spawn_count": 2}) == "2"

    def test_missing_key_returns_unknown(self):
        """No recognized key → returns the unknown sentinel."""
        assert _spawned({}) == _UNKNOWN
        assert _spawned({"other_key": 0}) == _UNKNOWN

    def test_first_matching_key_wins(self):
        """agents_spawned takes priority over spawned."""
        assert _spawned({"agents_spawned": 1, "spawned": 99}) == "1"

    def test_none_value_skipped(self):
        """An explicit None value should be skipped; next key tried."""
        assert _spawned({"agents_spawned": None, "spawned": 4}) == "4"

    def test_none_value_all_none_returns_unknown(self):
        """All keys present but all None → unknown."""
        assert _spawned({"agents_spawned": None, "spawned": None, "spawn_count": None}) == _UNKNOWN
