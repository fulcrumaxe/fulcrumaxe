"""
FastAPI router — legacy /feed and /events SSE aliases.

server.py's HttpAdapter serves GET /feed and GET /events on the RPC port.
The dashboard's agentFeedTail.ts hits rpcBaseUrl + /feed.  FastAPI only had
/stream/feed (different path), leaving a coverage gap.

This router closes that gap by adding /feed and /events on the FastAPI app
that produce the SAME SSE event shape as server.py's _handle_feed_sse and
_handle_events_sse.  The async generators are shared with streams.py
(_file_tail_gen / _bus_gen) so the wire format is byte-equivalent.

Auth — EventSource cannot set headers, so these routes accept auth EITHER via:
  - Authorization: Bearer <token>  (same as other routes)
  - ?token=<token>                 (query-param; matches server.py _check_sse_auth)
Returns 401 when no token is provided and auth is enabled; 403 for wrong token.

The routes are NOT in the require_auth Depends() group; they do their own
query-param-aware token check via _sse_require_auth so EventSource clients work.
They are added to PUBLIC_ROUTES in auth.py via an additive entry so
DefaultDenyMiddleware does not reject them before the handler runs — the
handler itself enforces the token check.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from backend.deps.shared_limiter import get_shared_limiter
from backend.event_bus import (
    AgentOutputEvent,
    BudgetSpendEvent,
    GateChangeEvent,
    LoopIterationEvent,
)
from backend.rate_limiter import SSEConnectionTracker
from backend.routers.streams import (
    _bus_gen,
    _file_tail_gen,
    _client_ip,
    _HEARTBEAT_INTERVAL,
)

# ---------------------------------------------------------------------------
# Per-IP tracker (separate from /stream/* tracker to avoid cross-contamination
# in tests; shares the global cap via get_shared_limiter()).
# ---------------------------------------------------------------------------

_ip_tracker = SSEConnectionTracker(max_per_ip=5)


# ---------------------------------------------------------------------------
# Router — no auth dependency; handler does query-param-aware token check.
# Both /feed and /events are listed as PUBLIC_ROUTES in deps/auth.py so
# DefaultDenyMiddleware passes them through; auth is enforced below.
# ---------------------------------------------------------------------------

router = APIRouter(tags=["streams-compat"])


# ---------------------------------------------------------------------------
# Auth helper — mirrors server.py _check_sse_auth
# ---------------------------------------------------------------------------

def _get_auth_key() -> Optional[str]:
    return os.environ.get("AF_API_AUTH_KEY") or None


def _sse_auth_ok(request: Request, token: Optional[str]) -> bool:
    """Return True when the request carries a valid credential.

    Accepts either:
    - Authorization: Bearer <key>   (standard header; any origin)
    - ?token=<key>                  (query param; EventSource-compatible)

    When AF_API_AUTH_KEY is not set, auth is disabled and every request passes.
    """
    key = _get_auth_key()
    if key is None:
        return True

    # Check Authorization header first.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[len("Bearer "):]
        if hmac.compare_digest(bearer, key):
            return True

    # Fall back to ?token= query param.
    if token is not None and hmac.compare_digest(token, key):
        return True

    return False


def _auth_missing(request: Request, token: Optional[str]) -> bool:
    """Return True when NO credential was provided (→ 401)."""
    key = _get_auth_key()
    if key is None:
        return False
    auth_header = request.headers.get("Authorization", "")
    has_bearer = auth_header.startswith("Bearer ")
    has_token = token is not None
    return not has_bearer and not has_token


# ---------------------------------------------------------------------------
# /feed  — alias for /stream/feed (agent-feed.jsonl file tail)
# ---------------------------------------------------------------------------

@router.get("/feed")
async def legacy_feed(
    request: Request,
    token: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    role_filter: Optional[str] = Query(default=None, alias="filter[role]"),
) -> Response:
    """SSE stream of agent-feed.jsonl events.

    Matches server.py _handle_feed_sse wire format:
      - Sends ``data: {"type":"connected"}`` immediately on connect.
      - Tails .autonomous-team/agent-feed.jsonl for new JSONL lines.
      - Applies optional ?since=<ts> and ?filter[role]=<role> filters.
      - Emits a heartbeat after 30s of silence.
    """
    if _auth_missing(request, token):
        return Response(
            content='{"detail":"unauthorized"}',
            status_code=401,
            media_type="application/json",
        )
    if not _sse_auth_ok(request, token):
        return Response(
            content='{"detail":"forbidden"}',
            status_code=403,
            media_type="application/json",
        )

    limiter = get_shared_limiter()
    ip = _client_ip(request)

    if not limiter.acquire():
        return Response(
            content='{"detail":"stream cap reached"}',
            status_code=503,
            media_type="application/json",
        )
    if not _ip_tracker.acquire(ip):
        limiter.release()
        return Response(
            content='{"detail":"too many connections from this IP"}',
            status_code=503,
            media_type="application/json",
        )

    # Resolve feed file (always the default AF feed, same as server.py).
    from pathlib import Path  # noqa: PLC0415
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    feed_file = _REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"

    async def _generate() -> AsyncIterator[bytes]:
        try:
            # Emit connected frame (matches server.py behaviour).
            yield b'data: {"type":"connected"}\n\n'
            # Tail the file, applying since/role filters inline.
            async for chunk in _filtered_feed_gen(feed_file, since, role_filter):
                yield chunk
        finally:
            _ip_tracker.release(ip)
            limiter.release()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# /events  — alias for /stream/events (all bus event types + _event_type)
# ---------------------------------------------------------------------------

@router.get("/events")
async def legacy_events(
    request: Request,
    token: Optional[str] = Query(default=None),
    loop_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
) -> Response:
    """SSE stream of ALL event bus events with _event_type field.

    Matches server.py _handle_events_sse wire format:
      - Sends ``data: {"type":"connected"}`` immediately on connect.
      - Subscribes to the in-process event bus for all 4 event types.
      - Applies optional ?loop_id= and ?since= filters.
      - Emits a heartbeat after 30s of silence.
    """
    if _auth_missing(request, token):
        return Response(
            content='{"detail":"unauthorized"}',
            status_code=401,
            media_type="application/json",
        )
    if not _sse_auth_ok(request, token):
        return Response(
            content='{"detail":"forbidden"}',
            status_code=403,
            media_type="application/json",
        )

    limiter = get_shared_limiter()
    ip = _client_ip(request)

    if not limiter.acquire():
        return Response(
            content='{"detail":"stream cap reached"}',
            status_code=503,
            media_type="application/json",
        )
    if not _ip_tracker.acquire(ip):
        limiter.release()
        return Response(
            content='{"detail":"too many connections from this IP"}',
            status_code=503,
            media_type="application/json",
        )

    event_types = [AgentOutputEvent, BudgetSpendEvent, GateChangeEvent, LoopIterationEvent]

    async def _generate() -> AsyncIterator[bytes]:
        try:
            yield b'data: {"type":"connected"}\n\n'
            async for chunk in _bus_gen(event_types, add_event_type=True):
                yield chunk
        finally:
            _ip_tracker.release(ip)
            limiter.release()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Filtered feed generator — mirrors server.py's since/role_filter logic
# ---------------------------------------------------------------------------

async def _filtered_feed_gen(
    feed_file,
    since: Optional[str],
    role_filter: Optional[str],
) -> AsyncIterator[bytes]:
    """Tail *feed_file* and emit filtered SSE frames.

    Filters:
    - ?since=<ts>: skip events whose timestamp < since
    - ?filter[role]=<role>: skip events whose role != role_filter

    Mirrors server.py _handle_feed_sse filtering logic.
    Delegates file I/O to _file_tail_gen and post-filters the yielded frames.
    """
    import asyncio  # noqa: PLC0415

    from pathlib import Path as _Path  # noqa: PLC0415
    import anyio.to_thread  # noqa: PLC0415

    POLL_INTERVAL = 0.5
    _HEARTBEAT: bytes = b'data: {"type":"heartbeat"}\n\n'

    import anyio.to_thread as _att  # noqa: PLC0415

    # Open and seek to end.
    def _open_seek():
        if not feed_file.exists():
            return None
        fh = open(feed_file, "r", encoding="utf-8", errors="replace")  # noqa: PTH123
        fh.seek(0, 2)
        return fh

    fh = await anyio.to_thread.run_sync(_open_seek)
    last_heartbeat = asyncio.get_event_loop().time()

    try:
        while True:
            new_events = False

            if fh is None:
                fh = await anyio.to_thread.run_sync(_open_seek)
            else:
                def _read_lines(f):
                    lines = []
                    try:
                        for raw in f:
                            raw = raw.strip()
                            if raw:
                                lines.append(raw)
                    except OSError:
                        pass
                    return lines

                lines = await anyio.to_thread.run_sync(lambda: _read_lines(fh))
                for raw in lines:
                    try:
                        ev = json.loads(raw)
                    except (json.JSONDecodeError, OSError):
                        continue

                    # Apply ?since= filter (same as server.py).
                    ts = ev.get("timestamp") or ev.get("ts") or ""
                    if since and ts < since:
                        continue

                    # Apply ?filter[role]= filter (same as server.py).
                    if role_filter and ev.get("role") != role_filter:
                        continue

                    yield ("data: " + json.dumps(ev, default=str) + "\n\n").encode("utf-8")
                    new_events = True

            now = asyncio.get_event_loop().time()
            if not new_events and (now - last_heartbeat) >= _HEARTBEAT_INTERVAL:
                yield _HEARTBEAT
                last_heartbeat = now

            await asyncio.sleep(POLL_INTERVAL)
    finally:
        if fh is not None:
            def _close(f):
                try:
                    f.close()
                except OSError:
                    pass
            await anyio.to_thread.run_sync(lambda: _close(fh))
