"""
OTLP JSON export for collected spans — zero external dependencies.

Converts internal Span objects into the OpenTelemetry Protocol (OTLP) JSON
ResourceSpans format, writes them to rotating files under
.autonomous-team/traces/, and optionally echoes them to stdout for piping
into otel-collector or Jaeger.

Usage:
    from backend.trace_export import export_spans, TraceExporter

    # One-shot pure conversion (useful for testing):
    otlp_payload = export_spans(list_of_spans)

    # Continuous rotating exporter (call flush() periodically):
    exporter = TraceExporter(traces_dir=Path(".autonomous-team/traces"))
    exporter.flush()   # drains the global collector and writes to disk
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

from backend.tracing import Span, get_collector

# ---------------------------------------------------------------------------
# Service metadata
# ---------------------------------------------------------------------------

_SERVICE_NAME = "fulcrumaxe"

try:
    # Attempt to read a version from a VERSION file if it exists, else fall back.
    _VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
    _SERVICE_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
except OSError:
    _SERVICE_VERSION = "0.0.0"


# ---------------------------------------------------------------------------
# Pure conversion function (testable without file I/O)
# ---------------------------------------------------------------------------


def export_spans(spans: Sequence[Span]) -> dict:
    """
    Convert a sequence of Span objects into an OTLP JSON ResourceSpans payload.

    The returned dict is suitable for json.dumps() and direct submission to an
    OTLP/HTTP receiver at /v1/traces.

    Args:
        spans: Completed Span objects to serialise.

    Returns:
        A dict matching the OTLP ResourceSpans schema::

            {
              "resourceSpans": [
                {
                  "resource": {"attributes": [...]},
                  "scopeSpans": [
                    {
                      "scope": {"name": "fulcrumaxe"},
                      "spans": [...]
                    }
                  ]
                }
              ]
            }
    """
    otlp_spans = []
    for span in spans:
        otlp_span: dict = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "parentSpanId": span.parent_span_id,
            "name": span.operation_name,
            "kind": 1,  # SPAN_KIND_INTERNAL (default)
            "startTimeUnixNano": str(span.start_time_unix_nano),
            "endTimeUnixNano": str(span.end_time_unix_nano),
            "status": {"code": _status_code(span.status)},
            "attributes": _kv_list(span.attributes),
            "events": [
                {
                    "name": e["name"],
                    "timeUnixNano": str(e["time_unix_nano"]),
                    "attributes": _kv_list(e.get("attributes", {})),
                }
                for e in span.events
            ],
        }
        otlp_spans.append(otlp_span)

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _kv_list(
                        {
                            "service.name": _SERVICE_NAME,
                            "service.version": _SERVICE_VERSION,
                        }
                    )
                },
                "scopeSpans": [
                    {
                        "scope": {"name": _SERVICE_NAME},
                        "spans": otlp_spans,
                    }
                ],
            }
        ]
    }


def _status_code(status: str) -> int:
    """Map span status string to OTLP status code integer."""
    return {"UNSET": 0, "OK": 1, "ERROR": 2}.get(status, 0)


def _kv_list(attributes: dict) -> list:
    """
    Convert a plain dict into an OTLP KeyValue list.

    Strings, booleans, integers, and floats are mapped to their OTLP
    value types. Everything else is coerced to a string.
    """
    result = []
    for k, v in attributes.items():
        if isinstance(v, bool):
            kv = {"key": str(k), "value": {"boolValue": v}}
        elif isinstance(v, int):
            kv = {"key": str(k), "value": {"intValue": str(v)}}
        elif isinstance(v, float):
            kv = {"key": str(k), "value": {"doubleValue": v}}
        elif isinstance(v, str):
            kv = {"key": str(k), "value": {"stringValue": v}}
        else:
            kv = {"key": str(k), "value": {"stringValue": str(v)}}
        result.append(kv)
    return result


# ---------------------------------------------------------------------------
# Rotating file writer
# ---------------------------------------------------------------------------

_MAX_SPANS_PER_FILE = 1000
_MAX_FILES_RETAINED = 10


class TraceExporter:
    """
    Periodic exporter that drains the global SpanCollector and writes
    completed spans to rotating JSON files in *traces_dir*.

    File naming: ``traces-{timestamp}.json``  (one file per flush batch when
    the span count crosses _MAX_SPANS_PER_FILE, otherwise appended).

    Rotation strategy:
      - When the current working file reaches _MAX_SPANS_PER_FILE spans, a new
        file is started on the next flush.
      - Only the _MAX_FILES_RETAINED most-recent files are kept; older files
        are deleted automatically.

    Thread safety: a single lock serialises flush() calls — safe to call from
    multiple threads, e.g. the HTTP handler thread and a background flusher.
    """

    def __init__(
        self,
        traces_dir: Path | None = None,
        stdout_export: bool = False,
    ) -> None:
        """
        Args:
            traces_dir:     Directory to write trace files.  Defaults to
                            ``.autonomous-team/traces`` relative to repo root.
            stdout_export:  If True, also write OTLP JSON to stdout — useful
                            for piping to ``otel-collector``.
        """
        if traces_dir is None:
            _repo_root = Path(__file__).resolve().parent.parent
            traces_dir = _repo_root / ".autonomous-team" / "traces"
        self._dir = Path(traces_dir)
        self._stdout = stdout_export
        self._lock = threading.Lock()
        self._current_file: Path | None = None
        self._current_file_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def flush(self) -> int:
        """
        Drain the global SpanCollector and persist all completed spans.

        Returns the number of spans written in this flush.
        """
        spans = get_collector().drain()
        if not spans:
            return 0
        with self._lock:
            self._write(spans)
        return len(spans)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write(self, spans: list[Span]) -> None:
        """Write *spans* to disk (and optionally stdout). Called with self._lock held."""
        self._dir.mkdir(parents=True, exist_ok=True)

        remaining = spans
        while remaining:
            capacity = _MAX_SPANS_PER_FILE - self._current_file_count
            if capacity <= 0 or self._current_file is None:
                self._rotate()
                capacity = _MAX_SPANS_PER_FILE

            batch = remaining[:capacity]
            remaining = remaining[capacity:]

            payload = export_spans(batch)
            with self._current_file.open("a", encoding="utf-8") as fh:  # type: ignore[union-attr]
                fh.write(json.dumps(payload, default=str) + "\n")
            self._current_file_count += len(batch)

            if self._stdout:
                sys.stdout.write(json.dumps(payload, default=str) + "\n")
                sys.stdout.flush()

        self._prune_old_files()

    def _rotate(self) -> None:
        """Start a new trace file. Uses nanosecond timestamp to avoid name collisions."""
        ts = time.time_ns()
        self._current_file = self._dir / f"traces-{ts}.json"
        self._current_file_count = 0

    def _prune_old_files(self) -> None:
        """Delete oldest trace files beyond the retention limit."""
        try:
            files = sorted(self._dir.glob("traces-*.json"))
            excess = len(files) - _MAX_FILES_RETAINED
            for old_file in files[:excess]:
                try:
                    old_file.unlink()
                except OSError:
                    pass
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Process-global singleton exporter
# ---------------------------------------------------------------------------

_exporter_lock = threading.Lock()
_global_exporter: TraceExporter | None = None


def get_exporter() -> TraceExporter:
    """Return the process-global TraceExporter, creating it on first call."""
    global _global_exporter  # noqa: PLW0603
    if _global_exporter is None:
        with _exporter_lock:
            if _global_exporter is None:
                _global_exporter = TraceExporter()
    return _global_exporter
