"""
Tests for backend.config_watcher — polling, change detection, debounce,
malformed/missing file handling, callback invocation, compute_diff, and
the context manager API.

All tests use tmp_path (pytest) — the real .autonomous-team/config.json is
never touched. The background-thread tests set poll_interval=0.05s and use
short, bounded timeouts so they never hang.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.config_watcher import ConfigWatcher, _compact, compute_diff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _touch_mtime(path: Path) -> None:
    """Bump the file's mtime so the watcher detects a change."""
    content = path.read_text(encoding="utf-8")
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# compute_diff — pure function, no I/O
# ---------------------------------------------------------------------------


class TestComputeDiff:
    def test_empty_dicts(self):
        assert compute_diff({}, {}) == []

    def test_added_key(self):
        result = compute_diff({}, {"a": 1})
        assert len(result) == 1
        assert result[0] == {"key": "a", "change": "added", "old_value": None, "new_value": 1}

    def test_removed_key(self):
        result = compute_diff({"x": "y"}, {})
        assert len(result) == 1
        assert result[0]["change"] == "removed"
        assert result[0]["old_value"] == "y"
        assert result[0]["new_value"] is None

    def test_modified_key(self):
        result = compute_diff({"k": 1}, {"k": 2})
        assert len(result) == 1
        r = result[0]
        assert r["change"] == "modified"
        assert r["old_value"] == 1
        assert r["new_value"] == 2

    def test_unchanged_keys_not_reported(self):
        result = compute_diff({"same": True, "differ": 1}, {"same": True, "differ": 2})
        assert len(result) == 1
        assert result[0]["key"] == "differ"

    def test_multiple_changes_sorted_by_key(self):
        result = compute_diff({"b": 1, "c": 2}, {"a": 99, "b": 1})
        keys = [r["key"] for r in result]
        assert keys == sorted(keys), "results should be sorted by key"
        assert any(r["key"] == "a" and r["change"] == "added" for r in result)
        assert any(r["key"] == "c" and r["change"] == "removed" for r in result)

    def test_nested_values_treated_as_opaque(self):
        """Diffs are only at the top level — nested dicts are compared by equality."""
        old = {"gates": {"a": True, "b": False}}
        new = {"gates": {"a": True, "b": True}}
        result = compute_diff(old, new)
        assert len(result) == 1
        assert result[0]["change"] == "modified"


# ---------------------------------------------------------------------------
# _compact helper
# ---------------------------------------------------------------------------


class TestCompact:
    def test_short_value(self):
        assert _compact(42) == "42"

    def test_none(self):
        assert _compact(None) == "null"

    def test_long_value_truncated(self):
        big = {"k": "x" * 200}
        result = _compact(big)
        assert len(result) <= 80
        assert result.endswith("...")

    def test_exactly_80_chars_not_truncated(self):
        # Build a value that serialises to exactly 80 chars.
        s = "a" * 78  # json.dumps adds two quote chars → 80
        result = _compact(s)
        assert len(result) == 80
        assert not result.endswith("...")


# ---------------------------------------------------------------------------
# ConfigWatcher.check_once — single-iteration API, no background thread
# ---------------------------------------------------------------------------


