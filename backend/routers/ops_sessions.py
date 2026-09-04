"""
FastAPI router — session POST endpoints.

Migrates from api.py:
  POST /sessions/start  (line 3848) — start a new session
  POST /sessions/close  (line 3853) — close the current session

Both require bearer auth + RBAC("POST", path).
Delegates all logic to backend.session_manager.SessionManager.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.session_manager import SessionManager
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["ops-sessions"],
    dependencies=[Depends(require_auth)],
)


@router.post(
    "/sessions/start",
    summary="Start a new session",
    description="Starts a new session via SessionManager. Requires authentication.",
    dependencies=[Depends(make_require_rbac("POST", "/sessions/start"))],
)
def ops_sessions_start() -> Any:
    """Sessions start — mirrors api.py:3848-3851."""
    sm = SessionManager()
    session = sm.start_session()
    return session


@router.post(
    "/sessions/close",
    summary="Close the current session",
    description="Closes the active session via SessionManager. Returns 404 if none active.",
    dependencies=[Depends(make_require_rbac("POST", "/sessions/close"))],
)
def ops_sessions_close() -> Any:
    """Sessions close — mirrors api.py:3853-3859."""
    sm = SessionManager()
    closed = sm.close_session()
    if closed is None:
        raise HTTPException(status_code=404, detail="no active session to close")
    return closed
