"""
FastAPI router — GET /api/config

Migrates the /api/config handler from api.py (lines 2454-2468).

SECURITY LANDMINE — preserve byte-equivalent behaviour:
  - Non-loopback caller → 403 "forbidden: /api/config is localhost-only"
  - Cross-origin request (non-localhost Origin header) → 403
    "forbidden: cross-origin access to /api/config denied"
  - All other callers → 200 with dashboard config JSON

This route does NOT require a bearer token (it sits before _check_auth in
the legacy flow). The loopback gate itself is the access control.

P1's DefaultDenyMiddleware strips X-Forwarded-* headers, so
request.client.host is the REAL connecting IP — no spoofing possible.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api import _get_dashboard_config

router = APIRouter(tags=["api-config"])

# Exact 403 messages from legacy api.py lines 2458 and 2466.
_MSG_NOT_LOCALHOST = "forbidden: /api/config is localhost-only"
_MSG_CROSS_ORIGIN = "forbidden: cross-origin access to /api/config denied"

# Loopback IP set — same as legacy _client_ip() check.
_LOOPBACK_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(request: Request) -> bool:
    """Return True when the direct connecting IP is a loopback address."""
    if request.client is None:
        return False
    return request.client.host in _LOOPBACK_IPS


@router.get(
    "/api/config",
    include_in_schema=True,
    summary="Dashboard runtime config (localhost-only)",
    description=(
        "Returns {rpcBaseUrl, rpcToken, dashboardVersion} for the React SPA. "
        "Restricted to loopback callers. Non-loopback or cross-origin → 403."
    ),
    tags=["api-config"],
)
async def api_config(request: Request) -> JSONResponse:
    """Serve dashboard runtime config with loopback + CORS gate.

    Byte-equivalent to legacy api.py handler at lines 2454-2468.
    """
    # Gate 1: loopback-only
    if not _is_loopback(request):
        return JSONResponse(
            status_code=403,
            content={"error": _MSG_NOT_LOCALHOST},
        )

    # Gate 2: refuse cross-origin requests (non-localhost Origin header)
    origin = request.headers.get("Origin", "")
    if origin and not (
        origin.startswith("http://localhost")
        or origin.startswith("http://127.0.0.1")
    ):
        return JSONResponse(
            status_code=403,
            content={"error": _MSG_CROSS_ORIGIN},
        )

    config = _get_dashboard_config()
    return JSONResponse(content=config)
