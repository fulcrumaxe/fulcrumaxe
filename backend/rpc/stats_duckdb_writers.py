"""RPC handler: stats_duckdb_writers

Return a list of processes currently holding an open FD on stats.duckdb,
so the dashboard can surface lock-holder visibility at a glance.

Response shape:
  {
    "writers": [
      {
        "pid": int,
        "cmd": str,
        "age_seconds": float | null,
        "fd_mode": str | null   -- "r" | "w" | "rw"; null = mode unknown
      },
      ...
    ],
    "checked_at": "<ISO8601>",    -- when the snapshot was taken
    "warning": str | null,        -- non-null ONLY when no source could answer
    "source": str | null,         -- "proc" | "lsof" | null
    "inspected_pids": int | null, -- pids whose fd table was read in full
    "uninspected_pids": int | null,  -- pids that could not be inspected;
                                     -- null means the source cannot count them
    "capped": bool                -- true when the scan was cut short
  }

Three states the consumer must keep apart (D#2326):
  * writers non-empty                       → these processes hold the file
  * writers empty, warning null, uninspected 0
                                            → determined: genuinely no writers
  * warning non-null                        → undetermined: nothing was measured
  * uninspected_pids > 0                    → partial: what was found, plus
                                              a count of what was not looked at

Collapsing the undetermined or partial case into the determined-empty one
would report a confident zero that was never measured.
"""
from __future__ import annotations

from datetime import datetime, timezone


def handle(params: dict) -> dict:  # noqa: ARG001
    from backend.stats.duckdb_writers import get_duckdb_writers  # noqa: PLC0415

    writers, warning, meta = get_duckdb_writers()
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "writers": writers,
        "checked_at": checked_at,
        "warning": warning,
        "source": meta["source"],
        "inspected_pids": meta["inspected_pids"],
        "uninspected_pids": meta["uninspected_pids"],
        "capped": meta["capped"],
    }
