"""
FastAPI router — replay POST endpoints.

Migrates from api.py:
  POST /replays/{agent_id}/start  (line 3905) — start a replay
  POST /replays/pause             (line 3929) — pause the active replay
  POST /replays/resume            (line 3937) — resume the active replay
  POST /replays/stop              (line 3945) — stop the active replay
  POST /replays/seek              (line 3949) — seek to an event number

All require bearer auth + RBAC("POST", path).
Delegates to backend.replay: start_replay, get_active_replay, stop_active_replay.

CRITICAL: Tests MUST mock start_replay, get_active_replay, and
stop_active_replay — these start real replay threads and load
agent feed files.  Never let them run in unit tests.

Route ordering: specific fixed paths (/replays/pause, /replays/resume,
/replays/stop, /replays/seek) MUST be registered BEFORE the parametric
route /replays/{agent_id}/start so FastAPI doesn't capture them as
agent_id values.  FastAPI matches in registration order.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.replay import get_active_replay, start_replay, stop_active_replay
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["ops-replays"],
    dependencies=[Depends(require_auth)],
)

# ---------------------------------------------------------------------------
# Fixed paths first — must come before /replays/{agent_id}/start
# ---------------------------------------------------------------------------

@router.post(
    "/replays/pause",
    summary="Pause the active replay",
    description="Pauses the currently active replay session. Returns 409 if none active.",
    dependencies=[Depends(make_require_rbac("POST", "/replays/pause"))],
)
def ops_replays_pause() -> Any:
    """Replays pause — mirrors api.py:3929-3935."""
    eng = get_active_replay()
    if eng is None or not eng.is_alive:
        raise HTTPException(status_code=409, detail="no active replay to pause")
    eng.pause()
    return {"ok": True}


@router.post(
    "/replays/resume",
    summary="Resume the active replay",
    description="Resumes the currently paused replay session. Returns 409 if none active.",
    dependencies=[Depends(make_require_rbac("POST", "/replays/resume"))],
)
def ops_replays_resume() -> Any:
    """Replays resume — mirrors api.py:3937-3943."""
    eng = get_active_replay()
    if eng is None or not eng.is_alive:
        raise HTTPException(status_code=409, detail="no active replay to resume")
    eng.resume()
    return {"ok": True}


@router.post(
    "/replays/stop",
    summary="Stop the active replay",
    description="Stops the currently active replay session.",
    dependencies=[Depends(make_require_rbac("POST", "/replays/stop"))],
)
def ops_replays_stop() -> Any:
    """Replays stop — mirrors api.py:3945-3947."""
    stopped = stop_active_replay()
    return {"ok": True, "was_active": stopped}


@router.post(
    "/replays/seek",
    summary="Seek to an event number",
    description=(
        "Seeks the active replay to a specific event number. "
        "Body: {\"event_number\": int}. "
        "Returns 409 if no active replay."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/replays/seek"))],
)
async def ops_replays_seek(request: Request) -> Any:
    """Replays seek — mirrors api.py:3949-3963."""
    eng = get_active_replay()
    if eng is None or not eng.is_alive:
        raise HTTPException(status_code=409, detail="no active replay to seek")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    event_number = body.get("event_number")
    if event_number is None:
        raise HTTPException(status_code=400, detail="'event_number' is required")

    try:
        eng.seek(int(event_number))
        return {"ok": True}
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="'event_number' must be an integer")


# ---------------------------------------------------------------------------
# Parametric path — must come AFTER fixed paths above
# ---------------------------------------------------------------------------

@router.post(
    "/replays/{agent_id}/start",
    summary="Start a replay for an agent",
    description=(
        "Starts a replay session for the given agent_id. "
        "Body: {\"speed\"?: str} (default: \"1x\"). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/replays/{agent_id}/start"))],
)
async def ops_replays_start(agent_id: str, request: Request) -> Any:
    """Replays start — mirrors api.py:3905-3927."""
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id required")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    speed = body.get("speed", "1x")

    try:
        eng = start_replay(agent_id, speed=speed)
        return {
            "replay_session_id": eng.replay_session_id,
            "total_events": len(eng._events),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
