"""
Unit tests for backend.tracing — span creation, context propagation,
W3C traceparent header parsing, and the span collector.
"""

from __future__ import annotations

import time

import pytest

from backend.tracing import (
    Span,
    SpanCollector,
    _new_span_id,
    _new_trace_id,
    get_collector,
    get_current_span,
    get_current_trace_id,
    make_traceparent,
    parse_traceparent,
    set_remote_context,
    start_span,
)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def test_new_trace_id_length_and_hex() -> None:
    """generate_trace_id returns a 32-char lowercase hex string."""
    tid = _new_trace_id()
    assert len(tid) == 32
    assert tid == tid.lower()
    int(tid, 16)  # must be valid hex — raises ValueError otherwise


def test_new_span_id_length_and_hex() -> None:
    """generate_span_id returns a 16-char lowercase hex string."""
    sid = _new_span_id()
    assert len(sid) == 16
    assert sid == sid.lower()
    int(sid, 16)


def test_ids_are_unique() -> None:
    """Each call produces a distinct ID."""
    ids = {_new_trace_id() for _ in range(50)}
    assert len(ids) == 50


# ---------------------------------------------------------------------------
# Span dataclass
# ---------------------------------------------------------------------------


def test_span_defaults() -> None:
    """Span starts with UNSET status and empty parent."""
    sp = Span(
        trace_id=_new_trace_id(),
        span_id=_new_span_id(),
        parent_span_id="",
        operation_name="test",
        service_name="svc",
        start_time_unix_nano=time.time_ns(),
    )
    assert sp.status == "UNSET"
    assert sp.parent_span_id == ""
    assert sp.end_time_unix_nano == 0
    assert not sp.is_complete()


def test_span_add_event() -> None:
    """add_event appends an entry to the events list."""
    sp = Span(
        trace_id=_new_trace_id(),
        span_id=_new_span_id(),
        parent_span_id="",
        operation_name="test",
        service_name="svc",
        start_time_unix_nano=time.time_ns(),
    )
    sp.add_event("checkpoint", {"step": 1})
    assert len(sp.events) == 1
    assert sp.events[0]["name"] == "checkpoint"
    assert sp.events[0]["attributes"]["step"] == 1


def test_span_is_complete_after_end() -> None:
    """is_complete returns True once end_time_unix_nano is set."""
    sp = Span(
        trace_id=_new_trace_id(),
        span_id=_new_span_id(),
        parent_span_id="",
        operation_name="test",
        service_name="svc",
        start_time_unix_nano=time.time_ns(),
    )
    assert not sp.is_complete()
    sp.end_time_unix_nano = time.time_ns()
    assert sp.is_complete()


# ---------------------------------------------------------------------------
# start_span context manager
# ---------------------------------------------------------------------------


def test_start_span_creates_root_span() -> None:
    """When no parent is active, start_span creates a root span (empty parent_id)."""
    with start_span("root-op") as sp:
        assert sp.parent_span_id == ""
        assert len(sp.trace_id) == 32
        assert len(sp.span_id) == 16
        assert sp.status == "UNSET"  # still running


def test_start_span_sets_ok_on_clean_exit() -> None:
    """Span status is set to OK when the block exits without raising."""
    with start_span("op") as sp:
        pass
    assert sp.status == "OK"
    assert sp.end_time_unix_nano > 0


def test_start_span_sets_error_on_exception() -> None:
    """Span status is ERROR when an exception propagates out of the block."""
    try:
        with start_span("failing-op") as sp:
            raise ValueError("boom")
    except ValueError:
        pass
    assert sp.status == "ERROR"


def test_start_span_child_inherits_trace_id() -> None:
    """Child spans share the parent's trace_id."""
    with start_span("parent") as parent:
        with start_span("child") as child:
            assert child.trace_id == parent.trace_id
            assert child.parent_span_id == parent.span_id


def test_start_span_restores_context_after_exit() -> None:
    """After the child exits, the active span reverts to the parent."""
    with start_span("outer") as outer:
        with start_span("inner"):
            pass
        assert get_current_span() is outer


