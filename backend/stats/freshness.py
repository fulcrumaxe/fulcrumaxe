"""backend/stats/freshness.py — honest arithmetic and scope for the freshness watchdog.

Two small pure helpers, both extracted because the watchdog was getting each
one wrong in a way that made its output unreadable (D#2316 finding 6).

``to_utc`` — ``metric_event.ts`` is a naive ``TIMESTAMP`` holding UTC
wall-clock (``stats_writer.record()`` writes it that way). The watchdog used
to branch on ``hasattr(ts, "astimezone")``, which every ``datetime`` has, so
the naive path never ran and ``naive.astimezone(utc)`` reinterpreted a UTC
value as *local* time — adding the host's offset and stamping the timestamp
into the future. Measured on the operator host (America/New_York): 16 of 17
metrics came back with negative ``age_seconds``, up to -8141s. A staleness
watchdog whose ages point backwards in time cannot detect staleness at all.
On a UTC host the offset is zero, which is why this survived so long.

``is_monitored`` — a metric is monitored when something in the codebase still
writes it. ``stats_writer.registered_metrics()`` already answers exactly that
question and is already maintained for it. ``bootstrap_ping`` is written once
at bootstrap, is absent from that registry, and had been asserting 1243h of
staleness at the top of every dashboard page — a banner that is always on is
a banner nobody reads. Labelling it unmonitored is the honest move; muting
the whole banner would not be, so genuinely stale *monitored* metrics still
flag.
"""

from __future__ import annotations

from datetime import datetime, timezone


def to_utc(ts: datetime) -> datetime:
    """Return ``ts`` as a UTC-aware datetime without shifting its instant.

    A naive value is *labelled* UTC (it already holds UTC wall-clock); an
    aware value is converted. Never reinterprets a naive value as local time.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def is_monitored(metric_name: str) -> bool:
    """True when ``metric_name`` still has a live writer in the codebase.

    Metrics with no registered writer (one-shot markers like ``bootstrap_ping``)
    are reported with their real age but are not asserted stale — nobody can act
    on the staleness of a metric that was never going to be written again.
    """
    from backend import stats_writer  # noqa: PLC0415

    return metric_name in stats_writer.registered_metrics()
