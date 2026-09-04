"""
Per-IP token-bucket rate limiter middleware for the FastAPI app.

Ports the legacy api.py _check_rate_limit / _send_429 behaviour to a
Starlette BaseHTTPMiddleware so the FastAPI app applies the same throttling
as the legacy ThreadingHTTPServer.

Parameters (matching legacy api.py exactly):
  rate=1.0 tokens/second, burst=60.0 tokens
  cleanup_interval=60.0s, stale_after=600.0s

IP source:
  ``request.client.host`` — the app strips X-Forwarded-* headers at the
  reverse-proxy layer, so ``client.host`` is the real peer IP.
  We intentionally do NOT read X-Forwarded-For; it is spoofable.

Rate-limit exemptions:
  /health, /health/loop, /health/modules — exempt in legacy (api.py:2421-2440)
  /metrics — exempt in legacy (sits before _check_rate_limit in api.py:2649)

429 response body matches legacy _send_429 exactly:
  {"error": "rate limit exceeded", "retry_after": <int>}
  Headers: Content-Type: application/json, Retry-After: <int>,
           X-RateLimit-Remaining: 0

SSE / WebSocket:
  The middleware gate fires on every request scope, so SSE/WS connections
  are gated on connection open — not on every keep-alive frame — which
  matches the legacy behaviour (legacy applies _check_rate_limit once per
  GET /stream/* or GET /ws request, before the streaming loop starts).

Disable at startup:
  Set AF_RATE_LIMIT_DISABLED=1 to pass all requests through (mirrors
  legacy --no-rate-limit flag), or call RateLimitMiddleware.enabled = False
  before adding the middleware (useful in tests).
"""

from __future__ import annotations

import json
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Paths exempt from rate limiting — must match legacy api.py exemptions.
# /health, /health/loop, /health/modules are served before _check_rate_limit.
# /metrics is also served before the rate-limit gate in legacy (api.py:2649).
# ---------------------------------------------------------------------------
_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/health/loop",
        "/health/modules",
        "/metrics",
    }
)

# Shared limiter instance — same parameters as api.py:2017-2019.
_limiter: RateLimiter = RateLimiter(
    rate=1.0,
    burst=60.0,
    cleanup_interval=60.0,
    stale_after=600.0,
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP token-bucket rate limiter.

    Returns HTTP 429 + Retry-After when the IP exhausts its token bucket.
    Exempt paths (health/metrics) pass through unconditionally.

    Set ``RateLimitMiddleware.enabled = False`` before instantiation (or set
    env var ``AF_RATE_LIMIT_DISABLED=1``) to disable for tests or the
    ``--no-rate-limit`` flag equivalent.
    """

    # Class-level toggle — mirrors legacy _Handler.enable_rate_limit.
    enabled: bool = True

    # Expose the shared limiter so tests can inspect bucket state.
    limiter: RateLimiter = _limiter

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        # Check env-var disable flag at dispatch time so it can be set after
        # app construction (e.g. in test fixtures).
        if not self.__class__.enabled or os.environ.get("AF_RATE_LIMIT_DISABLED") == "1":
            return await call_next(request)

        path = request.url.path
        if path in _EXEMPT_PATHS:
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        allowed, _remaining = self.__class__.limiter.check(ip)

        if not allowed:
            retry_after = self.__class__.limiter.retry_after(ip)
            body = json.dumps(
                {"error": "rate limit exceeded", "retry_after": retry_after}
            ).encode()
            return Response(
                content=body,
                status_code=429,
                media_type="application/json",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Remaining": "0",
                },
            )

        return await call_next(request)
