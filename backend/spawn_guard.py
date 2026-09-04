"""
spawn_guard.py — In-process chokepoint for all claude subprocess spawns.

Every call to _start_loop_run (and any direct subprocess.Popen(["claude", ...]))
must go through SpawnGuard.acquire() before spawning. This module enforces:

1. Wall-clock minimum interval per source (default 60s)
2. Per-source in-flight cap (default 1)
3. Global in-flight cap (default 2)
4. Hard feature gate: gates.allow_claude_spawn (default false, re-read on every acquire)

Usage:
    guard = SpawnGuard()
    result = guard.acquire("loop_run_global")
    if result.status != AcquireStatus.OK:
        # handle RATE_LIMITED, CAP_REACHED, GATE_DISABLED
        ...
        return
    try:
        # do the spawn
        ...
    finally:
        guard.release("loop_run_global")
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# Allow running standalone (e.g. CLI) or as part of the backend package.
try:
    from backend.control_plane import ControlPlane
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.control_plane import ControlPlane  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Constants / defaults (overridable via config policies.spawn_guard.*)
# ---------------------------------------------------------------------------

_DEFAULT_MIN_INTERVAL_SECONDS: int = 60
_DEFAULT_PER_SOURCE_CAP: int = 1
_DEFAULT_GLOBAL_CAP: int = 2

_STATS_FILE = Path(".autonomous-team/spawn-guard-stats.json")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class AcquireStatus(str, Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    CAP_REACHED = "cap_reached"
    GATE_DISABLED = "gate_disabled"


@dataclass
class AcquireResult:
    status: AcquireStatus
    retry_after_seconds: int = 0   # meaningful when status == RATE_LIMITED
    source: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Per-source state (internal)
# ---------------------------------------------------------------------------

@dataclass
class _SourceState:
    fires_total: int = 0
    in_flight: int = 0
    last_fire_ts: Optional[float] = None  # epoch seconds


# ---------------------------------------------------------------------------
# SpawnGuard
# ---------------------------------------------------------------------------

class SpawnGuard:
    """Thread-safe, process-local spawn guard.

    A single shared instance is created at module level (_GUARD) and used by
    backend/api.py. Tests can instantiate their own copies.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_source: dict[str, _SourceState] = {}
        self._global_in_flight: int = 0
        self._cp = ControlPlane()

    # ------------------------------------------------------------------
    # Config helpers (re-read each call — supports live gate flip)
    # ------------------------------------------------------------------

    def _gate_enabled(self) -> bool:
        try:
            self._cp.load()
            val = self._cp.get("gates.allow_claude_spawn")
            if val is None:
                return False  # default false when key absent
            return bool(val)
        except Exception:  # noqa: BLE001
            return False  # fail-safe: deny if config unreadable

    def _min_interval(self) -> int:
        try:
            self._cp.load()
            v = self._cp.get("policies.spawn_guard.min_interval_seconds")
            return int(v) if v is not None else _DEFAULT_MIN_INTERVAL_SECONDS
        except Exception:  # noqa: BLE001
            return _DEFAULT_MIN_INTERVAL_SECONDS

    def _per_source_cap(self) -> int:
        try:
            self._cp.load()
            v = self._cp.get("policies.spawn_guard.per_source_cap")
            return int(v) if v is not None else _DEFAULT_PER_SOURCE_CAP
        except Exception:  # noqa: BLE001
            return _DEFAULT_PER_SOURCE_CAP

    def _global_cap(self) -> int:
        try:
            self._cp.load()
            v = self._cp.get("policies.spawn_guard.global_cap")
            return int(v) if v is not None else _DEFAULT_GLOBAL_CAP
        except Exception:  # noqa: BLE001
            return _DEFAULT_GLOBAL_CAP

    def _source_state(self, source: str) -> _SourceState:
        """Return (creating if needed) the state for source. Must hold _lock."""
        if source not in self._by_source:
            self._by_source[source] = _SourceState()
        return self._by_source[source]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, source: str) -> AcquireResult:
        """Try to acquire a spawn slot for the given source label.

        Returns AcquireResult with status OK, RATE_LIMITED, CAP_REACHED, or
        GATE_DISABLED. Does NOT block.
        """
        # Gate check first (cheap, outside lock)
        if not self._gate_enabled():
            return AcquireResult(
                status=AcquireStatus.GATE_DISABLED,
                source=source,
                message="gates.allow_claude_spawn is false or missing",
            )

        min_interval = self._min_interval()
        per_source_cap = self._per_source_cap()
        global_cap = self._global_cap()
        now = time.monotonic()

        with self._lock:
            st = self._source_state(source)

            # Rate-limit check (interval per source)
            if st.last_fire_ts is not None:
                elapsed = now - st.last_fire_ts
                if elapsed < min_interval:
                    retry_after = int(min_interval - elapsed) + 1
                    return AcquireResult(
                        status=AcquireStatus.RATE_LIMITED,
                        retry_after_seconds=retry_after,
                        source=source,
                        message=f"source {source!r} fired {elapsed:.1f}s ago; min interval {min_interval}s",
                    )

            # Per-source cap
            if st.in_flight >= per_source_cap:
                return AcquireResult(
                    status=AcquireStatus.CAP_REACHED,
                    source=source,
                    message=f"source {source!r} already has {st.in_flight} in-flight (cap {per_source_cap})",
                )

            # Global cap
            if self._global_in_flight >= global_cap:
                return AcquireResult(
                    status=AcquireStatus.CAP_REACHED,
                    source=source,
                    message=f"global in-flight cap {global_cap} reached",
                )

            # All checks passed — claim slot
            st.in_flight += 1
            st.fires_total += 1
            st.last_fire_ts = now
            self._global_in_flight += 1

        # Write stats file outside lock (best-effort)
        self._persist_stats()
        return AcquireResult(status=AcquireStatus.OK, source=source)

    def release(self, source: str) -> None:
        """Release an in-flight slot. Must be called in a finally block."""
        with self._lock:
            if source in self._by_source:
                st = self._by_source[source]
                if st.in_flight > 0:
                    st.in_flight -= 1
            if self._global_in_flight > 0:
                self._global_in_flight -= 1

        self._persist_stats()

    def stats(self) -> dict:
        """Return a snapshot of current guard state (safe to call anytime)."""
        with self._lock:
            by_source: dict = {}
            for src, st in self._by_source.items():
                last_ts = None
                if st.last_fire_ts is not None:
                    # Convert monotonic offset to approximate wall-clock ISO string
                    wall = time.time() - (time.monotonic() - st.last_fire_ts)
                    import datetime as _dt  # noqa: PLC0415
                    last_ts = _dt.datetime.fromtimestamp(wall, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
                by_source[src] = {
                    "fires_total": st.fires_total,
                    "in_flight": st.in_flight,
                    "last_fire_ts": last_ts,
                }
            return {
                "by_source": by_source,
                "global_in_flight": self._global_in_flight,
                "gate_enabled": self._gate_enabled(),
            }

    def assert_gate_present(self) -> None:
        """Raise RuntimeError if gates.allow_claude_spawn is absent from config.

        Called at server startup. Presence of the key (with any boolean value)
        is sufficient — the value itself is validated at acquire() time.
        """
        try:
            self._cp.load()
            val = self._cp.get("gates.allow_claude_spawn")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"SpawnGuard: could not read .autonomous-team/config.json: {exc}\n"
                "Remediation: set gates.allow_claude_spawn to true or false in "
                ".autonomous-team/config.json before starting backend/api.py"
            ) from exc
        if val is None:
            raise RuntimeError(
                "SpawnGuard: gates.allow_claude_spawn is missing from .autonomous-team/config.json\n"
                "Remediation: set gates.allow_claude_spawn to true or false in "
                ".autonomous-team/config.json before starting backend/api.py"
            )

    # ------------------------------------------------------------------
    # Stats persistence (best-effort)
    # ------------------------------------------------------------------

    def _persist_stats(self) -> None:
        """Write current stats to _STATS_FILE atomically. Non-fatal on error."""
        try:
            data = self.stats()
            _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=_STATS_FILE.parent, suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, _STATS_FILE)
            except Exception:
                os.unlink(tmp_path)
                raise
        except OSError:
            pass  # non-fatal

    @classmethod
    def read_stats_file(cls) -> Optional[dict]:
        """Read stats from the file written by a running server. Returns None if missing."""
        try:
            return json.loads(_STATS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return None


# ---------------------------------------------------------------------------
# Module-level singleton used by backend/api.py
# ---------------------------------------------------------------------------

_GUARD = SpawnGuard()
