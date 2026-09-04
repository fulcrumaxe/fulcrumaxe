"""
FastAPI router — GET /api/events

Migrates the /api/events handler from api.py (lines 2529-2546).

/api/events is a plain JSON GET (polling endpoint), NOT an SSE stream.
It reads the audit trail and returns events in a {events, next_since} envelope.
Polled incrementally via ?since=<seq>.

This route sits before _check_auth in the legacy flow — no bearer token needed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from backend.api import _read_audit_events

router = APIRouter(tags=["api-events"])


@router.get(
    "/api/events",
    summary="Agent activity events (polling)",
    description=(
        "Returns recent audit-trail events as a JSON array. "
        "Use ?since=<seq> for incremental polling. "
        "NOT an SSE stream — plain JSON GET."
    ),
)
def api_events(
    since: int = Query(default=0, description="Return only events with seq > since."),
    limit: int = Query(default=200, description="Max events to return (1-1000)."),
) -> Any:
    """Events feed — mirrors api.py:2529-2546."""
    limit = max(1, min(limit, 1000))
    events = _read_audit_events(since=since, limit=limit)
    next_since = events[-1]["_seq"] if events else since
    wire_events = [{k: v for k, v in e.items() if k != "_seq"} for e in events]
    return {"events": wire_events, "next_since": next_since}
