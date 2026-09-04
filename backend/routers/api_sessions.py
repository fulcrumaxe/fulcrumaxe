"""
FastAPI router — session GET endpoints.

Migrates from api.py:
  GET /api/sessions/current  (line 2480) — current session
  GET /api/sessions          (line 2503) — list sessions

/api/sessions/current has special auth logic:
  - Loopback callers → allowed without a token
  - Remote callers → must present a valid bearer token
  - If AF_API_AUTH_KEY is not set AND caller is not loopback → 401

This is preserved verbatim from the legacy handler.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from backend.deps.auth import _extract_bearer, _get_auth_key
import hmac

router = APIRouter(tags=["api-sessions"])

_LOOPBACK_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(request: Request) -> bool:
    """Return True when the direct connecting IP is a loopback address."""
    if request.client is None:
        return False
    return request.client.host in _LOOPBACK_IPS


@router.get(
    "/api/sessions/current",
    summary="Current session",
    description=(
        "Returns the current dev session object. "
        "Loopback callers are allowed without a token. "
        "Remote callers require a valid bearer token."
    ),
)
def api_sessions_current(request: Request) -> Any:
    """Current session — mirrors api.py:2480-2501.

    Auth logic:
    - Loopback → always pass (the React app on localhost)
    - Remote + no key configured → 401 (CWE-306 protection)
    - Remote + key configured + correct token → pass
    - Remote + key configured + wrong/missing token → 401/403
    """
    if not _is_loopback(request):
        key = _get_auth_key()
        if key is None:
            # No auth key configured but non-loopback → deny (CWE-306)
            raise HTTPException(status_code=401, detail="unauthorized")
        token = _extract_bearer(request)
        if token is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        if not hmac.compare_digest(token, key):
            raise HTTPException(status_code=403, detail="forbidden")

    now = _dt.datetime.now(_dt.UTC)
    session = {
        "id": "dev-session",
        "userId": "11111111-1111-1111-1111-111111111111",
        "username": "dev",
        "avatarUrl": "",
        "createdAt": now.isoformat(),
        "expiresAt": (now + _dt.timedelta(hours=24)).isoformat(),
    }
    return session


@router.get(
    "/api/sessions",
    summary="List sessions",
    description="Returns an empty sessions list (stub for React SPA compatibility).",
)
def api_sessions() -> Any:
    """Sessions list — mirrors api.py:2503-2504."""
    return {"sessions": []}
