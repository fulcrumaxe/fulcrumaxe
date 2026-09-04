"""
Auth dependency and default-deny middleware for the FastAPI app.

This module provides:
- ``require_auth``  — FastAPI DI dependency for bearer-token auth
- ``DefaultDenyMiddleware`` — rejects any request to a non-public route that
  lacks a valid ``Authorization: Bearer <token>`` header
- ``PUBLIC_ROUTES`` — the explicit allowlist of paths that don't need a token

Legacy behaviour preserved (api.py:2321-2338):
- Token missing → 401 Unauthorized
- Token present but wrong → 403 Forbidden
- ``hmac.compare_digest`` used for constant-time comparison
- ``AF_API_AUTH_KEY`` unset → auth disabled (always pass)
"""

from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Public route allowlist — paths that don't require authentication.
# Keep this short; every new public route must be added here AND to the
# route-introspection test (test_deps_auth.py) to pass the audit gate.
# ---------------------------------------------------------------------------
PUBLIC_ROUTES: frozenset[str] = frozenset(
    {
        "/health",
        # P2: health sub-routes are also public (legacy api.py serves them
        # before the _check_auth() gate at api.py:2671).
        "/health/loop",
        "/health/modules",
        "/docs",
        "/openapi.json",
        "/redoc",
        # Stub SSE/stream endpoint used by the P1 perf gate tests.
        "/stub/stream",
        # P5d: Prometheus scrape endpoint — public (sits before _check_auth in
        # legacy api.py:2649 vs auth gate at api.py:2671).
        "/metrics",
        # P6a: RPC endpoint self-authenticates with the RPC token (the separate
        # dashboard-token file, not AF_API_AUTH_KEY).  Adding it to PUBLIC_ROUTES
        # lets DefaultDenyMiddleware pass the request through; the route handler
        # then enforces the RPC token itself.  These are two independent auth
        # systems: REST key protects other FastAPI routes, RPC token protects /rpc.
        "/rpc",
        # PR2: /feed and /events are SSE aliases for legacy server.py routes.
        # EventSource cannot set headers, so auth is via ?token= query param.
        # DefaultDenyMiddleware passes them through; the route handler enforces
        # the token check itself (both header and query-param forms).
        "/feed",
        "/events",
        # PR2: /dashboard and / are public pages (no auth required).
        "/dashboard",
        "/",
    }
)

# ---------------------------------------------------------------------------
# Public route prefix list — any path that STARTS WITH one of these prefixes
# is public (no auth required). Used for /api/* routes that legacy serves
# before _check_auth() — they are pre-auth in the legacy flow.
# ---------------------------------------------------------------------------
PUBLIC_PREFIXES: tuple[str, ...] = (
    # P3: these /api/* routes are public in the legacy server (pre-auth).
    "/api/config",
    "/api/projects",
    "/api/sessions",
    "/api/events",
    "/api/fleet/",
    "/api/ideas",
    "/api/spawn-blocks",
    "/api/loop/",
    "/api/innovate",
)


def _is_public(path: str) -> bool:
    """Return True when *path* is a public route (no auth required)."""
    if path in PUBLIC_ROUTES:
        return True
    for prefix in PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return True
    return False


def _get_auth_key() -> Optional[str]:
    """Return the configured auth key from the environment, or None if auth is disabled."""
    return os.environ.get("AF_API_AUTH_KEY") or None


def _extract_bearer(request: Request) -> Optional[str]:
    """Extract the Bearer token from the Authorization header, or None if absent."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]
    return None


async def require_auth(request: Request) -> None:
    """FastAPI dependency that enforces bearer-token authentication.

    Raises HTTPException(401) if no token is provided when auth is enabled.
    Raises HTTPException(403) if the token is present but wrong.
    Does nothing when AF_API_AUTH_KEY is unset (auth disabled).

    Uses hmac.compare_digest for constant-time comparison to prevent
    timing side-channel attacks.
    """
    key = _get_auth_key()
    if key is None:
        # Auth disabled — always pass.
        return

    token = _extract_bearer(request)
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")

    if not hmac.compare_digest(token, key):
        raise HTTPException(status_code=403, detail="forbidden")


class DefaultDenyMiddleware(BaseHTTPMiddleware):
    """Reject any request to a non-public route that lacks a valid auth token.

    This is a defence-in-depth layer on top of the per-route ``require_auth``
    dependency.  Both must be satisfied; adding a route without auth AND
    without adding it to PUBLIC_ROUTES will fail the route-introspection test.

    Public routes (listed in PUBLIC_ROUTES) pass through without a token.
    All other routes require ``Authorization: Bearer <key>``.

    When AF_API_AUTH_KEY is not set the middleware passes every request
    through (auth disabled).
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        key = _get_auth_key()
        if key is None:
            # Auth disabled globally.
            return await call_next(request)

        path = request.url.path
        if _is_public(path):
            return await call_next(request)

        token = _extract_bearer(request)
        if token is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "unauthorized"},
            )

        if not hmac.compare_digest(token, key):
            return JSONResponse(
                status_code=403,
                content={"detail": "forbidden"},
            )

        return await call_next(request)