class TestCheckOnce:
    def test_missing_file_returns_false(self, tmp_path: Path):
        watcher = ConfigWatcher(tmp_path / "nonexistent.json")
        assert watcher.check_once() is False

    def test_first_check_seeds_state_no_callback(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {"a": 1})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        result = watcher.check_once()
        assert result is False, "first check should seed state without firing callbacks"
        cb.assert_not_called()

    def test_unchanged_file_no_callback(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {"a": 1})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        watcher.check_once()  # seed
        result = watcher.check_once()
        assert result is False
        cb.assert_not_called()

    def test_changed_file_fires_callback(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {"a": 1})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        watcher.check_once()  # seed

        # Ensure mtime changes by writing a new file.
        time.sleep(0.01)
        _write(cfg, {"a": 2})

        # Skip debounce: set last_change_time far in the past.
        watcher._last_change_time = 0.0

        result = watcher.check_once()
        assert result is True
        cb.assert_called_once()
        old, new = cb.call_args[0]
        assert old == {"a": 1}
        assert new == {"a": 2}

    def test_callback_receives_correct_old_and_new(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        initial = {"gates": {"x": True}, "version": 1}
        _write(cfg, initial)
        received = []
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(lambda o, n: received.append((o, n)))
        watcher.check_once()  # seed

        time.sleep(0.01)
        updated = {"gates": {"x": False}, "version": 2}
        _write(cfg, updated)
        watcher._last_change_time = 0.0
        watcher.check_once()

        assert len(received) == 1
        old, new = received[0]
        assert old == initial
        assert new == updated

    def test_multiple_callbacks_all_invoked(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {"v": 1})
        cb1, cb2, cb3 = MagicMock(), MagicMock(), MagicMock()
        watcher = ConfigWatcher(cfg)
        for cb in (cb1, cb2, cb3):
            watcher.register_callback(cb)
        watcher.check_once()

        time.sleep(0.01)
        _write(cfg, {"v": 2})
        watcher._last_change_time = 0.0
        watcher.check_once()

        for cb in (cb1, cb2, cb3):
            cb.assert_called_once()

    def test_malformed_json_no_callback(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {"ok": True})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        watcher.check_once()  # seed

        time.sleep(0.01)
        cfg.write_text("{not valid json!!!", encoding="utf-8")
        watcher._last_change_time = 0.0
        result = watcher.check_once()

        assert result is False, "malformed JSON should not trigger callbacks"
        cb.assert_not_called()

    def test_malformed_json_mtime_updated_no_re_fire(self, tmp_path: Path):
        """After a malformed write, mtime is stored so the same write isn't re-fired."""
        cfg = tmp_path / "config.json"
        _write(cfg, {"ok": True})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        watcher.check_once()  # seed

        time.sleep(0.01)
        cfg.write_text("{bad json", encoding="utf-8")
        watcher._last_change_time = 0.0
        watcher.check_once()  # should notice + skip
        watcher.check_once()  # same mtime — should not re-attempt

        cb.assert_not_called()

    def test_deleted_file_after_first_check(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {"x": 1})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        watcher.check_once()  # seed — file exists
        cfg.unlink()
        result = watcher.check_once()
        assert result is False
        cb.assert_not_called()

    def test_callback_exception_does_not_stop_others(self, tmp_path: Path):
        """A raising callback must not prevent subsequent callbacks from running."""
        cfg = tmp_path / "config.json"
        _write(cfg, {"v": 0})
        good = MagicMock()
        bad = MagicMock(side_effect=RuntimeError("boom"))
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(bad)
        watcher.register_callback(good)
        watcher.check_once()

        time.sleep(0.01)
        _write(cfg, {"v": 1})
        watcher._last_change_time = 0.0
        watcher.check_once()

        good.assert_called_once()


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


class TestDebounce:
    def test_rapid_changes_within_debounce_window_not_double_fired(self, tmp_path: Path):
        """Two mtime changes within _DEBOUNCE_SECONDS should not fire callback twice."""
        cfg = tmp_path / "config.json"
        _write(cfg, {"v": 1})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        watcher.check_once()  # seed

        # First change — fires callback.
        time.sleep(0.01)
        _write(cfg, {"v": 2})
        watcher._last_change_time = 0.0
        watcher.check_once()
        assert cb.call_count == 1

        # Second change immediately after — within debounce window.
        time.sleep(0.01)
        _write(cfg, {"v": 3})
        # last_change_time was just set by the previous check; debounce window still open
        watcher.check_once()

        # Only one callback should have fired so far.
        assert cb.call_count == 1

    def test_change_after_debounce_window_fires_again(self, tmp_path: Path):
        """A change beyond the debounce window SHOULD fire the callback."""
        from backend.config_watcher import _DEBOUNCE_SECONDS

        cfg = tmp_path / "config.json"
        _write(cfg, {"v": 1})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        watcher.check_once()  # seed

        # First change.
        time.sleep(0.01)
        _write(cfg, {"v": 2})
        watcher._last_change_time = 0.0
        watcher.check_once()
        assert cb.call_count == 1

        # Simulate debounce window having elapsed.
        watcher._last_change_time = time.monotonic() - (_DEBOUNCE_SECONDS + 0.1)

        time.sleep(0.01)
        _write(cfg, {"v": 3})
        watcher.check_once()
        assert cb.call_count == 2


# ---------------------------------------------------------------------------
# Background thread / context manager
# ---------------------------------------------------------------------------


class TestBackgroundThread:
    def test_context_manager_starts_and_stops_thread(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {"running": True})
        watcher = ConfigWatcher(cfg, poll_interval=0.05)
        with watcher:
            assert watcher._thread is not None
            assert watcher._thread.is_alive()
        assert watcher._thread is None

    def test_start_idempotent(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {})
        watcher = ConfigWatcher(cfg, poll_interval=0.05)
        watcher.start()
        thread_id = id(watcher._thread)
        watcher.start()  # second call — must not spawn a second thread
        assert id(watcher._thread) == thread_id
        watcher.stop()

    def test_background_thread_detects_change(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {"phase": "before"})
        received = []
        done = threading.Event()

        def on_change(old, new):
            received.append((old, new))
            done.set()

        watcher = ConfigWatcher(cfg, poll_interval=0.05)
        watcher.register_callback(on_change)
        with watcher:
            # Allow the first seed check to happen.
            time.sleep(0.1)
            # Trigger a real change.
            _write(cfg, {"phase": "after"})
            assert done.wait(timeout=2.0), "callback never fired within 2 s"

        assert len(received) == 1
        assert received[0][1] == {"phase": "after"}

    def test_stop_does_not_hang(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        _write(cfg, {})
        watcher = ConfigWatcher(cfg, poll_interval=0.05)
        watcher.start()
        t0 = time.monotonic()
        watcher.stop()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"stop() took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Gate change events via event_bus (best-effort, exception-safe)
# ---------------------------------------------------------------------------


class TestGateChangeEvents:
    def test_gate_change_publishes_event(self, tmp_path: Path):
        """When gates change, a GateChangeEvent should be published to the bus."""
        cfg = tmp_path / "config.json"
        _write(cfg, {"gates": {"lint_must_pass": True}})
        watcher = ConfigWatcher(cfg)
        watcher.check_once()  # seed

        published = []

        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_bus.publish_async.side_effect = lambda e: published.append(e)
            mock_get_bus.return_value = mock_bus

            time.sleep(0.01)
            _write(cfg, {"gates": {"lint_must_pass": False}})
            watcher._last_change_time = 0.0
            watcher.check_once()

        assert len(published) == 1
        evt = published[0]
        assert evt.gate_name == "lint_must_pass"
        assert evt.old_value is True
        assert evt.new_value is False

    def test_event_bus_exception_does_not_abort_callbacks(self, tmp_path: Path):
        """If the event bus raises, user callbacks must still fire."""
        cfg = tmp_path / "config.json"
        _write(cfg, {"gates": {"x": True}})
        cb = MagicMock()
        watcher = ConfigWatcher(cfg)
        watcher.register_callback(cb)
        watcher.check_once()

        with patch("backend.event_bus.get_bus", side_effect=RuntimeError("bus unavailable")):
            time.sleep(0.01)
            _write(cfg, {"gates": {"x": False}})
            watcher._last_change_time = 0.0
            watcher.check_once()

        cb.assert_called_once()

    def test_no_gate_changes_no_events(self, tmp_path: Path):
        """Changing non-gate keys should not publish gate events."""
        cfg = tmp_path / "config.json"
        _write(cfg, {"gates": {"a": True}, "other": 1})
        watcher = ConfigWatcher(cfg)
        watcher.check_once()

        published = []
        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_bus.publish_async.side_effect = lambda e: published.append(e)
            mock_get_bus.return_value = mock_bus

            time.sleep(0.01)
            _write(cfg, {"gates": {"a": True}, "other": 2})
            watcher._last_change_time = 0.0
            watcher.check_once()

        # "other" changed, but gates didn't — no gate events.
        assert published == []