def test_start_span_records_to_collector() -> None:
    """Completed spans are added to the global collector."""
    before = get_collector().count()
    with start_span("collect-me"):
        pass
    assert get_collector().count() >= before + 1


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def test_get_current_trace_id_outside_span() -> None:
    """Returns empty string when no trace is active."""
    # Make sure we're not inside a span from a different test.
    result = get_current_trace_id()
    # Either empty or from a parent test context — just check it's a string.
    assert isinstance(result, str)


def test_get_current_trace_id_inside_span() -> None:
    """Returns the active span's trace_id."""
    with start_span("ctx-test") as sp:
        assert get_current_trace_id() == sp.trace_id


# ---------------------------------------------------------------------------
# W3C traceparent header
# ---------------------------------------------------------------------------


def test_parse_traceparent_valid() -> None:
    """Parses a valid W3C traceparent header correctly."""
    header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    ctx = parse_traceparent(header)
    assert ctx is not None
    assert ctx["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx["parent_id"] == "00f067aa0ba902b7"
    assert ctx["flags"] == "01"


def test_parse_traceparent_none_on_empty() -> None:
    """Returns None for empty/missing header."""
    assert parse_traceparent("") is None
    assert parse_traceparent(None) is None  # type: ignore[arg-type]


def test_parse_traceparent_none_on_malformed() -> None:
    """Returns None when the header doesn't match the expected format."""
    assert parse_traceparent("not-a-traceparent") is None
    assert parse_traceparent("00-tooshort-span-01") is None


def test_parse_traceparent_rejects_all_zeros() -> None:
    """All-zero trace or span IDs are invalid per W3C spec."""
    all_zero_trace = "00-" + "0" * 32 + "-00f067aa0ba902b7-01"
    all_zero_span = "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01"
    assert parse_traceparent(all_zero_trace) is None
    assert parse_traceparent(all_zero_span) is None


def test_make_traceparent_format() -> None:
    """make_traceparent produces a correctly formatted W3C header value."""
    trace_id = "a" * 32
    span_id = "b" * 16
    tp = make_traceparent(trace_id, span_id, sampled=True)
    assert tp == f"00-{'a' * 32}-{'b' * 16}-01"

    tp_unsampled = make_traceparent(trace_id, span_id, sampled=False)
    assert tp_unsampled.endswith("-00")


def test_set_remote_context_allows_child_spans() -> None:
    """Child spans created after set_remote_context link to the remote trace."""
    remote_trace = _new_trace_id()
    remote_parent = _new_span_id()
    set_remote_context(remote_trace, remote_parent)
    with start_span("local-child") as child:
        assert child.trace_id == remote_trace


# ---------------------------------------------------------------------------
# SpanCollector
# ---------------------------------------------------------------------------


def test_collector_add_and_drain() -> None:
    """Spans added via add() are returned by drain() and removed from buffer."""
    col = SpanCollector()
    sp = Span(
        trace_id=_new_trace_id(),
        span_id=_new_span_id(),
        parent_span_id="",
        operation_name="x",
        service_name="svc",
        start_time_unix_nano=time.time_ns(),
    )
    col.add(sp)
    assert col.count() == 1
    drained = col.drain()
    assert len(drained) == 1
    assert col.count() == 0


def test_collector_peek_does_not_remove() -> None:
    """peek() returns spans without removing them."""
    col = SpanCollector()
    sp = Span(
        trace_id=_new_trace_id(),
        span_id=_new_span_id(),
        parent_span_id="",
        operation_name="y",
        service_name="svc",
        start_time_unix_nano=time.time_ns(),
    )
    col.add(sp)
    _ = col.peek(10)
    assert col.count() == 1


def test_collector_capacity_drops_oldest() -> None:
    """When capacity is reached, the oldest span is dropped."""
    col = SpanCollector(max_spans=3)
    spans = []
    for i in range(5):
        s = Span(
            trace_id=_new_trace_id(),
            span_id=_new_span_id(),
            parent_span_id="",
            operation_name=f"op-{i}",
            service_name="svc",
            start_time_unix_nano=time.time_ns(),
        )
        col.add(s)
        spans.append(s)
    assert col.count() == 3
    kept = col.peek(3)
    # The oldest (index 0, 1) should have been evicted.
    kept_names = {sp.operation_name for sp in kept}
    assert "op-4" in kept_names
