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
        "fd_mode": str          -- "r" | "w" | "rw"
      },
      ...
    ],
    "checked_at": "<ISO8601>",  -- when the lsof snapshot was taken
    "warning": str | null       -- non-null when lsof unavailable
  }
"""
from __future__ import annotations

from datetime import datetime, timezone


def handle(params: dict) -> dict:  # noqa: ARG001
    from backend.stats.duckdb_writers import get_duckdb_writers  # noqa: PLC0415

    writers, warning = get_duckdb_writers()
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "writers": writers,
        "checked_at": checked_at,
        "warning": warning,
    }
