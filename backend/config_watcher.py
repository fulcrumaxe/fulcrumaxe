"""
Config file watcher — polls config.json for mtime changes and calls registered callbacks.

Uses os.stat() polling (no inotify dependency) to detect changes to a JSON config file.
When a change is detected, logs a structured diff and calls all registered callbacks with
(old_config, new_config).

Usage:
    from backend.config_watcher import ConfigWatcher
    from pathlib import Path

    watcher = ConfigWatcher(Path(".autonomous-team/config.json"))
    watcher.register_callback(lambda old, new: print("changed!"))
    with watcher:
        # background thread polls every 5 seconds
        ...

CLI:
    python backend/config_watcher.py watch                # watch and print changes
    python backend/config_watcher.py diff path1 path2     # diff two config files
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Minimum seconds between consecutive change callbacks (debounce).
_DEBOUNCE_SECONDS = 1.0


class ConfigWatcher:
    """Poll a JSON config file for mtime changes and call registered callbacks."""

    def __init__(self, config_path: Path, poll_interval: float = 5.0) -> None:
        self._path = Path(config_path)
        self._poll_interval = poll_interval
        self._callbacks: list[Callable[[dict, dict], None]] = []
        self._last_mtime_ns: int | None = None
        self._last_config: dict = {}
        self._last_change_time: float = 0.0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_callback(self, fn: Callable[[dict, dict], None]) -> None:
        """Register *fn* to be called with (old_config, new_config) on change."""
        self._callbacks.append(fn)

    def start(self) -> None:
        """Start the background daemon polling thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="config-watcher")
        self._thread.start()
        logger.debug("config_watcher: started polling %s every %ss", self._path, self._poll_interval)

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 1)
            self._thread = None
        logger.debug("config_watcher: stopped")

    def check_once(self) -> bool:
        """
        Manually check for changes. Returns True if the config changed.

        Useful for testing without a background thread.
        """
        return self._check_for_change()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ConfigWatcher":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background thread: poll until stop_event is set."""
        while not self._stop_event.is_set():
            try:
                self._check_for_change()
            except Exception:  # noqa: BLE001
                pass
            self._stop_event.wait(timeout=self._poll_interval)

    def _check_for_change(self) -> bool:
        """
        Check whether the file's mtime has changed since last call.

        Returns True if a change was detected and processed, False otherwise.
        Handles missing file, deleted file, and malformed JSON gracefully.
        """
        # --- File existence check ---
        try:
            stat = os.stat(self._path)
        except FileNotFoundError:
            if self._last_mtime_ns is not None:
                # File was present before; now deleted.
                logger.warning("config_watcher: %s was deleted — continuing to poll", self._path)
                self._last_mtime_ns = None
            else:
                logger.debug("config_watcher: %s not found yet, waiting", self._path)
            return False
        except OSError as exc:
            logger.warning("config_watcher: stat failed: %s", exc)
            return False

        current_mtime_ns = stat.st_mtime_ns

        # First check — seed state without triggering callbacks.
        if self._last_mtime_ns is None:
            self._last_mtime_ns = current_mtime_ns
            new_config = self._read_json()
            if new_config is not None:
                self._last_config = new_config
            return False

        if current_mtime_ns == self._last_mtime_ns:
            return False

        # mtime changed — debounce rapid successive changes.
        now = time.monotonic()
        if now - self._last_change_time < _DEBOUNCE_SECONDS:
            # Update mtime so we don't re-fire on next poll for the same change,
            # but skip callbacks this cycle.
            self._last_mtime_ns = current_mtime_ns
            return False

        # Read and parse the new content.
        new_config = self._read_json()
        if new_config is None:
            # Malformed JSON — keep old config, update mtime to avoid re-firing.
            self._last_mtime_ns = current_mtime_ns
            return False

        old_config = self._last_config
        self._last_mtime_ns = current_mtime_ns
        self._last_config = new_config
        self._last_change_time = now

        # Log structured diff.
        self._log_diff(old_config, new_config)

        # Publish gate change events to the bus (best-effort).
        try:
            from backend.event_bus import GateChangeEvent, get_bus  # noqa: PLC0415
            old_gates = old_config.get("gates", {})
            new_gates = new_config.get("gates", {})
            for gate_key in sorted(set(old_gates) | set(new_gates)):
                old_val = old_gates.get(gate_key)
                new_val = new_gates.get(gate_key)
                if old_val != new_val:
                    get_bus().publish_async(GateChangeEvent(
                        source="config_watcher",
                        gate_name=gate_key,
                        old_value=bool(old_val),
                        new_value=bool(new_val),
                    ))
        except Exception:  # noqa: BLE001
            pass

        # Call all registered callbacks.
        for cb in self._callbacks:
            try:
                cb(old_config, new_config)
            except Exception as exc:  # noqa: BLE001
                logger.warning("config_watcher: callback %r raised: %s", cb, exc)

        return True

    def _read_json(self) -> dict | None:
        """Read and parse the config file. Returns None on error."""
        try:
            raw = self._path.read_text(encoding="utf-8")
            return json.loads(raw)
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("config_watcher: cannot read %s: %s", self._path, exc)
            return None
        except json.JSONDecodeError as exc:
            logger.warning("config_watcher: malformed JSON in %s: %s", self._path, exc)
            return None

    @staticmethod
    def _log_diff(old: dict, new: dict) -> None:
        """Log a human-readable diff of top-level key changes."""
        all_keys = set(old) | set(new)
        changes: list[str] = []

        for key in sorted(all_keys):
            if key not in old:
                changes.append(f"{key}: (added) -> {_compact(new[key])}")
            elif key not in new:
                changes.append(f"{key}: {_compact(old[key])} -> (removed)")
            elif old[key] != new[key]:
                changes.append(f"{key}: {_compact(old[key])} -> {_compact(new[key])}")

        if changes:
            for change in changes:
                logger.info("config changed: %s", change)
        else:
            logger.info("config changed: mtime updated but content identical")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compact(value: object) -> str:
    """Compact JSON representation of a value, truncated to 80 chars."""
    s = json.dumps(value, separators=(",", ":"))
    return s if len(s) <= 80 else s[:77] + "..."


