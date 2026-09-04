"""
replay.py — Record and replay full agent interaction traces.

Every prompt sent, tool call made, and response received is captured as a
structured JSONL event and written to .autonomous-team/replays/{agent_id}.jsonl.
The ReplayRecorder subscribes to AgentOutputEvent on the event bus so recording
is automatic for any active agent.

Usage (standalone):
    from backend.replay import get_recorder

    rec = get_recorder()
    rec.start_recording("exec-42", role="executor", discussion=14)
    rec.record_event("exec-42", "prompt", "Implement the spec...")
    rec.record_event("exec-42", "tool_call", {"name": "Bash", "input": "ls"})
    rec.record_event("exec-42", "response", "Done.")
    rec.stop_recording("exec-42")

    replays = rec.list_replays()
    events  = rec.get_replay("exec-42")
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPLAYS_DIR = _REPO_ROOT / ".autonomous-team" / "replays"

_DEFAULT_RETENTION_DAYS = 7
_DEFAULT_MAX_STORAGE_MB = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_replay_config() -> dict:
    """Read replay config from config.json. Returns defaults on any failure."""
    config_path = _REPO_ROOT / ".autonomous-team" / "config.json"
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("replay", {})
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# ReplayRecorder
# ---------------------------------------------------------------------------


class ReplayRecorder:
    """
    Records full agent interaction traces as JSONL files.

    Thread-safe: each agent_id has its own lock so parallel agents don't
    block each other. File writes happen in the calling thread (sync) but
    callers should use publish_async() on the event bus to keep hot paths
    non-blocking.
    """

    def __init__(self, replays_dir: Path = _REPLAYS_DIR) -> None:
        self._dir = Path(replays_dir)
        # agent_id -> {"lock": Lock, "seq": int, "started_at": float, "role": str,
        #               "discussion": int|None, "event_count": int,
        #               "input_tokens": int, "output_tokens": int}
        self._active: dict[str, dict] = {}
        self._global_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_recording(
        self,
        agent_id: str,
        role: str = "",
        discussion: int | None = None,
    ) -> None:
        """Create the JSONL file and write a header event."""
        self._dir.mkdir(parents=True, exist_ok=True)
        state = {
            "lock": threading.Lock(),
            "seq": 0,
            "started_at": time.monotonic(),
            "started_ts": _now_iso(),
            "role": role,
            "discussion": discussion,
            "event_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        with self._global_lock:
            self._active[agent_id] = state

        header = {
            "seq": 0,
            "ts": state["started_ts"],
            "type": "header",
            "content": {
                "agent_id": agent_id,
                "role": role,
                "discussion": discussion,
            },
            "metadata": {},
        }
        self._append(agent_id, header)

    def record_event(
        self,
        agent_id: str,
        event_type: str,
        content: Any,
        metadata: dict | None = None,
    ) -> None:
        """Append one event line to the agent's JSONL file."""
        with self._global_lock:
            state = self._active.get(agent_id)
        if state is None:
            # Not recording this agent — silently ignore.
            return

        with state["lock"]:
            state["seq"] += 1
            state["event_count"] += 1
            seq = state["seq"]

            # Track token counts from metadata if provided.
            if metadata:
                state["input_tokens"] += metadata.get("input_tokens", 0)
                state["output_tokens"] += metadata.get("output_tokens", 0)

        event = {
            "seq": seq,
            "ts": _now_iso(),
            "type": event_type,
            "content": content,
            "metadata": metadata or {},
        }
        self._append(agent_id, event)

    def stop_recording(self, agent_id: str) -> dict | None:
        """Write a footer event with summary stats and close the recording."""
        with self._global_lock:
            state = self._active.pop(agent_id, None)
        if state is None:
            return None

        duration = time.monotonic() - state["started_at"]
        with state["lock"]:
            seq = state["seq"] + 1
            summary = {
                "total_events": state["event_count"],
                "duration_seconds": round(duration, 3),
                "input_tokens": state["input_tokens"],
                "output_tokens": state["output_tokens"],
            }

        footer = {
            "seq": seq,
            "ts": _now_iso(),
            "type": "footer",
            "content": summary,
            "metadata": {},
        }
        self._append(agent_id, footer)
        return summary

    def list_replays(self, limit: int = 20) -> list[dict]:
        """Return metadata for recent replays, pruning stale/oversized files first."""
        self._prune()
        self._dir.mkdir(parents=True, exist_ok=True)

        entries = []
        for path in sorted(self._dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = self._read_meta(path)
            if meta is not None:
                entries.append(meta)
            if len(entries) >= limit:
                break
        return entries

    def get_replay(self, agent_id: str) -> list[dict]:
        """Return the full event list for one replay in sequence order."""
        path = self._dir / f"{agent_id}.jsonl"
        if not path.exists():
            return []
        events = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        events.sort(key=lambda e: e.get("seq", 0))
        return events

    def get_summary(self, agent_id: str) -> dict | None:
        """Return header + footer events only (no content bulk)."""
        events = self.get_replay(agent_id)
        if not events:
            return None
        header = next((e for e in events if e.get("type") == "header"), None)
        footer = next((e for e in events if e.get("type") == "footer"), None)
        return {"header": header, "footer": footer}

    # ------------------------------------------------------------------
    # Event bus integration
    # ------------------------------------------------------------------

    def handle_agent_output_event(self, event: object) -> None:
        """Subscribe this to AgentOutputEvent on the bus.

        Only records events for agents that have an active recording started
        via start_recording(). This is intentional — the recorder doesn't
        auto-start for every agent; callers must opt in.
        """
        agent_id = getattr(event, "agent_id", None)
        if not agent_id:
            return
        with self._global_lock:
            active = agent_id in self._active
        if not active:
            return

        subtype = getattr(event, "event_subtype", "")
        content = getattr(event, "content", "")
        role = getattr(event, "agent_role", "")

        # Map event_subtype to replay event type.
        type_map = {
            "thinking": "prompt",
            "content": "response",
            "tool_use": "tool_call",
            "tool_result": "tool_result",
            "done": "response",
            "error": "error",
        }
        event_type = type_map.get(subtype, subtype or "response")
        self.record_event(agent_id, event_type, content, metadata={"role": role})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append(self, agent_id: str, event: dict) -> None:
        """Append a JSON line to the agent's file. Safe with fcntl.flock."""
        import fcntl  # noqa: PLC0415 — stdlib, Unix-only

        path = self._dir / f"{agent_id}.jsonl"
        line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(line)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _read_meta(self, path: Path) -> dict | None:
        """Read header and footer from a JSONL file to build metadata."""
        try:
            header: dict | None = None
            footer: dict | None = None
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = obj.get("type")
                    if t == "header":
                        header = obj
                    elif t == "footer":
                        footer = obj
            if header is None:
                return None
            hc = header.get("content", {})
            fc = footer.get("content", {}) if footer else {}
            return {
                "agent_id": hc.get("agent_id", path.stem),
                "role": hc.get("role", ""),
                "discussion": hc.get("discussion"),
                "started_at": header.get("ts", ""),
                "event_count": fc.get("total_events", 0),
                "duration_seconds": fc.get("duration_seconds"),
                "file": str(path),
            }
        except Exception:  # noqa: BLE001
            return None

    def _prune(self) -> None:
        """Lazy cleanup: remove files older than retention_days, then cap storage."""
        if not self._dir.exists():
            return

        cfg = _load_replay_config()
        retention_days = cfg.get("retention_days", _DEFAULT_RETENTION_DAYS)
        max_storage_mb = cfg.get("max_storage_mb", _DEFAULT_MAX_STORAGE_MB)

        now = time.time()
        cutoff = now - retention_days * 86400

        # Prune by age.
        for path in self._dir.glob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

        # Prune by total size (oldest first).
        max_bytes = max_storage_mb * 1024 * 1024
        files = sorted(
            [(p, p.stat().st_size, p.stat().st_mtime) for p in self._dir.glob("*.jsonl")],
            key=lambda t: t[2],  # oldest first
        )
        total = sum(size for _, size, _ in files)
        for path, size, _ in files:
            if total <= max_bytes:
                break
            try:
                path.unlink(missing_ok=True)
                total -= size
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_recorder_lock = threading.Lock()
_global_recorder: ReplayRecorder | None = None


def get_recorder(replays_dir: Path = _REPLAYS_DIR) -> ReplayRecorder:
    """Return the process-global ReplayRecorder, creating it on first call."""
    global _global_recorder
    if _global_recorder is None:
        with _recorder_lock:
            if _global_recorder is None:
                _global_recorder = ReplayRecorder(replays_dir)
    return _global_recorder


# ---------------------------------------------------------------------------
# ReplayEngine — re-emit recorded traces through the event bus
# ---------------------------------------------------------------------------

_SPEED_FACTORS: dict[str, float] = {
    "1x": 1.0,
    "5x": 5.0,
    "10x": 10.0,
    "instant": 0.0,
}


class ReplayEngine:
    """
    Reads a recorded JSONL trace and re-emits each event through the EventBus.

    Only one ReplayEngine session may be active at a time.  Starting a new one
    automatically stops the previous one.

    Thread-safety: pause/resume/stop/seek are safe to call from any thread
    while the replay background thread is running.
    """

    def __init__(
        self,
        agent_id: str,
        events: list[dict],
        speed: str = "1x",
        replays_dir: Path = _REPLAYS_DIR,
    ) -> None:
        if speed not in _SPEED_FACTORS:
            raise ValueError(f"speed must be one of {list(_SPEED_FACTORS)}; got {repr(speed)}")

        self.agent_id = agent_id
        self.speed = speed
        self._speed_factor = _SPEED_FACTORS[speed]
        self._events = events  # all events in seq order
        self.replay_session_id: str = str(uuid.uuid4())

        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially
        self._stop_flag = threading.Event()

        self._lock = threading.Lock()
        self._current_event: int = 0  # index into self._events
        self._seek_to: int | None = None  # pending seek index (set from outside)

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"replay-{self.replay_session_id[:8]}",
        )

    # ------------------------------------------------------------------
    # Control API (thread-safe — callable from any API handler thread)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the replay thread."""
        self._thread.start()

    def pause(self) -> None:
        """Pause event emission at the current position."""
        self._pause_event.clear()

    def resume(self) -> None:
        """Resume event emission from the current position."""
        self._pause_event.set()

    def stop(self) -> None:
        """Signal the replay thread to stop; waits up to 1 second for it."""
        self._stop_flag.set()
        self._pause_event.set()  # unblock if paused
        self._thread.join(timeout=1.0)

    def seek(self, event_number: int) -> None:
        """Jump to event_number (0-based index into the event list).

        Takes effect before the next event is emitted. Seeking does NOT pause
        playback — it just repositions the pointer.
        """
        with self._lock:
            self._seek_to = max(0, min(event_number, len(self._events) - 1))

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return not self._pause_event.is_set()

    def get_status(self) -> dict:
        with self._lock:
            current = self._current_event
        return {
            "active": self.is_alive,
            "agent_id": self.agent_id,
            "speed": self.speed,
            "current_event": current,
            "total_events": len(self._events),
            "paused": self.paused,
            "replay_session_id": self.replay_session_id,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        from backend.event_bus import AgentOutputEvent, get_bus  # noqa: PLC0415

        bus = get_bus()
        prev_ts: str | None = None

        idx = 0
        while idx < len(self._events):
            if self._stop_flag.is_set():
                break

            # Apply any pending seek.
            with self._lock:
                if self._seek_to is not None:
                    idx = self._seek_to
                    self._seek_to = None
                    prev_ts = None  # reset inter-event delay after seek

            if idx >= len(self._events):
                break

            # Wait while paused (checks stop flag every 0.05 s).
            while not self._pause_event.wait(timeout=0.05):
                if self._stop_flag.is_set():
                    return

            ev = self._events[idx]

            # Inter-event delay (skip for first event or instant mode).
            if prev_ts is not None and self._speed_factor > 0:
                try:
                    t0 = datetime.fromisoformat(prev_ts.replace("Z", "+00:00")).timestamp()
                    t1 = datetime.fromisoformat(
                        ev.get("ts", prev_ts).replace("Z", "+00:00")
                    ).timestamp()
                    raw_delay = max(0.0, t1 - t0)
                    delay = raw_delay / self._speed_factor
                    if delay > 0:
                        # Sleep in small chunks so stop/seek can interrupt.
                        deadline = time.monotonic() + delay
                        while time.monotonic() < deadline:
                            if self._stop_flag.is_set():
                                return
                            with self._lock:
                                if self._seek_to is not None:
                                    break
                            time.sleep(min(0.05, deadline - time.monotonic()))
                except Exception:  # noqa: BLE001
                    pass  # bad timestamp — skip delay

            if self._stop_flag.is_set():
                break

            # Re-emit the event through the bus.
            try:
                ev_dict = dict(ev)
                ev_dict["replay"] = True
                ev_dict["replay_session_id"] = self.replay_session_id

                bus.publish(AgentOutputEvent(
                    source="replay",
                    agent_id=self.agent_id,
                    agent_role=ev_dict.get("metadata", {}).get("role", ""),
                    content=str(ev_dict.get("content", "")),
                    event_subtype=ev_dict.get("type", ""),
                ))
            except Exception:  # noqa: BLE001
                pass  # never crash the replay thread

            prev_ts = ev.get("ts", prev_ts)

            with self._lock:
                self._current_event = idx

            idx += 1


# ---------------------------------------------------------------------------
# Singleton active-replay registry
# ---------------------------------------------------------------------------

_global_lock = threading.Lock()
_active_replay: ReplayEngine | None = None


def start_replay(
    agent_id: str,
    speed: str = "1x",
    replays_dir: Path = _REPLAYS_DIR,
) -> ReplayEngine:
    """Start a new replay session.

    If a replay is already active it is stopped first.
    Raises FileNotFoundError when no JSONL file exists for *agent_id*.
    Raises ValueError on an invalid *speed* value.
    """
    global _active_replay  # noqa: PLW0603

    # Load events before acquiring the lock so I/O doesn't block other calls.
    path = Path(replays_dir) / f"{agent_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no replay found for agent_id '{agent_id}'")

    events: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    events.sort(key=lambda e: e.get("seq", 0))

    engine = ReplayEngine(agent_id=agent_id, events=events, speed=speed, replays_dir=replays_dir)

    with _global_lock:
        if _active_replay is not None:
            _active_replay.stop()
        _active_replay = engine

    engine.start()
    return engine


def get_active_replay() -> ReplayEngine | None:
    """Return the currently active ReplayEngine, or None."""
    with _global_lock:
        return _active_replay


def stop_active_replay() -> bool:
    """Stop the active replay.  Returns True if there was one, False if idle."""
    global _active_replay  # noqa: PLW0603
    with _global_lock:
        eng = _active_replay
        _active_replay = None
    if eng is not None:
        eng.stop()
        return True
    return False
