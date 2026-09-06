"""backend/stats/spawn_total_today.py — the dashboard's "today" agent count, and its honesty.

This exists because the number it replaces was confidently wrong and had no way
to say so. The old reader counted ``spawn``-typed rows in the agent feed at
``.autonomous-team/agent-feed.jsonl``, and fell back to scanning the audit log
when that count came to zero. Three separate problems, all measured on the
operator host:

1. **It covered one spawn lane.** Only ``scripts/pre-spawn-check.sh`` writes a
   ``spawn_attempt`` row, and only ``scripts/spawn-agent.sh`` calls it. Agents
   started through the ``Agent()`` tool write nothing there. Measured
   2026-09-06T15:09Z: the feed's newest spawn-family row was 8h47m old and the
   old reader answered **19**, while 86 runs across 11 roles were recorded for
   the same day in ``agent_run``.
2. **A dead source looked like a quiet day.** The fallback fired on
   ``total_today == 0``, so "the writer has been silent since morning" and "no
   agents ran" produced the same code path and the same number.
3. **A read error was a zero.** The file loop sat inside ``except Exception:
   pass``, so an unreadable or truncated feed returned a partial count as if it
   were complete.

Source
------
``agent_run`` in ``stats.duckdb``, not the feed. It is the only store that sees
both lanes: ``spawn-agent.sh`` inserts at spawn time via ``start_run()``, and
``Agent()``-lane agents land through ``complete_run()``. Its **known gap** is
the other side of that: an ``Agent()``-lane agent gets its row when the run is
recorded, so agents still in flight are not counted yet. That undercount is
bounded by concurrency — a handful — against the feed's open-ended hole.

It is also cheaper for a polled endpoint: one aggregate query with a day
predicate, rather than a linear scan of a feed file that was 340 KB and 2043
rows when this was written.

"Today" is the UTC day. ``start_ts`` is stored UTC, and the rest of this system
stamps UTC; the old reader compared a *local* ``date.today()`` against UTC
timestamp strings, which is a fourth way to get the wrong number near midnight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

#: Name reported alongside every answer, so a reader knows what was counted.
SOURCE = "agent_run"

# How old the newest recorded run may be before this number is reported as
# unknown rather than as a count.
#
# Justified against the real gap distribution between consecutive ``agent_run``
# rows, operator host, 14 days to 2026-09-06 (476 rows):
#
#     p50 4m   p90 21m   p95 40m   p99 2h08m
#     every gap over 1h: 1.1 1.1 1.4 1.6 1.7 1.7 1.8 1.8 1.8 2.1 | 11.6 16.6 25.1 207.3  (hours)
#
# There is a clean break in that tail. Within-session gaps top out at 2.1h; the
# next gap up is 11.6h, and the four above it are overnight or offline
# stretches. 3h sits in the gap: above every silence a working session has
# actually produced, below every silence that meant the team was not running.
#
# It would have caught both observed failures — the 17h spawn-event gap this
# module was written for, and the 8h47m one still open when it was written.
#
# The error is deliberately asymmetric. During a genuine overnight idle this
# reports unknown when the source is merely quiet, which costs nothing: the
# honest answer after eleven silent hours really is "I cannot tell whether this
# source is still alive". Reporting a confident number from a dead source is the
# failure being fixed, and it is the one nobody notices.
STALE_AFTER_SECONDS = 3 * 60 * 60


@dataclass(frozen=True)
class TotalToday:
    """Either a count, or an explicit unknown that says why.

    ``count`` is ``None`` exactly when ``reason`` is set. There is no third
    state, and a partial read is never reported as a count.
    """

    count: int | None
    source: str
    newest_ts: str | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "count": self.count,
            "source": self.source,
            "newestTs": self.newest_ts,
        }
        if self.reason is not None:
            d["reason"] = self.reason
        return d


def _unknown(reason: str, newest_ts: str | None = None) -> TotalToday:
    return TotalToday(count=None, source=SOURCE, newest_ts=newest_ts, reason=reason)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _humanize(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def total_today(now: datetime | None = None, db_path: Path | None = None) -> TotalToday:
    """Count runs recorded for the current UTC day, or explain why we cannot.

    Every failure to read the source — missing store, missing driver, locked
    file, malformed schema — returns an unknown carrying the reason. None of
    them returns zero.
    """
    now = now or datetime.now(timezone.utc)
    if db_path is None:
        from backend import state_paths  # noqa: PLC0415

        db_path = state_paths.STATS_DB

    if not Path(db_path).exists():
        return _unknown(f"no metrics store at {db_path}")

    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return _unknown("duckdb driver not installed")

    day_start = datetime.combine(now.astimezone(timezone.utc).date(), time.min, timezone.utc)

    conn = None
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE start_ts >= ?), max(start_ts) FROM agent_run",
            [day_start],
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 — the reason is the product here
        return _unknown(f"{type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110 — close failure says nothing useful
                pass

    if row is None:
        return _unknown("metrics store returned no result")

    today_count, newest = row
    if newest is None:
        return _unknown("no runs recorded in the metrics store")

    from backend.stats.freshness import to_utc  # noqa: PLC0415

    newest_utc = to_utc(newest)
    newest_iso = _iso(newest_utc)
    age = (now - newest_utc).total_seconds()
    if age > STALE_AFTER_SECONDS:
        return _unknown(
            f"stale — newest recorded run is {_humanize(age)} old "
            f"(threshold {_humanize(STALE_AFTER_SECONDS)})",
            newest_ts=newest_iso,
        )

    return TotalToday(count=int(today_count or 0), source=SOURCE, newest_ts=newest_iso)


__all__ = ["SOURCE", "STALE_AFTER_SECONDS", "TotalToday", "total_today"]
