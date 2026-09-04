"""
FastAPI-native /ws WebSocket route — Phase 5a.

Migrates the /ws endpoint from the hand-rolled RFC-6455 handler in
backend/websocket.py onto a native Starlette WebSocket route.  Starlette
handles the HTTP → WS upgrade and RFC-6455 framing; this module focuses
entirely on the application protocol.

Protocol (byte-semantic parity with legacy WebSocketHandler):
    Inbound (client → server, JSON text):
        {"type": "subscribe",   "events": ["AgentOutputEvent", ...]}
        {"type": "unsubscribe", "events": ["AgentOutputEvent", ...]}
        {"type": "ping"}  → server replies {"type": "pong"}

    Outbound (server → client, JSON text frames):
        Each bus event is serialised as JSON with an extra "_event_type" key.
        Heartbeat every 30 s of idle: {"type": "heartbeat"}

Default subscription: ALL four known event types (matches legacy).

Auth:
    Mirrors legacy WebSocketHandler: when AF_API_AUTH_KEY is set, the client
    MUST pass ?token=<key> in the query string.  Wrong or missing token →
    close(4403) before accept.  When the env var is unset, auth is disabled.

Per-IP cap:
    Uses SSEConnectionTracker (same per-IP cap the legacy server uses for /ws).
    Over-cap: close before accept with code 4429.
    The global GlobalStreamLimiter is NOT acquired for /ws — legacy uses only
    _sse_tracker for this path.  Slot released in finally so disconnect always
    releases.

Bus → async bridge:
    Event bus callbacks are synchronous.  We post events to an asyncio.Queue
    via loop.call_soon_threadsafe so the async receive loop is never blocked.
    Pattern is identical to the SSE streams in routers/streams.py (P4).

Legacy files:
    backend/websocket.py  — NOT deleted (legacy api.py still imports it).
    backend/api.py        — UNCHANGED.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from typing import Optional

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from backend.event_bus import (
    AgentOutputEvent,
    BudgetSpendEvent,
    GateChangeEvent,
    LoopIterationEvent,
    get_bus,
)
from backend.rate_limiter import SSEConnectionTracker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All known event type classes + their string names — used for subscriptions.
_ALL_EVENT_TYPES: tuple[tuple[type, str], ...] = (
    (AgentOutputEvent, "AgentOutputEvent"),
    (BudgetSpendEvent, "BudgetSpendEvent"),
    (GateChangeEvent, "GateChangeEvent"),
    (LoopIterationEvent, "LoopIterationEvent"),
)

_ALL_EVENT_TYPE_NAMES: frozenset[str] = frozenset(name for _, name in _ALL_EVENT_TYPES)

#: Seconds between heartbeats when no events are published.
_HEARTBEAT_INTERVAL: float = 30.0

# ---------------------------------------------------------------------------
# Module-level per-IP tracker (mirrors legacy _sse_tracker on /ws path).
# max_per_ip=5 matches legacy SSEConnectionTracker(max_per_ip=5).
# ---------------------------------------------------------------------------

_ip_tracker = SSEConnectionTracker(max_per_ip=5)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _get_auth_key() -> Optional[str]:
    """Return the configured auth key, or None when auth is disabled."""
    return os.environ.get("AF_API_AUTH_KEY") or None


def _check_ws_auth(token: Optional[str]) -> bool:
    """Return True if the connection is allowed.

    When AF_API_AUTH_KEY is unset, always returns True (auth disabled).
    When set, *token* must match via hmac.compare_digest.
    """
    key = _get_auth_key()
    if key is None:
        return True
    if not token:
        return False
    return hmac.compare_digest(token, key)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["ws"])


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Native Starlette WebSocket route — protocol-equivalent to legacy /ws.

    1. Auth check (before accept): wrong/missing ?token= → close(4403).
    2. Per-IP cap (before accept): over-cap → close(4429).
    3. Subscribe to all four event bus types by default.
    4. Run receive loop: dispatch subscribe/unsubscribe/ping commands.
    5. Push bus events as JSON text frames; heartbeat every 30 s.
    6. Unsubscribe from bus + release IP slot on disconnect (finally block).
    """
    # --- Auth (before accept, so client sees a rejection close frame) ---
    token: Optional[str] = websocket.query_params.get("token")
    if not _check_ws_auth(token):
        await websocket.close(code=4403)
        return

    # --- Per-IP cap ---
    client_ip: str = websocket.client.host if websocket.client else "127.0.0.1"
    if not _ip_tracker.acquire(client_ip):
        await websocket.close(code=4429)
        return

    # --- Accept (Starlette handles the HTTP 101 / RFC-6455 handshake) ---
    await websocket.accept()

    # --- Async queue for the bus → async bridge ---
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    # Active subscriptions (mutable; updated by subscribe/unsubscribe commands).
    subscribed: set[str] = set(_ALL_EVENT_TYPE_NAMES)

    # --- Subscribe to the event bus ---
    # Each callback checks whether the event type is currently subscribed before
    # enqueuing so we don't push unwanted events.
    sub_ids: list[str] = []
    bus = get_bus()

    for event_class, event_type_name in _ALL_EVENT_TYPES:
        def _make_callback(name: str):
            def _cb(event) -> None:
                if name not in subscribed:
                    return
                data = event.to_dict()
                data["_event_type"] = name
                loop.call_soon_threadsafe(queue.put_nowait, data)
            return _cb

        sid = bus.subscribe(event_class, _make_callback(event_type_name))
        sub_ids.append(sid)

    # --- Sender task: drain the queue and push frames; heartbeat on idle ---
    async def _sender() -> None:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                await websocket.send_text(json.dumps(data, default=str))
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "heartbeat"})
                except Exception:  # noqa: BLE001
                    break

    sender_task = asyncio.create_task(_sender())

    # --- Receive loop: dispatch inbound commands ---
    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            # Parse JSON command
            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "invalid JSON"})
                continue

            if not isinstance(cmd, dict):
                await websocket.send_json({"type": "error", "message": "expected JSON object"})
                continue

            cmd_type = cmd.get("type", "")

            if cmd_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif cmd_type == "subscribe":
                events = cmd.get("events", [])
                if isinstance(events, list):
                    for ev in events:
                        if ev in _ALL_EVENT_TYPE_NAMES:
                            subscribed.add(ev)
                await websocket.send_json({"type": "subscribed", "events": list(subscribed)})

            elif cmd_type == "unsubscribe":
                events = cmd.get("events", [])
                if isinstance(events, list):
                    for ev in events:
                        subscribed.discard(ev)
                await websocket.send_json({"type": "unsubscribed", "events": list(subscribed)})

            else:
                await websocket.send_json(
                    {"type": "error", "message": f"unknown command: {cmd_type!r}"}
                )

    finally:
        # Cancel sender task
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):
            pass

        # Unsubscribe from the event bus
        for sid in sub_ids:
            try:
                bus.unsubscribe(sid)
            except Exception:  # noqa: BLE001
                pass

        # Release the per-IP slot
        _ip_tracker.release(client_ip)
