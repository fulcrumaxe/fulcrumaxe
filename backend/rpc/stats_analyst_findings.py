"""RPC handler: stats.analyst_findings

Surfaces the latest run-analyst report findings for the dashboard.
Delegates to backend/stats/analyst_findings.py (read-only).

Response::

    {
      "report_at": "2026-05-20T14:44:44Z" | null,
      "window": {"since": "<ISO>", "until": "<ISO>"} | null,
      "runs_analyzed": 12,
      "by_severity": {
        "high":   [{"category": ..., "severity": ..., "title": ..., "evidence": [...], ...}, ...],
        "medium": [...],
        "low":    [...]
      },
      "total": 5,
      "generated_at": "2026-05-20T15:00:00Z",
      "error": null
    }

When no reports have been written yet, all lists are empty and total=0.
This handler is read-only and contains no side effects.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def handle(params: dict) -> dict:
    """Return the latest analyst findings grouped by severity."""
    from backend.stats.analyst_findings import load  # noqa: PLC0415

    return load()
