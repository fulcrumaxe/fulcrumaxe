"""
FastAPI router — bare /sessions GET endpoints.

Migrates from api.py:
  GET /sessions             (line 3117) — list sessions
  GET /sessions/current     (line 3121) — current active session
  GET /sessions/compare     (line 3129) — compare two sessions by ?a=&b= params
  GET /sessions/{id}        (line 3149) — fetch a single session by ID

All four sit behind the standard auth + RBAC gate (legacy api.py:2671-2675).
Logic is delegated verbatim to SessionManager — no reimplementation.

Note: /api/sessions and /api/sessions/current are a separate router
(backend/routers/api_sessions.py) with different auth semantics.
These bare /sessions/* routes require bearer auth for all callers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from urllib.parse import parse_qs, urlparse

from backend.session_manager import SessionManager
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["sessions-get"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/sessions",
    summary="List sessions",
    description="Returns a list of all sessions (most recent first, up to 20). Requires authentication.",
    dependencies=[Depends(make_require_rbac("GET", "/sessions"))],
)
def sessions_list() -> Any:
    """Session list — mirrors api.py:3117-3119."""
    sm = SessionManager()
    return {"sessions": sm.list_sessions()}


@router.get(
    "/sessions/current",
    summary="Current active session",
    description="Returns the currently active session, or 404 if none is active. Requires authentication.",
    dependencies=[Depends(make_require_rbac("GET", "/sessions/current"))],
)
def sessions_current() -> Any:
    """Current session — mirrors api.py:3121-3127."""
    sm = SessionManager()
    session = sm.current_session()
    if session is None:
        raise HTTPException(status_code=404, detail="no active session")
    return session


@router.get(
    "/sessions/compare",
    summary="Compare two sessions",
    description=(
        "Compares two sessions by ID. "
        "Query params 'a' and 'b' are required session IDs. "
        "Returns 400 if either is missing, 404 if either session is not found. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/sessions/compare"))],
)
def sessions_compare(request: Request) -> Any:
    """Session compare — mirrors api.py:3129-3147."""
    parsed_url = urlparse(str(request.url))
    qs: dict[str, str] = {}
    if parsed_url.query:
        for part in parsed_url.query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                qs[k] = v
    id_a = qs.get("a", "")
    id_b = qs.get("b", "")
    if not id_a or not id_b:
        raise HTTPException(status_code=400, detail="query params 'a' and 'b' are required")
    sm = SessionManager()
    try:
        result = sm.compare_sessions(id_a, id_b)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/sessions/{session_id}",
    summary="Get a session by ID",
    description="Returns a single session by its ID. Returns 404 if not found. Requires authentication.",
    dependencies=[Depends(make_require_rbac("GET", "/sessions/{session_id}"))],
)
def sessions_get_by_id(session_id: str) -> Any:
    """Session by ID — mirrors api.py:3149-3159."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    sm = SessionManager()
    session = sm.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session '{session_id}' not found")
    return session
