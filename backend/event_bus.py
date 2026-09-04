"""
In-process publish/subscribe event bus — typed, thread-safe, zero dependencies.

Components publish typed events (dataclasses) and subscribe to specific event
types via callbacks. The bus runs in the same process as the API server and
uses only stdlib (threading, queue, dataclasses).

Usage:
    from backend.event_bus import get_bus, AgentOutputEvent

    # Subscribe
    bus = get_bus()
    sub_id = bus.subscribe(AgentOutputEvent, lambda e: print(e))

    # Publish
    bus.publish(AgentOutputEvent(
        timestamp="2026-04-10T00:00:00Z",
        source="server",
        agent_id="exec-1",
        agent_role="executor",
        content="hello",
        event_subtype="content",
    ))

    # Unsubscribe
    bus.unsubscribe(sub_id)
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Event base class and concrete event types
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Event:
    """Base event. All concrete events must subclass this."""

    timestamp: str = field(default_factory=_now_iso)
    source: str = ""
    trace_id: str = ""
    """W3C trace ID propagated from the publishing execution context.
    Empty string when no trace is active — backward-compatible default."""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentOutputEvent(Event):
    """Fired by server.py / agent wrappers when an agent produces output."""

    agent_id: str = ""
    agent_role: str = ""
    content: str = ""
    event_subtype: str = ""  # thinking | content | tool_use | tool_result | done | error


@dataclass
class BudgetSpendEvent(Event):
    """Fired by BudgetTracker.record_spend() on every token-spend record."""

    agent_id: str = ""
    role: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    discussion: int | None = None
    model: str = "default"


@dataclass
class LoopIterationEvent(Event):
    """Fired at the end of each /loop iteration."""

    iteration_id: str = ""
    idle: bool = False
    duration_seconds: float = 0.0
    agents_spawned: int = 0


@dataclass
class GateChangeEvent(Event):
    """Fired by ConfigWatcher when a gate value changes."""

    gate_name: str = ""
    old_value: bool = False
    new_value: bool = False


@dataclass
class ModuleHealthEvent(Event):
    """Fired when a backend module fails to import during a health check."""

    module_name: str = ""
    errors: list = field(default_factory=list)


@dataclass
class QualityScoreEvent(Event):
    """Fired by QualityScorer when a PR quality score is computed."""

    pr: int = 0
    discussion: int | None = None
    total_score: int = 0
    grade: str = ""


@dataclass
class ConfigValidationEvent(Event):
    """Fired by SchemaValidator when validation errors are found in a config file."""

    file_name: str = ""
    errors: list = field(default_factory=list)  # list[str]


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """
    Thread-safe, in-process publish/subscribe event bus.

    - subscribe()  registers a callback for one event type; returns a sub_id.
    - unsubscribe() removes a previously registered callback.
    - publish()     dispatches synchronously to all matching subscribers,
                    outside the lock so subscribers can themselves publish.
    - publish_async() queues an event for delivery by a background worker
                      thread, safe to call from async/threaded contexts where
                      blocking is undesirable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # event_type -> {sub_id -> callback}
        self._subs: dict[type, dict[str, Callable]] = {}
        self._counter = 0

        # Async dispatch machinery
        self._async_queue: queue.Queue[Event | None] = queue.Queue()
        self._worker = threading.Thread(
            target=self._async_worker, daemon=True, name="event-bus-worker"
        )
        self._worker.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, event_type: type, callback: Callable) -> str:
        """
        Register *callback* to be called whenever an event of *event_type*
        is published. Returns a subscription ID that can be passed to
        unsubscribe().
        """
        with self._lock:
            self._counter += 1
            sub_id = f"sub-{self._counter}"
            self._subs.setdefault(event_type, {})[sub_id] = callback
            return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove the subscription identified by *subscription_id*."""
        with self._lock:
            for subs_by_id in self._subs.values():
                subs_by_id.pop(subscription_id, None)

    def publish(self, event: Event) -> None:
        """
        Synchronously dispatch *event* to all subscribers of its exact type.

        Dispatch happens outside the lock, so subscribers may themselves
        call publish() or subscribe() without deadlocking. Exceptions raised
        by subscribers are silently swallowed so the publisher is never
        interrupted.

        Trace context: if the event carries a trace_id, each subscriber
        callback is invoked after restoring that trace context (via
        backend.tracing) so that child spans created inside the callback
        link back to the publisher's trace automatically.
        """
        # Inject current trace context into the event if not already set.
        if not event.trace_id:
            try:
                from backend.tracing import get_current_trace_id  # noqa: PLC0415
                event.trace_id = get_current_trace_id()
            except ImportError:
                pass

        with self._lock:
            callbacks = list(self._subs.get(type(event), {}).values())

        for cb in callbacks:
            try:
                if event.trace_id:
                    # Restore trace context so subscriber child spans link correctly.
                    try:
                        from backend.tracing import set_remote_context  # noqa: PLC0415
                        set_remote_context(event.trace_id, "")
                    except ImportError:
                        pass
                cb(event)
            except Exception:  # noqa: BLE001
                pass  # never crash the publisher

    def publish_async(self, event: Event) -> None:
        """
        Queue *event* for async dispatch by the background worker thread.

        Returns immediately; the event will be delivered shortly after in
        a dedicated thread. Useful from async contexts or hot paths where
        blocking is undesirable.
        """
        self._async_queue.put(event)

    def shutdown(self) -> None:
        """Signal the async worker to stop. Blocks until it exits."""
        self._async_queue.put(None)  # sentinel
        self._worker.join(timeout=5)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _async_worker(self) -> None:
        """Background thread: drain async_queue and dispatch events."""
        while True:
            item = self._async_queue.get()
            if item is None:
                break  # sentinel — shut down
            self.publish(item)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_bus_lock = threading.Lock()
_global_bus: EventBus | None = None


def get_bus() -> EventBus:
    """Return the process-global EventBus, creating it on first call."""
    global _global_bus
    if _global_bus is None:
        with _bus_lock:
            if _global_bus is None:
                _global_bus = EventBus()
    return _global_bus


# ---------------------------------------------------------------------------
# FileAppender — backward-compatible persistence subscriber
# ---------------------------------------------------------------------------


class FileAppender:
    """
    Subscriber that writes AgentOutputEvent instances to a JSONL file.

    Maintains backward compatibility with external tools that read
    agent-feed.jsonl directly. Register with:

        bus = get_bus()
        appender = FileAppender(Path(".autonomous-team/agent-feed.jsonl"))
        bus.subscribe(AgentOutputEvent, appender.handle)
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def handle(self, event: AgentOutputEvent) -> None:
        """Append *event* as a JSON line to the feed file."""
        line = json.dumps(event.to_dict(), default=str) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)


