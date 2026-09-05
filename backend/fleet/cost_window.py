"""backend/fleet/cost_window.py — the calendar window the fleet cost panel
actually means (D#2317 PR-b).

Background
----------
``cost_summary.json`` stores one entry per UTC calendar date. Two things
used to be computed off it that did not match their own labels:

  - the writer pruned with ``entries[-7:]`` *after* sorting by date, which
    keeps the last seven **written** entries regardless of age. On the
    operator's host that produced a file spanning 2026-08-17 to 2026-09-04
    — 19 days — every one of which was summed into a figure the UI called
    "Last 7d".
  - the reader summed the whole file for the 7d figure and reported "the
    entry whose date == today (UTC)" as the 24h figure. That second one is
    a calendar-day-to-date total, not a rolling 24 hours: it resets at
    00:00 UTC, which is 20:00 local on the operator's host, so every
    evening the panel read near-zero through active work.

This module is the one place that decides which dates are inside the
window, so the writer's prune and the reader's totals can't drift apart
again.

Choice made for the 24h figure (D#2317 PR-b item 6): the stored data has
**calendar-date granularity only** — there are no hourly buckets to sum —
so a true rolling 24-hour figure is not computable from this file without
changing its schema. Rather than keep a number whose label lies, the value
is named for what it actually is (``tokens_today_utc``) and the UI labels
it "Today (UTC)". Nothing on the page claims "24h" any more.

No-signal vs. measured zero
---------------------------
``summarize()`` returns ``None`` — never ``0`` — for every total when the
entry list holds nothing inside the window. That case means "this project
has reported no spend we can see", which is exactly the condition that
produced the original bug report: the panel read ``cost_summary.json`` for
seven dead fixtures and printed their stale totals as the fleet's spend,
while the serving project's file (holding 1.9M tokens that day) was never
opened at all. A project with *some* in-window entry but none dated today
does get a real ``0`` for today — the writer appends on every completed
agent run, so an absent today entry there is a measurement, not a gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# Seven UTC calendar dates, ending at and including today.
WINDOW_DAYS = 7

_DATE_FMT = "%Y-%m-%d"


def today_utc(now: datetime | None = None) -> str:
    """Return today's UTC calendar date as ``YYYY-MM-DD``."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(_DATE_FMT)


def window_dates(today: str) -> list[str]:
    """Return the ``WINDOW_DAYS`` UTC dates ending at (and including) *today*.

    Ascending, so ``window_dates(t)[0]`` is ``today - (WINDOW_DAYS - 1)``.
    """
    end = datetime.strptime(today, _DATE_FMT).replace(tzinfo=timezone.utc)
    return [
        (end - timedelta(days=offset)).strftime(_DATE_FMT)
        for offset in range(WINDOW_DAYS - 1, -1, -1)
    ]


def entries_in_window(entries: Iterable[Any], today: str) -> list[dict[str, Any]]:
    """Return the subset of *entries* dated inside the window, date-ascending.

    An entry whose ``date`` is missing, malformed, or older than
    ``today - (WINDOW_DAYS - 1)`` is dropped: it can't be placed in the
    window, so it must not contribute to a window total.
    """
    window = set(window_dates(today))
    kept = [e for e in entries if isinstance(e, dict) and e.get("date") in window]
    kept.sort(key=lambda e: e["date"])
    return kept


def billable_tokens(entry: dict[str, Any]) -> int:
    """Billable tokens for one entry: input + output.

    cache_read tokens are free under Anthropic pricing and are not stored
    here in the first place. A non-numeric field counts as 0 rather than
    taking down the panel.
    """
    total = 0
    for key in ("input_tokens", "output_tokens"):
        try:
            total += int(entry.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def summarize(entries: Iterable[Any], now: datetime | None = None) -> dict[str, Any]:
    """Summarise *entries* over the window ending today (UTC).

    Returns ``tokens_today_utc`` / ``tokens_7d`` / ``projected_eod_tokens``
    as ints when there is at least one in-window entry, and ``None`` for
    all three when there is not — a project we have no observation for
    reports no signal, not a zero.

    ``tokens_today_utc <= tokens_7d`` holds by construction: today is
    always inside the window, so today's entry is one of the entries the
    7d total sums.
    """
    today = today_utc(now)
    in_window = entries_in_window(entries, today)

    if not in_window:
        return {
            "tokens_today_utc": None,
            "tokens_7d": None,
            "projected_eod_tokens": None,
            "by_day": [],
        }

    tokens_7d = sum(billable_tokens(e) for e in in_window)
    tokens_today = sum(billable_tokens(e) for e in in_window if e["date"] == today)

    # Linear extrapolation of today's spend across the remaining UTC hours.
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hours_elapsed = stamp.hour + stamp.minute / 60.0
    projected_eod = int(tokens_today / hours_elapsed * 24) if hours_elapsed > 0 else tokens_today

    return {
        "tokens_today_utc": tokens_today,
        "tokens_7d": tokens_7d,
        "projected_eod_tokens": projected_eod,
        "by_day": in_window,
    }
