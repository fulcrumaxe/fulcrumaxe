"""
FastAPI router — GET/POST /api/innovate

Migrates the innovate state and mutation handlers from api.py:
  GET  /api/innovate        (line 2645) — innovate toggle state (pre-auth)
  POST /api/innovate/toggle (line 3783) — set enabled/disabled (auth + RBAC)
  POST /api/innovate/tick   (line 3796) — run one innovate tick (auth + RBAC + spawn-guard)
"""

from __future__ import annotations

import os
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api import _innovate_state, _set_innovate, _innovate_tick
from backend.deps.auth import require_auth
from backend.deps.origin_guard import require_not_test_origin
from backend.deps.rbac import make_require_rbac

router = APIRouter(tags=["api-innovate"])


@router.get(
    "/api/innovate",
    summary="Innovate toggle state",
    description="Returns the current state of the innovate toggle.",
)
def api_innovate() -> Any:
    """Innovate state — mirrors api.py:2645-2646."""
    return _innovate_state()


# ---------------------------------------------------------------------------
# POST mutation endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/api/innovate/toggle",
    summary="Set innovate enabled/disabled",
    description="Enables or disables the innovate loop. Body: {\"enabled\": bool}. Requires auth.",
    dependencies=[
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/innovate/toggle")),
    ],
)
async def api_innovate_toggle(request: Request) -> Any:
    """Innovate toggle — mirrors api.py:3783-3794."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="'enabled' is required")
    try:
        return _set_innovate(bool(body["enabled"]))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/api/innovate/tick",
    summary="Run one innovate tick (spawn-guarded)",
    description=(
        "Triggers one innovate loop iteration. "
        "Rejected with 403 for HeadlessChrome/Puppeteer/Playwright User-Agents. "
        "Requires auth."
    ),
    dependencies=[
        Depends(require_not_test_origin),
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/innovate/tick")),
    ],
)
def api_innovate_tick() -> Any:
    """Innovate tick — mirrors api.py:3796-3824. Side-effects MOCKED in tests."""
    try:
        return _innovate_tick()
    except PermissionError as exc:
        body_str = str(exc)
        if "rate-limited" in body_str:
            _m = re.search(r"wait (\d+)s", body_str)
            retry = int(_m.group(1)) if _m else 60
            return JSONResponse(
                status_code=429,
                content={"error": "rate-limited", "source": "innovate_tick_internal", "retry_after_seconds": retry},
                headers={"Retry-After": str(retry)},
            )
        elif "spawn gate disabled" in body_str:
            raise HTTPException(
                status_code=503,
                detail={"error": "spawn gate disabled", "gate": "gates.allow_claude_spawn"},
            ) from exc
        else:
            raise HTTPException(
                status_code=503,
                detail={"error": "spawn-cap reached", "source": "innovate_tick_internal"},
            ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