# ---------------------------------------------------------------------------
# BusEventFileAppender — all-4-types subscriber for TS /events parity
# ---------------------------------------------------------------------------


class BusEventFileAppender:
    """
    Subscriber that writes ALL 4 event bus event types to a JSONL file,
    including the ``_event_type`` field that Python's /events SSE emits.

    This provides the persistence layer consumed by the TypeScript /events
    SSE handler so it can emit all 4 event types without being in-process.

    Serialisation is byte-equivalent to what _bus_gen (streams.py) emits:
        json.dumps(event.to_dict() | {"_event_type": type(event).__name__},
                   default=str)

    Register with:

        bus = get_bus()
        appender = BusEventFileAppender(Path(".autonomous-team/events-bus.jsonl"))
        for event_type in (AgentOutputEvent, BudgetSpendEvent,
                           GateChangeEvent, LoopIterationEvent):
            bus.subscribe(event_type, appender.handle)

    The subscriber is best-effort: any IO error is silently swallowed so it
    can never crash the publisher or the bus worker thread.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def handle(self, event: Event) -> None:
        """Append *event* as a JSON line to the events bus file.

        Adds ``_event_type`` = the event's class name, matching the wire
        format that Python's /events SSE emits via _bus_gen(add_event_type=True).
        """
        try:
            data = event.to_dict()
            data["_event_type"] = type(event).__name__
            line = json.dumps(data, default=str) + "\n"
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception:  # noqa: BLE001
            pass  # never crash the bus
