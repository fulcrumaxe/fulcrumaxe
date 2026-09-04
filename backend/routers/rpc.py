"""
FastAPI router — POST /rpc (JSON-RPC 2.0 over HTTP).

Exposes the same JSON-RPC surface as the legacy server.py :8765 /rpc endpoint,
dispatching into the EXISTING ``_RPC_METHODS`` registry.  This is purely
additive — legacy server.py continues to run on :8765 unchanged.

Auth note
---------
This route uses the RPC bearer token (stored in .autonomous-team/dashboard-token,
loaded by ``_load_rpc_token``), NOT the REST ``AF_API_AUTH_KEY`` used by the
DefaultDenyMiddleware.  The two auth systems are intentionally separate:
  - REST API key  → protects all other FastAPI routes
  - RPC token     → protects this route (and :8765 legacy) exclusively
We add "/rpc" to PUBLIC_ROUTES so the DefaultDenyMiddleware lets the request
through without requiring the REST key.  This route self-authenticates against
the RPC token before touching any business logic.

Spawn guard
-----------
``loop.start`` requests from test runners (HeadlessChrome/Puppeteer/Playwright
User-Agents, or localhost Vite dev-server Origins) are blocked with the exact
same -32000 error the legacy server returns, unless
AF_ALLOW_TEST_ORIGIN_SPAWNS=1 is set.

Blocking I/O
------------
All registered handler functions are synchronous (they use DuckDB/SQLite/file
I/O).  We run each handler in the anyio default thread pool via
``anyio.to_thread.run_sync`` so we never block the event loop.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from backend import rpc_project_scope as _rpc_project_scope

# ---------------------------------------------------------------------------
# RPC token — loaded from the same file server.py uses.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TOKEN_PATH = _REPO_ROOT / ".autonomous-team" / "dashboard-token"


def _load_rpc_token() -> str:
    """Read the RPC bearer token from disk.

    Fail-closed: if the token file is missing or empty, returns "" which
    causes the auth gate to reject ALL requests with 401.  A missing-token
    configuration must never silently open /rpc to unauthenticated access —
    the spawn-guard and dispatch logic still apply independently.
    """
    try:
        return _TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Spawn-guard constants (mirrors server.py do_POST verbatim).
# ---------------------------------------------------------------------------

_SPAWN_METHODS: frozenset[str] = frozenset({"loop.start"})
_TEST_UA_RE: re.Pattern[str] = re.compile(r"HeadlessChrome|Puppeteer|playwright", re.IGNORECASE)
_TEST_ORIGINS: frozenset[str] = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["rpc"])


def _rpc_ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@router.post("/rpc")
async def rpc_dispatch(request: Request) -> Response:
    """JSON-RPC 2.0 dispatch endpoint.

    Parity contract (must be byte-equivalent to legacy :8765 /rpc for the
    same request):

    - Invalid JSON body  → HTTP 400, jsonrpc error envelope
    - Auth failure       → HTTP 401, jsonrpc error envelope
    - Spawn-guard hit    → HTTP 200, jsonrpc error {code:-32000}
    - Unknown method     → HTTP 200, jsonrpc error {code:-32601}
    - Handler exception  → HTTP 200, jsonrpc error {code: exc.rpc_code or -32000}
    - Success            → HTTP 200, jsonrpc result envelope
    """
    # ------------------------------------------------------------------
    # 1. Parse body first (before auth, so we can return the right id).
    # ------------------------------------------------------------------
    raw = await request.body()
    try:
        req = json.loads(raw) if raw else {}
        if not isinstance(req, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(
            status_code=400,
            content=_rpc_err(None, -32000, "invalid JSON"),
        )

    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    # ------------------------------------------------------------------
    # 2. Auth — RPC token (separate from the REST AF_API_AUTH_KEY).
    #    Accept: Authorization: Bearer <token>  OR  ?token=<token>
    #    Fail-closed: an empty/missing token file rejects all requests (401).
    #    Never silently open /rpc — matches the default-deny principle.
    # ------------------------------------------------------------------
    rpc_token = _load_rpc_token()
    bearer: str | None = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[len("Bearer "):].strip()
    if bearer is None:
        # Fall back to ?token= query param
        bearer = request.query_params.get("token")

    # Reject when: no token configured (empty string → compare_digest rejects ""),
    # token missing from request, or token doesn't match.
    if not rpc_token or bearer is None or not hmac.compare_digest(bearer, rpc_token):
        return JSONResponse(
            status_code=401,
            content=_rpc_err(req_id, -32000, "unauthorized"),
        )

    # ------------------------------------------------------------------
    # 3. Spawn-guard for loop.start — mirrors server.py do_POST exactly.
    # ------------------------------------------------------------------
    if method in _SPAWN_METHODS:
        allow_env = os.environ.get("AF_ALLOW_TEST_ORIGIN_SPAWNS", "").strip() == "1"
        if not allow_env:
            ua = request.headers.get("user-agent", "")
            origin = request.headers.get("origin", "")
            if _TEST_UA_RE.search(ua) or origin in _TEST_ORIGINS:
                return JSONResponse(
                    status_code=200,
                    content=_rpc_err(req_id, -32000, "spawn_blocked_test_origin"),
                )

    # ------------------------------------------------------------------
    # 4. Dispatch into _RPC_METHODS registry.
    #    Import is deferred to avoid triggering backend.server's module-level
    #    setup at app startup;
    #    the registry is populated by @_rpc_method decorators at import time,
    #    so the first call to this route will cache the module in sys.modules.
    # ------------------------------------------------------------------
    from backend.server import _RPC_METHODS  # noqa: PLC0415 — intentionally lazy

    handler = _RPC_METHODS.get(method)
    if handler is None:
        return JSONResponse(
            status_code=200,
            content=_rpc_err(req_id, -32601, f"method not found: {method}"),
        )

    # ------------------------------------------------------------------
    # 5. Call the handler in a thread pool (all handlers do blocking I/O).
    # ------------------------------------------------------------------
    try:
        result = await anyio.to_thread.run_sync(
            lambda: _rpc_project_scope.dispatch_scoped(method, params, handler)
        )
        return JSONResponse(status_code=200, content=_rpc_ok(req_id, result))
    except Exception as exc:
        err_code: int = getattr(exc, "rpc_code", -32000)
        return JSONResponse(
            status_code=200,
            content=_rpc_err(req_id, err_code, str(exc)),
        )
