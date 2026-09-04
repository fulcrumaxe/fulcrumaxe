"""
FastAPI app hub — Phase 1 scaffolding.

This module:
1. Creates the FastAPI ``app`` object
2. Registers the default-deny middleware
3. Raises anyio threadpool ``total_tokens`` at startup (AC7)
4. Mounts the reverse-proxy catch-all for unmigrated legacy routes (AC3)
5. Exposes a ``/health`` public endpoint (AC4)
6. Exposes ``/stub/stream`` for the P1 perf-gate / stream-cap tests (AC8, AC10)

It ONLY wires things — no business logic lives here.
Future phases add ``app.include_router(...)`` lines as the only hub change.

Legacy server (api.py ThreadingHTTPServer) stays on :18099.
This app runs on a NEW port (default :18100 via AF_ASGI_PORT env var).
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from backend.deps.auth import DefaultDenyMiddleware, PUBLIC_ROUTES, require_auth
from backend.deps.origin_guard import SpawnOriginBlocked, spawn_origin_blocked_handler
from backend.deps.shared_limiter import get_shared_limiter
from backend.middleware.legacy_envelope import LegacyEnvelopeMiddleware, unhandled_exc_handler
from backend.middleware.rate_limit import RateLimitMiddleware
from backend.routers import health as health_router
from backend.routers import stats as stats_router
from backend.routers import api_config as api_config_router
from backend.routers import api_projects as api_projects_router
from backend.routers import api_sessions as api_sessions_router
from backend.routers import api_events as api_events_router
from backend.routers import api_fleet as api_fleet_router
from backend.routers import api_ideas as api_ideas_router
from backend.routers import api_loop as api_loop_router
from backend.routers import api_innovate as api_innovate_router
from backend.routers import streams as streams_router
from backend.routers import ws as ws_router
from backend.routers import ops_budget as ops_budget_router
from backend.routers import ops_control as ops_control_router
from backend.routers import ops_sessions as ops_sessions_router
from backend.routers import sessions_get as sessions_get_router
from backend.routers import ops_backup as ops_backup_router
from backend.routers import ops_misc as ops_misc_router
from backend.routers import ops_replays as ops_replays_router
from backend.routers import replays_get as replays_get_router
from backend.routers import ops_projects as ops_projects_router
from backend.routers import obs_metrics as obs_metrics_router
from backend.routers import obs_cost as obs_cost_router
from backend.routers import obs_registry as obs_registry_router
from backend.routers import obs_control as obs_control_router
from backend.routers import obs_audit as obs_audit_router
from backend.routers import obs_kpi as obs_kpi_router
from backend.routers import obs_deps as obs_deps_router
from backend.routers import obs_quality as obs_quality_router
from backend.routers import info_misc as info_misc_router
from backend.routers import info_agents as info_agents_router
from backend.routers import info_plugins as info_plugins_router
from backend.routers import info_memory as info_memory_router
from backend.routers import info_benchmarks as info_benchmarks_router
from backend.routers import graphql_route as graphql_route_router
from backend.routers import spawn_queue_get as spawn_queue_get_router
from backend.routers import rpc as rpc_router
from backend.routers import rpc_sse as rpc_sse_router
from backend.routers import dashboard_page as dashboard_page_router
from backend.routers import traces_get as traces_get_router

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default anyio threadpool total_tokens. Override via AF_THREADPOOL_TOKENS env var.
_THREADPOOL_TOKENS_DEFAULT: int = 256
#: Hard cap — never allow above this even if env var says higher.
_THREADPOOL_TOKENS_CAP: int = 512

#: Global concurrent SSE/WS stream limit (all IPs combined).
#: Override via AF_GLOBAL_STREAM_CAP env var.
#: Bumped from 20 → 40 (PR2: stream-limiter fragmentation fix).
_GLOBAL_STREAM_CAP_DEFAULT: int = 40

#: Legacy dashboard server base URL (loopback only — never trust X-Forwarded-For).
_LEGACY_BASE_URL: str = "http://127.0.0.1:18099"

# Hop-by-hop headers that must not be forwarded to the legacy server or
# returned to the client.  Defined at module level so the set is built once.
_HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _threadpool_tokens() -> int:
    """Read AF_THREADPOOL_TOKENS env var, clamping to [1, _THREADPOOL_TOKENS_CAP]."""
    raw = os.environ.get("AF_THREADPOOL_TOKENS", "")
    try:
        val = int(raw)
    except (ValueError, TypeError):
        val = _THREADPOOL_TOKENS_DEFAULT
    return max(1, min(val, _THREADPOOL_TOKENS_CAP))


def _global_stream_cap() -> int:
    """Read AF_GLOBAL_STREAM_CAP env var; fall back to _GLOBAL_STREAM_CAP_DEFAULT."""
    raw = os.environ.get("AF_GLOBAL_STREAM_CAP", "")
    try:
        val = int(raw)
        return max(1, val)
    except (ValueError, TypeError):
        return _GLOBAL_STREAM_CAP_DEFAULT


# ---------------------------------------------------------------------------
# Global stream limiter (AC8) — wired here, class lives in deps/stream_limiter.py
# ---------------------------------------------------------------------------

# Module-level limiter — shared across all requests.
# Points to the SAME singleton as deps/shared_limiter.py so that /stream/*,
# /feed, /events, and /stub/stream all count against ONE cap (PR2 fix).
stream_limiter = get_shared_limiter()


# ---------------------------------------------------------------------------
# Lifespan — threadpool sizing at startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Raise the anyio default thread limiter to the configured value."""
    from anyio import to_thread  # noqa: PLC0415 — local import avoids top-level anyio dep
    limiter = to_thread.current_default_thread_limiter()
    tokens = _threadpool_tokens()
    limiter.total_tokens = tokens  # anyio requires int (or math.inf)
    # Store the configured value in app.state so tests can verify it without
    # needing to query the limiter from an async context.
    app.state.threadpool_tokens = tokens
    yield


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="fulcrumaxe API",
    description="FastAPI strangler-fig app — Phase 1 scaffolding.",
    version="1.0.0-p1",
    lifespan=_lifespan,
)

