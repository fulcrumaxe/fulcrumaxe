"""loop_metrics_ts.py -- shared timestamp parsing for loop-metrics.jsonl readers.

Background (D#2315)
--------------------
`.autonomous-team/loop-metrics.jsonl` rows are supposed to carry a
"timestamp" (or legacy "ts") field as an ISO-8601 string. At least one row
in the wild -- of unknown, non-live-writer provenance -- carries a raw epoch
**int** instead:

    {"ts": 1784925063, "iso": "2026-07-24T20:31:00Z", "event_count": 3, ...}

Before this module existed, three readers of that same file each handled
that shape differently: one raised `AttributeError` straight into an RPC
caller (`backend/stats_writer.py:loop_idle_ratio_24h`), one silently zeroed
a freshness comparison with no signal (`backend/health_monitor.py`), and one
passed the raw int through into a payload typed as a string
(`backend/server.py`'s `loop.timeline`). Only `backend/run_analyst.py`
(D#1753) got it right: skip the row, but say so on stderr.

This module is the one shared implementation. Policy: **skip, don't
recover.** It would be tempting to fall back to the row's `iso` field, which
looks correct here -- don't. No live writer produces a row shaped like this
(both writers of this file always emit an ISO string `timestamp`), so `ts`
and `iso` agreeing is a guess about a row of unknown provenance. Recovering
it would also make this reader and `run_analyst.load_loop_metrics` (whose
D#1753 tests assert *skip*, not recover) disagree about which rows exist in
the same file.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any


def parse_loop_metrics_ts(ts_value: Any) -> "datetime | None":
    """Parse a single raw timestamp value from a loop-metrics.jsonl row.

    Returns a timezone-aware ``datetime`` on success. Returns ``None`` --
    never raises -- when the value is missing, empty, not a string (e.g. an
    epoch int), or fails to parse as ISO-8601. Callers are expected to skip
    the row and report the skip via :func:`report_skipped_row`.

    Uses stdlib ``datetime.fromisoformat`` (after normalizing a trailing
    "Z" to "+00:00") rather than a hand-rolled format list or a
    ``python-dateutil`` fallback -- the Spec's "no new runtime dependency"
    constraint rules dateutil out, and dateutil is in fact not installed in
    this environment (its import would silently no-op behind a broad
    except, which is worse than not having the fallback at all). This
    matches every real timestamp shape seen across this file's readers and
    their existing tests: the "Z"-suffixed strings both live writers emit
    (D#1753's fixtures use this form) and the "+00:00"-offset strings
    ``datetime.isoformat()`` produces (several `backend/tests/test_stats_writer.py`
    fixtures use this form).
    """
    if not isinstance(ts_value, str) or not ts_value:
        return None
    try:
        dt = datetime.fromisoformat(ts_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def row_ts(row: dict) -> Any:
    """Extract the raw (unparsed) timestamp value out of a loop-metrics row.

    Prefers ``"timestamp"``, falls back to the legacy ``"ts"`` field name.
    Returns ``None`` when neither is present or the value is an empty
    string -- callers should treat that as "no timestamp on this row" and
    skip quietly, with no diagnostic (that's a normal, expected shape, not
    the malformed-row case this module exists for).
    """
    raw = row.get("timestamp")
    if raw is None:
        raw = row.get("ts")
    if raw is None or raw == "":
        return None
    return raw


def report_skipped_row(filename: str, lineno: "int | None", raw_ts: Any, *, prefix: str = "") -> None:
    """Print a one-line diagnostic to stderr for a row skipped because its
    timestamp value was present but unparseable.

    Loud, not silent: the D#2315 Spec requires every reader to say when and
    where it skipped a row, in the same ``<file>:<lineno>`` shape
    ``backend/run_analyst.py`` already emits (D#1753).
    """
    label = f"{prefix}: " if prefix else ""
    where = f"{filename}:{lineno}" if lineno is not None else filename
    print(
        f"{label}skipping malformed row at {where} (unparseable timestamp: {raw_ts!r})",
        file=sys.stderr,
    )
