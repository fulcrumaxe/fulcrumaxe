"""
FastAPI router — replay GET read-only endpoints.

Migrates from api.py:
  GET /replays                     (line 3161) — list recent replay metadata (limit=20)
  GET /replays/status              (line 3165) — active replay state
  GET /replays/{agent_id}/summary  (line 3182) — header + footer only (no content bulk)
  GET /replays/{agent_id}          (line 3188) — full event list for one agent run

All sit after the _check_auth() + _check_rbac("GET", path) gates in the legacy
server, so require_auth + make_require_rbac are applied.

CRITICAL: Tests MUST mock get_active_replay and get_recorder — these touch
real files and state.  Never let them run in unit tests.

Route ordering: fixed paths (/replays, /replays/status) MUST be registered
BEFORE the parametric routes (/replays/{agent_id}/summary, /replays/{agent_id})
so FastAPI doesn't capture them as agent_id values.  FastAPI matches in
registration order.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.replay import get_active_replay, get_recorder
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["replays-get"],
    dependencies=[Depends(require_auth)],
)

# ---------------------------------------------------------------------------
# Fixed paths first — must come before /replays/{agent_id}
# ---------------------------------------------------------------------------


@router.get(
    "/replays",
    summary="List recent replay metadata",
    description="Returns metadata for up to 20 recent replays.",
    dependencies=[Depends(make_require_rbac("GET", "/replays"))],
)
def replays_list() -> Any:
    """Replay list — mirrors api.py:3161-3163."""
    replays = get_recorder().list_replays()
    return {"replays": replays}


@router.get(
    "/replays/status",
    summary="Active replay state",
    description="Returns current replay engine status, or {active: false} if none running.",
    dependencies=[Depends(make_require_rbac("GET", "/replays/status"))],
)
def replays_status() -> Any:
    """Replay status — mirrors api.py:3165-3170."""
    eng = get_active_replay()
    if eng is None or not eng.is_alive:
        return {"active": False}
    return eng.get_status()


# ---------------------------------------------------------------------------
# Parametric paths — must come AFTER the fixed paths above
# ---------------------------------------------------------------------------


@router.get(
    "/replays/{agent_id}/summary",
    summary="Replay summary for an agent",
    description="Returns header + footer events only (no content bulk). 404 if not found.",
    dependencies=[Depends(make_require_rbac("GET", "/replays/{agent_id}/summary"))],
)
def replays_summary(agent_id: str) -> Any:
    """Replay summary — mirrors api.py:3182-3187."""
    result = get_recorder().get_summary(agent_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no replay found for agent_id '{agent_id}'")
    return result


@router.get(
    "/replays/{agent_id}",
    summary="Full event list for one agent replay",
    description="Returns all recorded events for the given agent_id. 404 if not found.",
    dependencies=[Depends(make_require_rbac("GET", "/replays/{agent_id}"))],
)
def replays_get(agent_id: str) -> Any:
    """Replay full events — mirrors api.py:3188-3193."""
    events = get_recorder().get_replay(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"no replay found for agent_id '{agent_id}'")
    return {"agent_id": agent_id, "events": events}