# Register middlewares.
#
# Starlette processes add_middleware() calls in LIFO order: the LAST call
# becomes the OUTERMOST wrapper (first to see requests, last to see responses).
#
# Stack (outermost → innermost):
#   LegacyEnvelopeMiddleware  — wraps everything; added last
#   DefaultDenyMiddleware     — auth gate; added second
#   RateLimitMiddleware       — per-IP throttle; added first (innermost)
#
# A 429 from RateLimitMiddleware is still wrapped by the envelope so
# callers get a consistent response shape.

# Per-IP token-bucket rate limiter (ports legacy api.py _check_rate_limit).
# Same parameters: 60 req/min burst, 1 req/sec refill.
app.add_middleware(RateLimitMiddleware)

# Default-deny auth gate — sits outside the rate-limiter, inside the envelope.
app.add_middleware(DefaultDenyMiddleware)

# Legacy-envelope middleware — OUTERMOST, added last.
# Starlette applies add_middleware() in reverse registration order — the LAST
# add_middleware() call becomes the outermost (first to receive each request,
# last to see each response).  We register it here, after DefaultDenyMiddleware,
# so it wraps everything including the auth and rate-limit layers.
app.add_middleware(LegacyEnvelopeMiddleware)

# Register spawn-origin guard exception handler.
# Returns the byte-equivalent legacy 403 body: {"error": "spawn_blocked_test_origin"}
app.add_exception_handler(SpawnOriginBlocked, spawn_origin_blocked_handler)  # type: ignore[arg-type]

