"""Tests for unified loop staleness predicate.

Both loop_health._status() and loop_controller._loop_status() must agree:
  - last_run 30 min ago → ok / alive
  - last_run 90 min ago → stale / stale
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# dashboard_tui/ is not present in every tree that runs this suite (an adopter
# clone legitimately has no TUI). Skip rather than raise at collection time: an
# uncaught ImportError here aborts the whole run for every other test file too.
pytest.importorskip(
    "dashboard_tui.loop_staleness",
    reason="dashboard_tui/ not present in this tree",
)

from dashboard_tui.loop_staleness import is_loop_stale, STALE_THRESHOLD_SECONDS  # noqa: E402
from dashboard_tui.screens.loop_health import _status as health_status  # noqa: E402
from dashboard_tui.screens.loop_controller import _loop_status as ctrl_status  # noqa: E402


def _ts_ago(minutes: int) -> str:
    """Return an ISO8601 UTC timestamp for N minutes ago."""
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Shared helper: is_loop_stale
# ---------------------------------------------------------------------------

class TestIsLoopStale:
    def test_recent_run_not_stale(self):
        # 5 minutes ago — well within the 30-min threshold
        assert is_loop_stale(_ts_ago(5)) is False

    def test_90_min_ago_is_stale(self):
        assert is_loop_stale(_ts_ago(90)) is True

    def test_none_not_stale(self):
        assert is_loop_stale(None) is False

    def test_empty_string_not_stale(self):
        assert is_loop_stale("") is False

    def test_unparseable_not_stale(self):
        assert is_loop_stale("not-a-timestamp") is False

    def test_threshold_boundary(self):
        # 10 min under threshold → not stale; 10 min over → stale.
        assert is_loop_stale(_ts_ago(STALE_THRESHOLD_SECONDS // 60 - 10)) is False
        assert is_loop_stale(_ts_ago(STALE_THRESHOLD_SECONDS // 60 + 10)) is True


# ---------------------------------------------------------------------------
# Loop Health screen: _status()
# ---------------------------------------------------------------------------

class TestLoopHealthStatus:
    def test_recent_run_returns_ok(self):
        # 5 minutes ago — well within the 30-min threshold
        entry = {"ts": _ts_ago(5)}
        assert health_status(entry) == "ok"

    def test_90_min_ago_returns_stale(self):
        entry = {"ts": _ts_ago(90)}
        assert health_status(entry) == "stale"

    def test_idle_flag_overrides_staleness(self):
        entry = {"ts": _ts_ago(90), "idle": True}
        assert health_status(entry) == "idle"

    def test_error_flag_overrides_staleness(self):
        entry = {"ts": _ts_ago(90), "error": True}
        assert health_status(entry) == "error"

    def test_timestamp_key_fallback(self):
        """Also accepts 'timestamp' and 'run_ts' keys."""
        assert health_status({"timestamp": _ts_ago(90)}) == "stale"
        assert health_status({"run_ts": _ts_ago(90)}) == "stale"
        assert health_status({"timestamp": _ts_ago(5)}) == "ok"

    def test_no_timestamp_returns_ok(self):
        """No timestamp → not stale (unknown, not penalised)."""
        assert health_status({}) == "ok"


# ---------------------------------------------------------------------------
# Loop Controller screen: _loop_status()
# ---------------------------------------------------------------------------

class TestLoopControllerStatus:
    def test_recent_run_returns_alive(self):
        # 5 minutes ago — well within the 30-min threshold
        entry = {"timestamp": _ts_ago(5)}
        assert ctrl_status([entry]) == "alive"

    def test_90_min_ago_returns_stale(self):
        entry = {"timestamp": _ts_ago(90)}
        assert ctrl_status([entry]) == "stale"

    def test_empty_iters_returns_no_data(self):
        assert ctrl_status([]) == "no data"

    def test_idle_flag_overrides_staleness(self):
        entry = {"timestamp": _ts_ago(90), "idle": True}
        assert ctrl_status([entry]) == "idle"

    def test_error_flag_overrides_staleness(self):
        entry = {"timestamp": _ts_ago(90), "error": True}
        assert ctrl_status([entry]) == "error"

    def test_ts_key_fallback(self):
        """Also accepts 'ts' key."""
        assert ctrl_status([{"ts": _ts_ago(90)}]) == "stale"
        assert ctrl_status([{"ts": _ts_ago(5)}]) == "alive"


# ---------------------------------------------------------------------------
# Agreement test: both screens must agree on the same data
# ---------------------------------------------------------------------------

class TestBothScreensAgree:
    def test_recent_run_both_say_ok_or_alive(self):
        # 5 minutes ago — well within the 30-min threshold
        ts = _ts_ago(5)
        h = health_status({"ts": ts})
        c = ctrl_status([{"ts": ts}])
        assert h == "ok", f"Loop Health returned {h!r} for 5 min ago (expected 'ok')"
        assert c == "alive", f"Loop Controller returned {c!r} for 5 min ago (expected 'alive')"

    def test_90_min_ago_both_say_stale(self):
        ts = _ts_ago(90)
        h = health_status({"ts": ts})
        c = ctrl_status([{"ts": ts}])
        assert h == "stale", f"Loop Health returned {h!r} for 90 min ago (expected 'stale')"
        assert c == "stale", f"Loop Controller returned {c!r} for 90 min ago (expected 'stale')"
