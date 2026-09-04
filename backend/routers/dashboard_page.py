"""
FastAPI router — /dashboard HTML page and / root endpoint.

Migrates two legacy api.py GET routes onto the FastAPI app:
- GET /dashboard  → returns the dashboard HTML (backend.dashboard.get_dashboard_html)
- GET /           → returns a minimal JSON root greeting (matches legacy api.py fallthrough)

Both routes are public (no auth required) — legacy api.py serves /dashboard and /
before the _check_auth() gate.  They are added to PUBLIC_ROUTES in auth.py so
DefaultDenyMiddleware passes them through.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["ui"])


@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard() -> HTMLResponse:
    """Return the fulcrumaxe dashboard HTML page.

    Delegates to backend.dashboard.get_dashboard_html() — same function
    api.py uses, so the page content is byte-equivalent.
    """
    from backend.dashboard import get_dashboard_html  # noqa: PLC0415
    html = get_dashboard_html()
    return HTMLResponse(content=html, status_code=200)


@router.get("/", include_in_schema=False)
async def root() -> JSONResponse:
    """Root endpoint — returns a minimal API greeting.

    Legacy api.py returns 404 for unknown paths; the root path / is
    not explicitly handled there (it falls through to the 404 handler).
    FastAPI apps conventionally return a greeting here, and the dashboard
    client doesn't call this endpoint.  We return a simple JSON object
    so that curl / healthcheck scripts get a 200 instead of a 404/proxy hop.
    """
    from backend.api_version import CURRENT_VERSION  # noqa: PLC0415
    return JSONResponse(
        content={
            "name": "fulcrumaxe",
            "api_version": CURRENT_VERSION,
            "status": "ok",
        },
        status_code=200,
    )