# Catch all other unhandled exceptions — emit generic 5xx (CWE-209 safe).
app.add_exception_handler(Exception, unhandled_exc_handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Stub stream endpoint — used by perf-gate / stream-cap tests only (P1)
# No SSE/WS business routes migrate in P1; this stub exists solely to let
# the global-cap and perf-gate tests exercise the limiter.
# ---------------------------------------------------------------------------

@app.get("/stub/stream", tags=["public"])
async def stub_stream(request: Request) -> Response:
    """Stub SSE endpoint used by P1 perf/cap tests.  Not a real business route.

    Acquires the global stream slot; holds it until the client disconnects
    (or the async generator is exhausted).  Returns 503 when the cap is full.
    """
    if not stream_limiter.acquire():
        return Response(
            content='{"detail": "stream cap reached"}',
            status_code=503,
            media_type="application/json",
        )

    async def _event_gen() -> AsyncIterator[bytes]:
        try:
            # Send one keep-alive comment then wait for disconnect.
            yield b": keepalive\n\n"
            while True:
                if await request.is_disconnected():
                    break
                # Small sleep to avoid busy-loop in tests.
                await asyncio.sleep(0.05)
        finally:
            stream_limiter.release()

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# P2 routers — registered BEFORE the catch-all so specific routes win.
# Starlette matches routes in registration order; catch-all must remain LAST.
# ---------------------------------------------------------------------------

# Health router: /health, /health/loop, /health/modules (PUBLIC — no auth)
app.include_router(health_router.router)

# Stats router: /registry/stats, /audit/stats, /quality/stats,
#               /memory/stats, /traces/stats (require_auth)
app.include_router(stats_router.router)

# ---------------------------------------------------------------------------
# P3 routers — /api/* GET routes migrated off the legacy proxy.
# Registered BEFORE the catch-all so these specific routes win.
# ---------------------------------------------------------------------------

# /api/config — loopback-only, no bearer required (loopback is the gate)
app.include_router(api_config_router.router)

# /api/projects, /api/projects/{pid}, /api/projects/{pid}/loop/runs[/{run_id}]
# Pre-auth routes — no bearer required (legacy places them before _check_auth)
app.include_router(api_projects_router.router)

# /api/sessions/current, /api/sessions — loopback free; remote needs bearer
app.include_router(api_sessions_router.router)

# /api/events — plain JSON polling endpoint (NOT SSE), no auth required
app.include_router(api_events_router.router)

# /api/fleet/projects — no auth required
app.include_router(api_fleet_router.router)

# /api/ideas, /api/spawn-blocks — no auth required
app.include_router(api_ideas_router.router)

# /api/loop/runs, /api/loop/runs/{run_id} — no auth required
app.include_router(api_loop_router.router)

# /api/innovate — no auth required
app.include_router(api_innovate_router.router)

# /stream/feed, /stream/status, /stream/events — async SSE routes (P4)
# Registered BEFORE the catch-all; require auth (legacy places these post-auth).
app.include_router(streams_router.router)

# /ws — native Starlette WebSocket route (P5a)
# Auth is handled inside the route handler via ?token= query param
# (BaseHTTPMiddleware skips websocket scope, so the route owns auth).
app.include_router(ws_router.router)

# ---------------------------------------------------------------------------
# P5c routers — operational POST/PATCH/DELETE routes migrated off the proxy.
# Registered BEFORE the catch-all so these specific routes win.
# ---------------------------------------------------------------------------

# /budget/init — budget session initialisation (POST, auth+RBAC)
app.include_router(ops_budget_router.router)

# /control/set — set a dial/gate (POST, auth+RBAC)
# /api/projects/{name}/control — bulk ControlSettings update (PATCH, auth+RBAC)
app.include_router(ops_control_router.router)

# /sessions/start, /sessions/close — session lifecycle (POST, auth+RBAC)
app.include_router(ops_sessions_router.router)

# /sessions, /sessions/current, /sessions/compare, /sessions/{id} — GET reads (auth+RBAC)
# Fixed paths (/sessions/current, /sessions/compare) registered before the
# parameterised catch (/sessions/{session_id}) because Starlette matches in
# registration order; include_router preserves the declaration order in the router.
app.include_router(sessions_get_router.router)

# /backup, /backup/restore — state-dir backup ops (POST, auth+RBAC)
app.include_router(ops_backup_router.router)

# /notifications/test, /spawn-queue/enqueue — misc ops (POST, auth+RBAC)
app.include_router(ops_misc_router.router)

# /replays/{agent_id}/start, /replays/pause|resume|stop|seek — replay control
# (POST, auth+RBAC; fixed paths registered first inside the router)
app.include_router(ops_replays_router.router)

# /replays, /replays/status, /replays/{agent_id}/summary, /replays/{agent_id}
# (GET, auth+RBAC; fixed paths registered first inside the router)
app.include_router(replays_get_router.router)

# DELETE /api/projects/{pid} — project deletion (auth, no RBAC — mirrors legacy)
app.include_router(ops_projects_router.router)

# ---------------------------------------------------------------------------
# P5d routers — observability GET routes migrated off the proxy.
# Registered BEFORE the catch-all so these specific routes win.
# ---------------------------------------------------------------------------

# /metrics — Prometheus scrape (public; sits before _check_auth in legacy)
app.include_router(obs_metrics_router.router)

# /budget/status, /cost, /cost/summary — cost/budget reads (auth + RBAC)
app.include_router(obs_cost_router.router)

# /registry — full registry data + inline stats (auth + RBAC)
# NOTE: /registry/stats already migrated in P2 (stats.py)
app.include_router(obs_registry_router.router)

# /control, /control/gates, /control/audit — control-plane reads (auth + RBAC)
# NOTE: POST /control/set already migrated in P5c (ops_control.py)
app.include_router(obs_control_router.router)

# /audit — audit trail query (auth + RBAC)
# NOTE: /audit/stats already migrated in P2 (stats.py)
app.include_router(obs_audit_router.router)

# /kpi, /kpi/velocity, /kpi/cycle-time — KPI reads (auth + RBAC)
app.include_router(obs_kpi_router.router)

# /deps — dependency graph (auth + RBAC; multi-format: json/dot/ascii)
app.include_router(obs_deps_router.router)

# /quality — quality scorer history (auth + RBAC)
# NOTE: /quality/stats already migrated in P2 (stats.py)
app.include_router(obs_quality_router.router)

# ---------------------------------------------------------------------------
# D#1425 PR5 — spawn-queue GET reads migrated off the proxy.
# Registered BEFORE the catch-all so these specific routes win.
# ---------------------------------------------------------------------------

# /spawn-queue, /spawn-queue/pending, /spawn-queue/active — queue reads (auth + RBAC)
# /spawn-blocks — bare spawn-blocks feed (auth + RBAC)
app.include_router(spawn_queue_get_router.router)

# ---------------------------------------------------------------------------
# P5e routers — remaining informational GET routes migrated off the proxy.
# Registered BEFORE the catch-all so these specific routes win.
# ---------------------------------------------------------------------------

# /rbac/whoami, /backups, /notifications/history (auth + RBAC)
app.include_router(info_misc_router.router)

# /agents, /agents/{role}, /agents/profiles, /agents/profiles/summary,
# /agents/profiles/{role_name} (auth + RBAC)
# Fixed paths registered first inside the router.
app.include_router(info_agents_router.router)

# /plugins, /plugins/{name} (auth + RBAC)
app.include_router(info_plugins_router.router)

# /memory/lessons, /memory/context (auth + RBAC)
# NOTE: /memory/stats already migrated in P2 (stats.py)
app.include_router(info_memory_router.router)

# /benchmarks, /benchmarks/history, /benchmarks/{category}[/{operation}]
# (auth + RBAC; /benchmarks/history registered first inside the router)
app.include_router(info_benchmarks_router.router)

# ---------------------------------------------------------------------------
# P5f router — POST /graphql (home-grown, no external GraphQL library)
# ---------------------------------------------------------------------------

# POST /graphql — auth + RBAC; registered before catch-all
app.include_router(graphql_route_router.router)

# ---------------------------------------------------------------------------
# P6a router — POST /rpc (JSON-RPC 2.0 dispatch into _RPC_METHODS registry).
# Self-authenticates with the RPC token; see backend/routers/rpc.py for details.
# Registered BEFORE the catch-all so /rpc is handled natively, not proxied.
# ---------------------------------------------------------------------------

# POST /rpc — JSON-RPC 2.0 dispatch (additive, legacy :8765 still runs)
app.include_router(rpc_router.router)

# ---------------------------------------------------------------------------
# PR2 routers — /feed, /events SSE aliases + /dashboard + / root
# Registered BEFORE the catch-all so these specific routes win.
# ---------------------------------------------------------------------------

# GET /feed, GET /events — legacy SSE aliases with ?token= query-param auth.
# Registered before catch-all; auth handled in handler (EventSource-compatible).
app.include_router(rpc_sse_router.router)

# GET /dashboard — dashboard HTML page; GET / — root greeting (both public).
app.include_router(dashboard_page_router.router)

# ---------------------------------------------------------------------------
# D#1425 teardown cluster — GET /traces and GET /traces/{trace_id}
# NOTE: GET /traces/stats is already native in stats.py above; not duplicated here.
# Registered BEFORE the catch-all so these specific routes win.
# ---------------------------------------------------------------------------

# GET /traces, GET /traces/{trace_id} — trace reads (auth + RBAC)
app.include_router(traces_get_router.router)


# ---------------------------------------------------------------------------
# Reverse-proxy catch-all — forwards unmigrated paths to legacy :18099 (AC3)
# ---------------------------------------------------------------------------
# SECURITY: This proxy connects to loopback ONLY (127.0.0.1:18099).
# It NEVER reads X-Forwarded-* from the incoming request and NEVER passes
# them to the legacy server, preventing XFF-spoofing attacks (AC9).
# ---------------------------------------------------------------------------

_proxy_client: httpx.AsyncClient | None = None


def _get_proxy_client() -> httpx.AsyncClient:
    """Return (creating if needed) the shared httpx AsyncClient for proxying."""
    global _proxy_client
    if _proxy_client is None or _proxy_client.is_closed:
        _proxy_client = httpx.AsyncClient(
            base_url=_LEGACY_BASE_URL,
            # Timeouts: connect 5s, read/write/pool 60s (SSE can be long).
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=60.0),
            # Disable follow-redirects — pass redirect responses through verbatim.
            follow_redirects=False,
        )
    return _proxy_client


