"""
Lightweight W3C Trace Context compatible distributed tracing — zero external dependencies.

Provides trace/span ID generation, context propagation via contextvars, and a
thread-safe span collector. Integrates with trace_export.py for OTLP JSON output.

Usage:
    from backend.tracing import start_span, get_current_trace_id, get_collector

    with start_span("my-operation", attributes={"key": "value"}) as span:
        # do work — child spans created here will be parented to this span
        with start_span("child-operation") as child:
            pass  # child.parent_span_id == span.span_id

    # Correlate logs with trace ID
    trace_id = get_current_trace_id()

    # Parse / set a traceparent header (W3C format)
    ctx = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    set_remote_context(ctx["trace_id"], ctx["parent_id"])
"""

from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Generator


# ---------------------------------------------------------------------------
# Span dataclass
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """
    A single unit of work within a distributed trace.

    Fields follow the OTLP / W3C Trace Context naming conventions.
    """

    trace_id: str
    """128-bit hex trace identifier (32 hex chars)."""

    span_id: str
    """64-bit hex span identifier (16 hex chars)."""

    parent_span_id: str
    """Parent span_id, or empty string for root spans."""

    operation_name: str
    """Human-readable description of the work this span represents."""

    service_name: str
    """Name of the service that emitted this span."""

    start_time_unix_nano: int
    """Span start time in nanoseconds since Unix epoch."""

    end_time_unix_nano: int = 0
    """Span end time in nanoseconds since Unix epoch. 0 means still in progress."""

    status: str = "UNSET"
    """Span status: UNSET | OK | ERROR."""

    attributes: dict = field(default_factory=dict)
    """Key/value metadata attached to the span."""

    events: list = field(default_factory=list)
    """List of timed log entries within this span."""

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        """Append a named event to this span's event log."""
        self.events.append(
            {
                "name": name,
                "time_unix_nano": time.time_ns(),
                "attributes": attributes or {},
            }
        )

    def is_complete(self) -> bool:
        """Return True when the span has been ended."""
        return self.end_time_unix_nano > 0


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------

_current_span: ContextVar[Span | None] = ContextVar("_current_span", default=None)


def get_current_span() -> Span | None:
    """Return the active span in the current execution context, or None."""
    return _current_span.get()


def get_current_trace_id() -> str:
    """
    Return the current trace ID for log correlation.

    Returns an empty string when no trace is active.
    """
    span = _current_span.get()
    return span.trace_id if span is not None else ""


# ---------------------------------------------------------------------------
# ID generation (W3C Trace Context spec)
# ---------------------------------------------------------------------------


def _new_trace_id() -> str:
    """Generate a random 128-bit trace ID as a 32-character lowercase hex string."""
    return secrets.token_hex(16)


def _new_span_id() -> str:
    """Generate a random 64-bit span ID as a 16-character lowercase hex string."""
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Span collector — thread-safe, bounded buffer
# ---------------------------------------------------------------------------

_MAX_COLLECTOR_SPANS = 10_000


class SpanCollector:
    """
    Thread-safe buffer of completed Span objects.

    Spans are added by the start_span context manager on exit.
    The exporter reads and drains completed spans periodically.

    The buffer is bounded at _MAX_COLLECTOR_SPANS entries to prevent
    unbounded memory growth if the exporter stalls.
    """

    def __init__(self, max_spans: int = _MAX_COLLECTOR_SPANS) -> None:
        self._lock = threading.Lock()
        self._spans: list[Span] = []
        self._max = max_spans

    def add(self, span: Span) -> None:
        """Add a completed span to the buffer. Drops oldest if at capacity."""
        with self._lock:
            if len(self._spans) >= self._max:
                self._spans.pop(0)
            self._spans.append(span)

    def drain(self) -> list[Span]:
        """Remove and return all buffered spans atomically."""
        with self._lock:
            spans, self._spans = self._spans, []
            return spans

    def peek(self, limit: int = 1000) -> list[Span]:
        """Return up to *limit* spans without removing them."""
        with self._lock:
            return list(self._spans[-limit:])

    def count(self) -> int:
        """Return the number of buffered spans."""
        with self._lock:
            return len(self._spans)


_collector_lock = threading.Lock()
_global_collector: SpanCollector | None = None


def get_collector() -> SpanCollector:
    """Return the process-global SpanCollector, creating it on first call."""
    global _global_collector  # noqa: PLW0603
    if _global_collector is None:
        with _collector_lock:
            if _global_collector is None:
                _global_collector = SpanCollector()
    return _global_collector


# ---------------------------------------------------------------------------
# start_span context manager
# ---------------------------------------------------------------------------


@contextmanager
def start_span(
    operation_name: str,
    *,
    attributes: dict | None = None,
    service_name: str = "fulcrumaxe",
) -> Generator[Span, None, None]:
    """
    Context manager that creates a child span automatically parented to the
    current span (or a root span if none is active), sets it as the current
    context, and records it on exit.

    Example::

        with start_span("process-request", attributes={"http.method": "GET"}) as span:
            span.attributes["http.url"] = "/health"
            with start_span("db-query") as child:
                pass  # child.parent_span_id == span.span_id
    """
    parent = _current_span.get()
    trace_id = parent.trace_id if parent is not None else _new_trace_id()
    parent_id = parent.span_id if parent is not None else ""

    span = Span(
        trace_id=trace_id,
        span_id=_new_span_id(),
        parent_span_id=parent_id,
        operation_name=operation_name,
        service_name=service_name,
        start_time_unix_nano=time.time_ns(),
        attributes=dict(attributes or {}),
    )
    token = _current_span.set(span)
    try:
        yield span
        if span.status == "UNSET":
            span.status = "OK"
    except Exception:
        span.status = "ERROR"
        raise
    finally:
        span.end_time_unix_nano = time.time_ns()
        _current_span.reset(token)
        get_collector().add(span)


# ---------------------------------------------------------------------------
# W3C traceparent header helpers
# ---------------------------------------------------------------------------

_TRACEPARENT_VERSION = "00"


def parse_traceparent(header: str) -> dict | None:
    """
    Parse a W3C traceparent header into a dict with trace_id, parent_id, and flags.

    Returns None if the header is absent, malformed, or uses an unsupported version.

    Expected format: ``00-{trace_id}-{parent_id}-{flags}``
    """
    if not header:
        return None
    parts = header.strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, parent_id, flags = parts
    # Reject future versions that change the format (per W3C spec).
    if version != _TRACEPARENT_VERSION:
        return None
    if len(trace_id) != 32 or len(parent_id) != 16:
        return None
    # All-zero trace/span IDs are invalid per spec.
    if trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    return {"trace_id": trace_id, "parent_id": parent_id, "flags": flags}


def make_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """
    Produce a W3C traceparent header value.

    ``sampled=True`` sets the trace-flags to ``01`` (sampled), ``False`` → ``00``.
    """
    flags = "01" if sampled else "00"
    return f"{_TRACEPARENT_VERSION}-{trace_id}-{span_id}-{flags}"


def set_remote_context(trace_id: str, parent_span_id: str) -> Span:
    """
    Install a remote (incoming) trace context as the current span so that
    child spans created in this thread will link back to the upstream caller.

    Returns the synthetic root Span that was installed.
    """
    span = Span(
        trace_id=trace_id,
        span_id=parent_span_id,
        parent_span_id="",
        operation_name="remote-parent",
        service_name="remote",
        start_time_unix_nano=time.time_ns(),
        status="OK",
    )
    span.end_time_unix_nano = span.start_time_unix_nano  # already ended (remote)
    _current_span.set(span)
    return span
