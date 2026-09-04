"""
FastAPI dependency — spawn-origin guard.

This is a port of ``_reject_test_origin_spawn`` from backend/api.py:1773 to a
FastAPI HTTP-request model.  The detection logic is BYTE-EQUIVALENT to the
legacy function: it blocks requests whose User-Agent matches HeadlessChrome,
Puppeteer, or Playwright (case-insensitive) and returns 403 with the exact
same JSON body ``{"error": "spawn_blocked_test_origin"}``.

Apply this dependency ONLY to the three spawn-trigger routes:
  POST /api/loop/run
  POST /api/projects/{pid}/loop/run
  POST /api/innovate/tick

Usage::

    from fastapi import Depends
    from backend.deps.origin_guard import require_not_test_origin

    @router.post("/api/loop/run", dependencies=[Depends(require_not_test_origin)])
    def loop_run(...):
        ...

Env-var bypasses (both preserve auth gate — only the UA check is skipped):
  AF_ALLOW_TEST_ORIGIN_SPAWNS=1  — legacy bypass for local human-driven dev
  AF_MCP_TEST_ORIGIN=1           — MCP Chrome DevTools scenario runs
"""

from __future__ import annotations

import os
import re

from fastapi import Request
from fastapi.responses import JSONResponse

# Exact same regex as api.py:1767
_TEST_UA_RE = re.compile(r"HeadlessChrome|Puppeteer|playwright", re.IGNORECASE)


class SpawnOriginBlocked(Exception):
    """Raised by require_not_test_origin when the UA looks like a test runner.

    Caught by the exception handler registered in asgi_app.py which returns the
    byte-equivalent legacy body: ``{"error": "spawn_blocked_test_origin"}``.
    """


def spawn_origin_blocked_handler(request: Request, exc: SpawnOriginBlocked) -> JSONResponse:  # noqa: ARG001
    """Exception handler — returns the exact legacy 403 body."""
    return JSONResponse(
        status_code=403,
        content={"error": "spawn_blocked_test_origin"},
    )


async def require_not_test_origin(request: Request) -> None:
    """FastAPI dependency — raises SpawnOriginBlocked when the request looks like a test-runner spawn.

    Mirrors ``_reject_test_origin_spawn`` in api.py:1773 exactly:
    - Blocks requests whose User-Agent matches HeadlessChrome, Puppeteer, or
      Playwright (case-insensitive).
    - Does NOT block by Origin alone (same as legacy reasoning in api.py:1784-1786).
    - Bypasses when AF_ALLOW_TEST_ORIGIN_SPAWNS=1 or AF_MCP_TEST_ORIGIN=1.
    """
    if os.environ.get("AF_ALLOW_TEST_ORIGIN_SPAWNS", "").strip() == "1":
        return
    if os.environ.get("AF_MCP_TEST_ORIGIN", "").strip() == "1":
        return

    ua = request.headers.get("user-agent", "")

    if _TEST_UA_RE.search(ua):
        raise SpawnOriginBlocked()
