"""RPC handler: stats.freshness_list

Exposes the stats freshness watchdog result to the dashboard.
Returns all metric rows with their age so the dashboard can render
a stale-metrics banner.

Response:
  {
    "rows": [{"metric_name": str, "last_ts": str, "age_seconds": int}, ...],
    "warn_age_seconds": int,
    "bug_age_seconds": int
  }
"""
import os
import sys


def handle(params: dict) -> dict:
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from stats_freshness_watchdog import (
        check,
        WARN_AGE_SECONDS,
        BUG_AGE_SECONDS,
    )
    rows = check()
    return {
        "rows": rows,
        "warn_age_seconds": WARN_AGE_SECONDS,
        "bug_age_seconds": BUG_AGE_SECONDS,
    }
