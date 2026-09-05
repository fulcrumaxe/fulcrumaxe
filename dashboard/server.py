"""
dashboard/server.py — HTTP/SSE bridge for the autonomous-forever backend.

Reads .autonomous-team/agent-feed.jsonl directly via tail-follow and fans
events out to connected SSE clients. No subprocess spawning — no LLM key
required to start this service.

Usage:
    python dashboard/server.py [--port 8420]

Environment:
    AF_SSE_PORT         Override default port 8420 (preferred; set by start-dashboard.sh)
    AF_DASHBOARD_PORT   Override default port 8420 (legacy alias)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from aiohttp import web

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DASHBOARD_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DASHBOARD_DIR.parent
_STATIC_DIR = _DASHBOARD_DIR / "static"
_AGENT_FEED = _REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"

# ---------------------------------------------------------------------------
# Global server state
# ---------------------------------------------------------------------------
class ServerState:
    def __init__(self) -> None:
        self.ready: bool = True  # always ready — no subprocess dependency
        self.start_time: float = time.time()
        self.last_event_at: float | None = None
        # Set of asyncio.Queue — one per connected SSE client.
        self.sse_clients: set[asyncio.Queue] = set()

    def uptime_s(self) -> float:
        return time.time() - self.start_time

    def connected_clients(self) -> int:
        return len(self.sse_clients)

    def fan_out(self, event: dict[str, Any]) -> None:
        """Push an event to every connected SSE client queue."""
        self.last_event_at = time.time()
        dead: list[asyncio.Queue] = []
        for q in self.sse_clients:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.sse_clients.discard(q)


STATE = ServerState()

# ---------------------------------------------------------------------------
# agent-feed.jsonl tail-follower
# ---------------------------------------------------------------------------
async def _tail_feed() -> None:
    """
    Tail .autonomous-team/agent-feed.jsonl, emitting each new JSON line as
    an SSE event to all connected clients.

    Runs forever as a background task. If the file doesn't exist yet it waits
    for it to appear. On rotation (file shrinks) it resets to position 0.
    """
    feed_path = _AGENT_FEED
    file_pos: int = 0

    while True:
        try:
            if not feed_path.exists():
                await asyncio.sleep(2)
                continue

            stat = feed_path.stat()
            # Detect rotation/truncation — reset to start.
            if stat.st_size < file_pos:
                print("[dashboard] agent-feed.jsonl rotated — resetting position", flush=True)
                file_pos = 0

            if stat.st_size == file_pos:
                # Nothing new — wait a moment and retry.
                await asyncio.sleep(0.5)
                continue

            with feed_path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(file_pos)
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        event: dict[str, Any] = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        print(f"[dashboard] feed parse error: {exc} — raw: {raw[:120]}", flush=True)
                        continue
                    STATE.fan_out(event)
                file_pos = fh.tell()

        except OSError as exc:
            print(f"[dashboard] feed read error: {exc}", flush=True)
            await asyncio.sleep(2)
        except Exception as exc:
            print(f"[dashboard] unexpected feed error: {exc}", flush=True)
            await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------
async def handle_index(request: web.Request) -> web.Response:
    index_path = _STATIC_DIR / "index.html"
    return web.FileResponse(index_path)


async def handle_static(request: web.Request) -> web.FileResponse:
    filename = request.match_info["path"]
    file_path = _STATIC_DIR / filename
    # Prevent path traversal.
    try:
        file_path.resolve().relative_to(_STATIC_DIR.resolve())
    except ValueError:
        raise web.HTTPForbidden()
    if not file_path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(file_path)


async def handle_status(request: web.Request) -> web.Response:
    data = {
        "ready": STATE.ready,
        "uptime_s": round(STATE.uptime_s(), 1),
        "connected_clients": STATE.connected_clients(),
        "last_event_at": STATE.last_event_at,
        "feed_path": str(_AGENT_FEED),
        "feed_exists": _AGENT_FEED.exists(),
    }
    return web.json_response(data)


async def handle_feed(request: web.Request) -> web.StreamResponse:
    """GET /api/feed — SSE endpoint streaming agent-feed.jsonl events."""
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind proxy
        },
    )
    await response.prepare(request)

    # Send an initial comment to establish the connection.
    await response.write(b": connected\n\n")

    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
    STATE.sse_clients.add(queue)
    print(f"[dashboard] SSE client connected — {STATE.connected_clients()} total", flush=True)

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                # Send a keepalive comment so the connection stays alive.
                try:
                    await response.write(b": keepalive\n\n")
                except Exception:
                    break
                continue

            try:
                data = json.dumps(event, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode("utf-8"))
            except Exception:
                break
    finally:
        STATE.sse_clients.discard(queue)
        print(f"[dashboard] SSE client disconnected — {STATE.connected_clients()} remaining", flush=True)

    return response


# Alias /events → /api/feed (spec says GET http://127.0.0.1:8420/events)
async def handle_events(request: web.Request) -> web.StreamResponse:
    return await handle_feed(request)


async def handle_prompt(request: web.Request) -> web.Response:
    """POST /api/prompt — LLM prompts require a running backend/server.py subprocess.

    Since dashboard/server.py no longer spawns that subprocess, LLM-dependent
    RPCs return a structured error. Use backend/server.py --http on port 8765
    for JSON-RPC LLM access.
    """
    return web.json_response(
        {"error": "LLM unavailable", "hint": "Use the JSON-RPC endpoint at http://localhost:8765/rpc"},
        status=503,
    )


async def handle_loop_trigger(request: web.Request) -> web.Response:
    """POST /api/loop/trigger — LLM-dependent; returns structured error."""
    return web.json_response(
        {"error": "LLM unavailable", "hint": "Use the JSON-RPC endpoint at http://localhost:8765/rpc"},
        status=503,
    )


async def handle_spawn_blocks(request: web.Request) -> web.Response:
    """GET /api/spawn-blocks?limit=10 — return recent spawn_blocked events from agent-feed.jsonl."""
    try:
        limit = int(request.rel_url.query.get("limit", "10"))
    except (ValueError, TypeError):
        limit = 10
    limit = max(1, min(limit, 50))

    feed_path = _AGENT_FEED
    results: list[dict[str, Any]] = []

    if feed_path.exists():
        try:
            # Read up to the last 5000 lines for safety, then filter
            lines: list[str] = []
            with feed_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    lines.append(line)
                    if len(lines) > 5000:
                        lines.pop(0)

            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("event_type") == "spawn_blocked":
                    results.append(event)
                    if len(results) >= limit:
                        break
        except OSError:
            pass  # feed not readable — return empty list

    return web.json_response(results)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/static/{path:.+}", handle_static)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/feed", handle_feed)
    app.router.add_get("/events", handle_events)
    app.router.add_post("/api/prompt", handle_prompt)
    app.router.add_post("/api/loop/trigger", handle_loop_trigger)
    app.router.add_get("/api/spawn-blocks", handle_spawn_blocks)
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous-forever dashboard SSE bridge")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AF_SSE_PORT") or os.environ.get("AF_DASHBOARD_PORT", "8420")),
        help="Port to listen on (default: 8420, or AF_SSE_PORT / AF_DASHBOARD_PORT env var)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    args = parser.parse_args()

    async def _run() -> None:
        app = _make_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await site.start()

        url = f"http://{args.host}:{args.port}"
        print(f"[dashboard] listening on {url}", flush=True)
        print(f"[dashboard] tailing feed: {_AGENT_FEED}", flush=True)

        # Start tail-follower background task.
        asyncio.create_task(_tail_feed(), name="feed-tail")

        # Handle SIGTERM/SIGINT for graceful shutdown.
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            print("\n[dashboard] shutdown signal received", flush=True)
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)

        await stop_event.wait()

        print("[dashboard] shutting down...", flush=True)
        await runner.cleanup()
        print("[dashboard] bye", flush=True)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