def compute_diff(old: dict, new: dict) -> list[dict]:
    """
    Compute a list of top-level key differences between *old* and *new*.

    Returns a list of dicts with keys: key, change (added/removed/modified),
    old_value, new_value.
    """
    all_keys = set(old) | set(new)
    result: list[dict] = []
    for key in sorted(all_keys):
        if key not in old:
            result.append({"key": key, "change": "added", "old_value": None, "new_value": new[key]})
        elif key not in new:
            result.append({"key": key, "change": "removed", "old_value": old[key], "new_value": None})
        elif old[key] != new[key]:
            result.append({"key": key, "change": "modified", "old_value": old[key], "new_value": new[key]})
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_watch(args: list[str]) -> None:
    """Watch a config file and print changes to stdout."""
    path = Path(args[0]) if args else Path(".autonomous-team/config.json")
    print(f"Watching {path} (Ctrl-C to stop)...", flush=True)

    from backend.log import setup_logging
    setup_logging(json_format=False)

    def on_change(old: dict, new: dict) -> None:
        diffs = compute_diff(old, new)
        for d in diffs:
            print(f"  [{d['change']}] {d['key']}: {_compact(d['old_value'])} -> {_compact(d['new_value'])}", flush=True)

    watcher = ConfigWatcher(path, poll_interval=2.0)
    watcher.register_callback(on_change)
    try:
        with watcher:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


def _cmd_diff(args: list[str]) -> None:
    """Print the diff between two config files."""
    if len(args) < 2:
        print("Usage: config_watcher.py diff <path1> <path2>", file=sys.stderr)
        sys.exit(1)
    p1, p2 = Path(args[0]), Path(args[1])
    try:
        old = json.loads(p1.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Error reading {p1}: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        new = json.loads(p2.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Error reading {p2}: {exc}", file=sys.stderr)
        sys.exit(1)

    diffs = compute_diff(old, new)
    if not diffs:
        print("No differences.")
        return
    for d in diffs:
        print(f"[{d['change']}] {d['key']}: {_compact(d['old_value'])} -> {_compact(d['new_value'])}")


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        print("Usage: config_watcher.py <watch|diff> [args...]", file=sys.stderr)
        sys.exit(1)
    cmd, rest = argv[0], argv[1:]
    if cmd == "watch":
        _cmd_watch(rest)
    elif cmd == "diff":
        _cmd_diff(rest)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