# The catch-all carries require_auth so the route-introspection test (AC5)
# can verify every non-public route is guarded.  The DefaultDenyMiddleware
# already rejected unauthenticated requests before reaching this handler,
# so the dependency is a belt-and-suspenders gate, not duplicate logic.

@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,  # Exclude from OpenAPI — it's not a real route.
    tags=["proxy"],
    dependencies=[Depends(require_auth)],
)
async def _strangler_proxy(path: str, request: Request) -> Response:
    """Forward every unmigrated request to the legacy ThreadingHTTPServer on :18099.

    Security invariants:
    - Connects to 127.0.0.1:18099 ONLY (hard-coded loopback).
    - NEVER reads ``X-Forwarded-*`` from the incoming request.
    - NEVER sets ``X-Forwarded-For`` on the upstream request.
    - Passes the original ``Authorization`` header through (the legacy server
      does its own auth check for guarded endpoints).
    """
    client = _get_proxy_client()
    url = f"/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # Build upstream headers — drop hop-by-hop headers and ANY X-Forwarded-*.
    upstream_headers: dict[str, str] = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and not k.lower().startswith("x-forwarded-")
        and k.lower() != "host"
    }

    body = await request.body()

    upstream_resp = await client.request(
        method=request.method,
        url=url,
        headers=upstream_headers,
        content=body,
    )

    # Strip hop-by-hop headers from the downstream response.
    resp_headers: dict[str, str] = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and k.lower() != "transfer-encoding"
    }

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
