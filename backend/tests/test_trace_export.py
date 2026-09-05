"""
Unit tests for backend.trace_export — OTLP JSON serialisation and file rotation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.tracing import Span, _new_span_id, _new_trace_id
from backend.trace_export import (
    TraceExporter,
    _kv_list,
    _status_code,
    export_spans,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_span(
    operation: str = "test-op",
    status: str = "OK",
    attributes: dict | None = None,
) -> Span:
    now = time.time_ns()
    return Span(
        trace_id=_new_trace_id(),
        span_id=_new_span_id(),
        parent_span_id="",
        operation_name=operation,
        service_name="autonomous-forever",
        start_time_unix_nano=now - 1_000_000,
        end_time_unix_nano=now,
        status=status,
        attributes=attributes or {},
    )


# ---------------------------------------------------------------------------
# export_spans — pure conversion function
# ---------------------------------------------------------------------------


def test_export_spans_returns_resource_spans_structure() -> None:
    """Top-level key is 'resourceSpans' containing exactly one resource entry."""
    spans = [_make_span()]
    result = export_spans(spans)
    assert "resourceSpans" in result
    assert len(result["resourceSpans"]) == 1
    rs = result["resourceSpans"][0]
    assert "resource" in rs
    assert "scopeSpans" in rs


def test_export_spans_resource_attributes_include_service_name() -> None:
    """Resource attributes always include service.name."""
    result = export_spans([_make_span()])
    attrs = result["resourceSpans"][0]["resource"]["attributes"]
    keys = {item["key"] for item in attrs}
    assert "service.name" in keys


def test_export_spans_span_fields_present() -> None:
    """Each exported span includes required OTLP fields."""
    sp = _make_span(operation="my-op", status="OK")
    result = export_spans([sp])
    otlp_span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert otlp_span["traceId"] == sp.trace_id
    assert otlp_span["spanId"] == sp.span_id
    assert otlp_span["name"] == "my-op"
    assert "startTimeUnixNano" in otlp_span
    assert "endTimeUnixNano" in otlp_span
    assert "status" in otlp_span


def test_export_spans_empty_list() -> None:
    """export_spans handles an empty span list gracefully."""
    result = export_spans([])
    spans = result["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans == []


def test_export_spans_events_serialised() -> None:
    """Span events appear in the exported output."""
    sp = _make_span()
    sp.add_event("my-event", {"key": "val"})
    result = export_spans([sp])
    otlp_events = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"]
    assert len(otlp_events) == 1
    assert otlp_events[0]["name"] == "my-event"


# ---------------------------------------------------------------------------
# _status_code
# ---------------------------------------------------------------------------


def test_status_code_mapping() -> None:
    """Status strings map to correct OTLP integer codes."""
    assert _status_code("UNSET") == 0
    assert _status_code("OK") == 1
    assert _status_code("ERROR") == 2
    assert _status_code("unknown") == 0  # fallback


# ---------------------------------------------------------------------------
# _kv_list
# ---------------------------------------------------------------------------


def test_kv_list_string_value() -> None:
    """String values use stringValue."""
    kv = _kv_list({"key": "hello"})
    assert kv[0]["value"]["stringValue"] == "hello"


def test_kv_list_int_value() -> None:
    """Integer values use intValue (as string per OTLP spec)."""
    kv = _kv_list({"n": 42})
    assert kv[0]["value"]["intValue"] == "42"


def test_kv_list_bool_value() -> None:
    """Boolean values use boolValue."""
    kv = _kv_list({"flag": True})
    assert kv[0]["value"]["boolValue"] is True


def test_kv_list_float_value() -> None:
    """Float values use doubleValue."""
    kv = _kv_list({"ratio": 0.5})
    assert kv[0]["value"]["doubleValue"] == pytest.approx(0.5)


def test_kv_list_fallback_to_string() -> None:
    """Unknown types are stringified."""
    kv = _kv_list({"obj": [1, 2, 3]})
    assert isinstance(kv[0]["value"]["stringValue"], str)


# ---------------------------------------------------------------------------
# TraceExporter — file I/O
# ---------------------------------------------------------------------------


def test_exporter_flush_writes_file(tmp_path: Path) -> None:
    """flush() creates a trace file when spans are available."""
    from backend.tracing import get_collector, start_span

    # Generate a real span so the collector has something to drain.
    with start_span("export-test"):
        pass

    exporter = TraceExporter(traces_dir=tmp_path)
    n = exporter.flush()
    assert n >= 1
    files = list(tmp_path.glob("traces-*.json"))
    assert len(files) >= 1


def test_exporter_flush_empty_collector_returns_zero(tmp_path: Path) -> None:
    """flush() returns 0 and creates no files when the collector is empty."""
    from backend.tracing import get_collector

    # Drain the collector first so it's empty.
    get_collector().drain()

    exporter = TraceExporter(traces_dir=tmp_path)
    n = exporter.flush()
    assert n == 0
    files = list(tmp_path.glob("traces-*.json"))
    assert len(files) == 0


def test_exporter_rotation(tmp_path: Path) -> None:
    """After _MAX_SPANS_PER_FILE spans, a new file is started."""
    from backend.trace_export import _MAX_SPANS_PER_FILE
    from backend.tracing import get_collector

    get_collector().drain()  # start clean
    spans = [_make_span(f"op-{i}") for i in range(_MAX_SPANS_PER_FILE + 5)]

    exporter = TraceExporter(traces_dir=tmp_path)
    exporter._lock.acquire()
    exporter._write(spans)
    exporter._lock.release()

    files = list(tmp_path.glob("traces-*.json"))
    assert len(files) == 2  # first file full, second file overflow


def test_exporter_prunes_old_files(tmp_path: Path) -> None:
    """Files beyond _MAX_FILES_RETAINED are deleted."""
    from backend.trace_export import _MAX_FILES_RETAINED

    # Create more files than the retention limit.
    for i in range(_MAX_FILES_RETAINED + 3):
        (tmp_path / f"traces-{1000 + i}.json").write_text("{}")

    exporter = TraceExporter(traces_dir=tmp_path)
    exporter._prune_old_files()

    remaining = list(tmp_path.glob("traces-*.json"))
    assert len(remaining) == _MAX_FILES_RETAINED


def test_exporter_stdout_mode(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """With stdout_export=True, OTLP JSON is also written to stdout."""
    from backend.tracing import get_collector, start_span

    get_collector().drain()
    with start_span("stdout-export-test"):
        pass

    exporter = TraceExporter(traces_dir=tmp_path, stdout_export=True)
    exporter.flush()

    captured = capsys.readouterr()
    assert "resourceSpans" in captured.out
