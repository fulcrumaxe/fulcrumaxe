"""
Tests for backend/config_watcher.py — ConfigWatcher and compute_diff.

Covers: detect change, no change, callback invocation, multiple callbacks,
missing file, malformed JSON, diff computation, and start/stop lifecycle.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from backend.config_watcher import ConfigWatcher, compute_diff


def _bump_mtime(p: Path) -> None:
    """Advance p's mtime by 1 second so the watcher sees a change."""
    stat = os.stat(p)
    new_time = stat.st_mtime + 1.0
    os.utime(p, (new_time, new_time))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """Return a path to a temp config.json with initial content."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gates": {"auto_merge": True}, "version": "1.0"}), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# check_once — core detection logic
# ---------------------------------------------------------------------------


def test_check_once_returns_false_on_first_call(config_file: Path) -> None:
    """First call seeds state — no change reported even if file exists."""
    watcher = ConfigWatcher(config_file)
    assert watcher.check_once() is False


def test_check_once_returns_false_when_unchanged(config_file: Path) -> None:
    """Returns False when mtime has not changed since last check."""
    watcher = ConfigWatcher(config_file)
    watcher.check_once()  # seed
    assert watcher.check_once() is False


def test_check_once_returns_true_on_mtime_change(config_file: Path) -> None:
    """Returns True when the file's mtime changes."""
    watcher = ConfigWatcher(config_file)
    watcher.check_once()  # seed

    # Write new content then explicitly bump mtime (fast writes share the same mtime_ns).
    config_file.write_text(json.dumps({"gates": {"auto_merge": False}, "version": "1.1"}), encoding="utf-8")
    _bump_mtime(config_file)
    assert watcher.check_once() is True


def test_check_once_false_after_second_identical_write(config_file: Path) -> None:
    """After detecting a change, a subsequent call with no further change returns False."""
    watcher = ConfigWatcher(config_file)
    watcher.check_once()  # seed

    config_file.write_text(json.dumps({"gates": {"auto_merge": False}}), encoding="utf-8")
    _bump_mtime(config_file)
    assert watcher.check_once() is True  # first detection
    assert watcher.check_once() is False  # same mtime


# ---------------------------------------------------------------------------
# Callback invocation
# ---------------------------------------------------------------------------


def test_callback_called_with_old_and_new(config_file: Path) -> None:
    """Callback receives (old_config, new_config) when change is detected."""
    events: list[tuple[dict, dict]] = []

    watcher = ConfigWatcher(config_file)
    watcher.register_callback(lambda old, new: events.append((old, new)))

    watcher.check_once()  # seed

    new_cfg = {"gates": {"auto_merge": False}, "version": "2.0"}
    config_file.write_text(json.dumps(new_cfg), encoding="utf-8")
    _bump_mtime(config_file)
    watcher.check_once()

    assert len(events) == 1
    old, new = events[0]
    assert old["version"] == "1.0"
    assert new["version"] == "2.0"


def test_multiple_callbacks_all_called(config_file: Path) -> None:
    """All registered callbacks are invoked on a single change event."""
    calls: list[int] = []

    watcher = ConfigWatcher(config_file)
    watcher.register_callback(lambda old, new: calls.append(1))
    watcher.register_callback(lambda old, new: calls.append(2))
    watcher.register_callback(lambda old, new: calls.append(3))

    watcher.check_once()  # seed

    config_file.write_text(json.dumps({"version": "99"}), encoding="utf-8")
    _bump_mtime(config_file)
    watcher.check_once()

    assert calls == [1, 2, 3]


# ---------------------------------------------------------------------------
# Edge cases — missing file and malformed JSON
# ---------------------------------------------------------------------------


def test_missing_file_does_not_raise(tmp_path: Path) -> None:
    """ConfigWatcher does not raise when the config file does not exist."""
    watcher = ConfigWatcher(tmp_path / "nonexistent.json")
    assert watcher.check_once() is False  # no exception


def test_missing_file_then_appears(tmp_path: Path) -> None:
    """Watcher begins tracking once the file appears after being absent."""
    p = tmp_path / "config.json"
    watcher = ConfigWatcher(p)

    watcher.check_once()  # file absent — seeds None mtime
    p.write_text(json.dumps({"x": 1}), encoding="utf-8")
    # First call after file appears seeds state (returns False).
    assert watcher.check_once() is False


def test_malformed_json_does_not_crash(config_file: Path) -> None:
    """Watcher keeps the old config when JSON is malformed; no exception."""
    watcher = ConfigWatcher(config_file)
    watcher.check_once()  # seed

    config_file.write_bytes(b"{ not valid json !!!")
    watcher._last_change_time = 0.0
    # Should not raise and should return False (change detected but parsing failed)
    result = watcher.check_once()
    assert result is False  # malformed → skips callbacks, returns False


# ---------------------------------------------------------------------------
# compute_diff
# ---------------------------------------------------------------------------


def test_compute_diff_added_key() -> None:
    diff = compute_diff({}, {"new_key": True})
    assert len(diff) == 1
    assert diff[0]["change"] == "added"
    assert diff[0]["key"] == "new_key"


def test_compute_diff_removed_key() -> None:
    diff = compute_diff({"old_key": 42}, {})
    assert len(diff) == 1
    assert diff[0]["change"] == "removed"
    assert diff[0]["old_value"] == 42


def test_compute_diff_modified_key() -> None:
    diff = compute_diff({"x": True}, {"x": False})
    assert len(diff) == 1
    assert diff[0]["change"] == "modified"
    assert diff[0]["old_value"] is True
    assert diff[0]["new_value"] is False


def test_compute_diff_no_changes() -> None:
    diff = compute_diff({"a": 1, "b": 2}, {"a": 1, "b": 2})
    assert diff == []


# ---------------------------------------------------------------------------
# Start/stop lifecycle
# ---------------------------------------------------------------------------


def test_start_stop_lifecycle(config_file: Path) -> None:
    """Background thread starts and stops cleanly."""
    watcher = ConfigWatcher(config_file, poll_interval=0.1)
    watcher.start()
    assert watcher._thread is not None
    assert watcher._thread.is_alive()
    watcher.stop()
    assert watcher._thread is None or not watcher._thread.is_alive()


def test_context_manager_starts_and_stops(config_file: Path) -> None:
    """Context manager protocol starts on enter and stops on exit."""
    watcher = ConfigWatcher(config_file, poll_interval=0.1)
    with watcher:
        assert watcher._thread is not None and watcher._thread.is_alive()
    # After exit, thread should be stopped.
    assert watcher._thread is None or not watcher._thread.is_alive()


def test_background_thread_detects_change(config_file: Path) -> None:
    """Background polling thread eventually detects a real file change."""
    events: list[tuple[dict, dict]] = []
    watcher = ConfigWatcher(config_file, poll_interval=0.05)
    watcher.register_callback(lambda old, new: events.append((old, new)))

    with watcher:
        time.sleep(0.15)  # let the thread seed its state

        # Write new config then bump mtime so the watcher sees a change.
        config_file.write_text(json.dumps({"gates": {"auto_merge": False}}), encoding="utf-8")
        _bump_mtime(config_file)

        # Wait up to 1 second for the callback to fire.
        deadline = time.monotonic() + 1.0
        while not events and time.monotonic() < deadline:
            time.sleep(0.05)

    assert len(events) >= 1
