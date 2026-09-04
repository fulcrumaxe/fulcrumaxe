"""
FastAPI router — SSE streaming routes.

Migrates /stream/feed, /stream/status, and /stream/events from the legacy
ThreadingHTTPServer onto async FastAPI routes.

Wire format is BYTE-EQUIVALENT to legacy (api.py:2094-2098):
    data: <json.dumps(data, default=str)>\n\n
Heartbeat:
    data: {"type":"heartbeat"}\n\n

Cadence:
    /stream/feed   — push each AgentOutputEvent; 30s idle heartbeat
    /stream/events — push all 4 event types with _event_type; 30s idle heartbeat
    /stream/status — push status snapshot every 10s (no idle heartbeat needed)

Bus→async bridge:
    The event bus is synchronous.  We bridge with asyncio.Queue +
    loop.call_soon_threadsafe(queue.put_nowait, event) inside the subscriber
    callback.  The async generator awaits with asyncio.wait_for(..., timeout=30)
    so heartbeats fire when the bus is quiet.  Unsubscribe in a finally block
    that runs on client disconnect (Starlette cancels the generator →
    GeneratorExit/CancelledError → finally executes).

Cap discipline:
    Acquire BOTH GlobalStreamLimiter and SSEConnectionTracker on connect;
    over-cap → 503.  Release BOTH in the generator finally block so a token
    leak is impossible even on abrupt client disconnect.

Auth:
    Legacy places /stream/* after the _check_auth() gate (api.py:2671), so
    these routes require authentication via Depends(require_auth).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import AsyncIterator, Optional

import anyio.to_thread
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from backend.budget import BudgetTracker
from backend.deps.auth import require_auth
from backend.deps.shared_limiter import get_shared_limiter
from backend.deps.stream_limiter import GlobalStreamLimiter
from backend.event_bus import (
    AgentOutputEvent,
    BudgetSpendEvent,
    GateChangeEvent,
    LoopIterationEvent,
    get_bus,
)
from backend.rate_limiter import SSEConnectionTracker
from backend.registry import DiscussionRegistry

# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------

#: Metrics file path — same definition as api.py:142
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_METRICS_FILE = _REPO_ROOT / ".autonomous-team" / "loop-metrics.jsonl"

#: CWE-22 project name guard — same regex as api.py:157
_VALID_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_project_name(name: str) -> bool:
    return bool(_VALID_PROJECT_NAME_RE.fullmatch(name))


#: Per-IP tracker — separate per-route, replaced in tests via monkeypatch.
_ip_tracker = SSEConnectionTracker(max_per_ip=5)

# ---------------------------------------------------------------------------
# Shared global stream limiter — points to the singleton in deps/shared_limiter.py
# so that /stream/* routes and the /feed,/events aliases all count against the
# SAME cap and AF_GLOBAL_STREAM_CAP controls all of them.
#
# Previously this was a SEPARATE GlobalStreamLimiter(max_global=20) that ignored
# AF_GLOBAL_STREAM_CAP — that was the fragmentation bug fixed in PR2.
#
# Tests can still monkeypatch ``smod._global_limiter`` directly; that pattern
# continues to work because ``_get_global_limiter()`` reads this attribute.
# ---------------------------------------------------------------------------

#: Module-level reference to the shared limiter.  Tests may replace this via
#: ``smod._global_limiter = <fake>``.  Cap defaults to 40 (was 20).
_global_limiter: GlobalStreamLimiter = get_shared_limiter()


def _get_global_limiter() -> GlobalStreamLimiter:
    """Return the current global stream limiter (shared singleton by default)."""
    return _global_limiter

# SSE wire format constants
_HEARTBEAT: bytes = b'data: {"type":"heartbeat"}\n\n'
_HEARTBEAT_INTERVAL: float = 30.0   # seconds between heartbeats when idle
_STATUS_INTERVAL: float = 10.0      # seconds between status pushes


def _sse_bytes(data: object) -> bytes:
    """Encode *data* as one SSE data frame, byte-equivalent to legacy."""
    return ("data: " + json.dumps(data, default=str) + "\n\n").encode("utf-8")


def _client_ip(request: Request) -> str:
    """Return best-effort client IP (no X-Forwarded-For trust)."""
    if request.client:
        return request.client.host
    return "127.0.0.1"


# ---------------------------------------------------------------------------
# KPI cache (mirrors api.py — imported lazily to avoid circular deps)
# ---------------------------------------------------------------------------

def _get_cached_kpi() -> dict:
    """Delegate to the cached KPI function in api.py to avoid reimplementing."""
    try:
        from backend import api as _api  # noqa: PLC0415
        return _api._get_cached_kpi()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    tags=["streams"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# /stream/feed
# ---------------------------------------------------------------------------

@router.get("/stream/feed")
async def stream_feed(
    request: Request,
    project: Optional[str] = Query(default=None),
) -> Response:
    """SSE stream of agent-feed events.

    Default mode: subscribes to the in-process AgentOutputEvent bus.
    ?project=X mode: tails that project's agent-feed.jsonl file.
    """
    ip = _client_ip(request)

    # Cap checks — both must succeed before we start streaming.
    if not _get_global_limiter().acquire():
        return Response(
            content='{"detail":"stream cap reached"}',
            status_code=503,
            media_type="application/json",
        )
    if not _ip_tracker.acquire(ip):
        _get_global_limiter().release()
        return Response(
            content='{"detail":"too many connections from this IP"}',
            status_code=503,
            media_type="application/json",
        )

    # CWE-22: validate project name before using as path component.
    if project is not None and not _validate_project_name(project):
        _ip_tracker.release(ip)
        _get_global_limiter().release()
        return Response(
            content=json.dumps({"detail": f"invalid project name: {project!r}"}),
            status_code=400,
            media_type="application/json",
        )

    # Resolve feed file for ?project= mode.
    feed_file: Optional[Path] = None
    if project:
        try:
            from backend.state_paths import for_project as _fp  # noqa: PLC0415
            feed_file = _fp(project).state_dir / "agent-feed.jsonl"
        except Exception:  # noqa: BLE001
            feed_file = None

    async def _generate() -> AsyncIterator[bytes]:
        try:
            if feed_file is not None:
                # File-tail mode: poll asynchronously so the event loop is free.
                async for chunk in _file_tail_gen(feed_file):
                    yield chunk
            else:
                # Bus-subscribe mode: bridge sync bus to async queue.
                async for chunk in _bus_gen([AgentOutputEvent]):
                    yield chunk
        finally:
            _ip_tracker.release(ip)
            _get_global_limiter().release()

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
# /stream/events
# ---------------------------------------------------------------------------

@router.get("/stream/events")
async def stream_events(request: Request) -> Response:
    """SSE stream of ALL event bus event types with _event_type field."""
    ip = _client_ip(request)

    if not _get_global_limiter().acquire():
        return Response(
            content='{"detail":"stream cap reached"}',
            status_code=503,
            media_type="application/json",
        )
    if not _ip_tracker.acquire(ip):
        _get_global_limiter().release()
        return Response(
            content='{"detail":"too many connections from this IP"}',
            status_code=503,
            media_type="application/json",
        )

    event_types = [AgentOutputEvent, BudgetSpendEvent, GateChangeEvent, LoopIterationEvent]

    async def _generate() -> AsyncIterator[bytes]:
        try:
            async for chunk in _bus_gen(event_types, add_event_type=True):
                yield chunk
        finally:
            _ip_tracker.release(ip)
            _get_global_limiter().release()

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
# /stream/status
# ---------------------------------------------------------------------------

@router.get("/stream/status")
async def stream_status(request: Request) -> Response:
    """SSE stream of status snapshots pushed every 10 seconds."""
    ip = _client_ip(request)

    if not _get_global_limiter().acquire():
        return Response(
            content='{"detail":"stream cap reached"}',
            status_code=503,
            media_type="application/json",
        )
    if not _ip_tracker.acquire(ip):
        _get_global_limiter().release()
        return Response(
            content='{"detail":"too many connections from this IP"}',
            status_code=503,
            media_type="application/json",
        )

    async def _generate() -> AsyncIterator[bytes]:
        try:
            while True:
                snapshot: dict = {}

                # Budget — read via thread so we don't block the event loop.
                def _budget() -> object:
                    try:
                        return BudgetTracker().get_status()
                    except Exception:  # noqa: BLE001
                        return None

                snapshot["budget"] = await anyio.to_thread.run_sync(_budget)

                # Queue counts.
                def _queue() -> object:
                    try:
                        reg = DiscussionRegistry()
                        stats = reg.stats()
                        return {k: stats.get(k, 0) for k in ("SPEC_READY", "IMPLEMENTING", "REVIEWING")}
                    except Exception:  # noqa: BLE001
                        return None

                snapshot["queue"] = await anyio.to_thread.run_sync(_queue)

                # Loop-ago — last line of loop-metrics.jsonl.
                def _loop_ago() -> object:
                    try:
                        if not _METRICS_FILE.exists():
                            return None
                        last_line = ""
                        with _METRICS_FILE.open("r") as fh:
                            for line in fh:
                                stripped = line.strip()
                                if stripped:
                                    last_line = stripped
                        if last_line:
                            return json.loads(last_line)
                        return None
                    except Exception:  # noqa: BLE001
                        return None

                snapshot["loop_ago"] = await anyio.to_thread.run_sync(_loop_ago)

                # KPI snapshot.
                def _kpi() -> object:
                    try:
                        return _get_cached_kpi()
                    except Exception:  # noqa: BLE001
                        return None

                snapshot["kpi"] = await anyio.to_thread.run_sync(_kpi)

                yield _sse_bytes(snapshot)
                await asyncio.sleep(_STATUS_INTERVAL)
        finally:
            _ip_tracker.release(ip)
            _get_global_limiter().release()

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
# Shared async generator helpers
# ---------------------------------------------------------------------------

async def _bus_gen(
    event_types: list,
    add_event_type: bool = False,
) -> AsyncIterator[bytes]:
    """Bridge the synchronous event bus to an async generator.

    Subscribes to *event_types* on the in-process bus via a sync callback
    that posts events to an asyncio.Queue using call_soon_threadsafe.
    Yields SSE frames; sends a heartbeat after _HEARTBEAT_INTERVAL of silence.
    Unsubscribes all subscriptions in a finally block on generator close.
    """
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    sub_ids: list[str] = []
    for et in event_types:
        sid = get_bus().subscribe(et, lambda e: loop.call_soon_threadsafe(queue.put_nowait, e))
        sub_ids.append(sid)

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                data = event.to_dict()
                if add_event_type:
                    data["_event_type"] = type(event).__name__
                yield _sse_bytes(data)
            except asyncio.TimeoutError:
                yield _HEARTBEAT
    finally:
        for sid in sub_ids:
            get_bus().unsubscribe(sid)


async def _file_tail_gen(feed_file: Path) -> AsyncIterator[bytes]:
    """Poll *feed_file* for new JSONL lines, yielding SSE frames.

    Seeks to the end on open so only events written after connect are emitted.
    File reads happen on a thread via anyio.to_thread.run_sync so the event
    loop is never blocked.  Emits a heartbeat when no new events arrive for
    _HEARTBEAT_INTERVAL seconds.
    """
    POLL_INTERVAL = 0.5

    # Open file and seek to end in a thread.
    def _open_seek() -> object:
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
                # File didn't exist yet; try to open it.
                fh = await anyio.to_thread.run_sync(_open_seek)
            else:
                def _read_lines(f) -> list[str]:  # type: ignore[type-arg]
                    lines = []
                    try:
                        for raw in f:
                            raw = raw.strip()
                            if raw:
                                lines.append(raw)
                    except OSError:
                        pass
                    return lines

                # Read is synchronous but fast; run on thread to stay async-safe.
                lines = await anyio.to_thread.run_sync(lambda: _read_lines(fh))
                for raw in lines:
                    try:
                        ev = json.loads(raw)
                        yield _sse_bytes(ev)
                        new_events = True
                    except (json.JSONDecodeError, OSError):
                        continue

            now = asyncio.get_event_loop().time()
            if not new_events and (now - last_heartbeat) >= _HEARTBEAT_INTERVAL:
                yield _HEARTBEAT
                last_heartbeat = now

            await asyncio.sleep(POLL_INTERVAL)
    finally:
        if fh is not None:
            def _close(f) -> None:  # type: ignore[type-arg]
                try:
                    f.close()
                except OSError:
                    pass
            await anyio.to_thread.run_sync(lambda: _close(fh))
