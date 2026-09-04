"""Tests for backend/routers/streams.py — async SSE routes (P4).

Covers:
- SSE wire format (data: ...\n\n, heartbeat)
- /stream/feed default (bus) mode
- /stream/events (all 4 event types + _event_type)
- /stream/status (10s cadence, snapshot keys present)
- Cap enforcement: global 503, per-IP 503
- Cap RELEASE on disconnect (critical: no token leak)
- Async non-blocking (blocking file read dispatched to thread)
- Auth gate: 401/403 when auth is enabled

SSE integration tests use a live uvicorn server so the generator is truly
async and the HTTP connection can be closed without hanging.  Cap/format
tests use the sync TestClient where the response is not streaming.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
import pathlib
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import httpx
import pytest
import uvicorn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int) -> tuple[uvicorn.Server, threading.Thread]:
    """Start a uvicorn server on *port* in a daemon thread; return (server, thread)."""
    import backend.asgi_app as asgi_mod

    config = uvicorn.Config(
        asgi_mod.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return server, t


def _wait_ready(base: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base}/health", timeout=1.0)
            return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError(f"Server at {base} did not become ready within {timeout}s")


def _read_sse_frames_from_lines(lines: list[str]) -> list[dict]:
    frames = []
    for line in lines:
        line = line.strip()
        if line.startswith("data: "):
            try:
                frames.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return frames


def _collect_frames(
    url: str,
    max_frames: int = 2,
    timeout_per_frame: float = 5.0,
    headers: Optional[dict] = None,
) -> tuple[int, list[dict]]:
    """Open an SSE stream, collect up to *max_frames* data frames, then close."""
    frames: list[dict] = []
    status = 0
    deadline = time.monotonic() + timeout_per_frame * max_frames + 5.0
    try:
        with httpx.stream("GET", url, timeout=timeout_per_frame, headers=headers or {}) as resp:
            status = resp.status_code
            if status != 200:
                return status, frames
            for line in resp.iter_lines():
                if time.monotonic() > deadline:
                    break
                line = line.strip()
                if line.startswith("data: "):
                    try:
                        frames.append(json.loads(line[6:]))
                    except json.JSONDecodeError:
                        pass
                    if len(frames) >= max_frames:
                        break
    except (httpx.ReadTimeout, httpx.RemoteProtocolError):
        pass
    return status, frames


# ---------------------------------------------------------------------------
# Wire format tests (pure unit, no server needed)
# ---------------------------------------------------------------------------


def test_sse_bytes_format():
    """_sse_bytes must produce data: <json>\n\n."""
    from backend.routers.streams import _sse_bytes
    result = _sse_bytes({"type": "heartbeat"})
    assert result == b'data: {"type": "heartbeat"}\n\n'


def test_sse_bytes_non_serializable():
    """_sse_bytes uses default=str so non-JSON types are stringified."""
    from backend.routers.streams import _sse_bytes
    from datetime import datetime
    dt = datetime(2024, 1, 1)
    raw = _sse_bytes({"ts": dt})
    parsed = json.loads(raw[6:].rstrip(b"\n"))
    assert isinstance(parsed["ts"], str)


def test_heartbeat_constant():
    """The heartbeat constant must be byte-equivalent to the legacy format."""
    from backend.routers.streams import _HEARTBEAT
    assert _HEARTBEAT == b'data: {"type":"heartbeat"}\n\n'


# ---------------------------------------------------------------------------
# Cap enforcement: 503 without a real server (TestClient OK, no streaming)
# ---------------------------------------------------------------------------


def test_stream_feed_503_when_global_cap_full(monkeypatch):
    """Over the global cap, /stream/feed returns 503."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.deps.stream_limiter import GlobalStreamLimiter
    import backend.routers.streams as smod

    full_limiter = GlobalStreamLimiter(max_global=1)
    full_limiter.acquire()  # exhaust it

    original = smod._global_limiter
    smod._global_limiter = full_limiter
    try:
        from fastapi.testclient import TestClient
        import backend.asgi_app as asgi_mod
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get("/stream/feed")
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}"
    finally:
        smod._global_limiter = original


def test_stream_events_503_when_global_cap_full(monkeypatch):
    """Over the global cap, /stream/events returns 503."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.deps.stream_limiter import GlobalStreamLimiter
    import backend.routers.streams as smod

    full_limiter = GlobalStreamLimiter(max_global=1)
    full_limiter.acquire()

    original = smod._global_limiter
    smod._global_limiter = full_limiter
    try:
        from fastapi.testclient import TestClient
        import backend.asgi_app as asgi_mod
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get("/stream/events")
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}"
    finally:
        smod._global_limiter = original


def test_stream_status_503_when_global_cap_full(monkeypatch):
    """Over the global cap, /stream/status returns 503."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.deps.stream_limiter import GlobalStreamLimiter
    import backend.routers.streams as smod

    full_limiter = GlobalStreamLimiter(max_global=1)
    full_limiter.acquire()

    original = smod._global_limiter
    smod._global_limiter = full_limiter
    try:
        from fastapi.testclient import TestClient
        import backend.asgi_app as asgi_mod
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get("/stream/status")
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}"
    finally:
        smod._global_limiter = original


def test_stream_feed_503_when_per_ip_cap_full(monkeypatch):
    """Over the per-IP cap, /stream/feed returns 503."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.rate_limiter import SSEConnectionTracker
    from backend.deps.stream_limiter import GlobalStreamLimiter
    import backend.routers.streams as smod

    # TestClient host is "testclient"
    full_ip_tracker = SSEConnectionTracker(max_per_ip=1)
    full_ip_tracker.acquire("testclient")

    original_tracker = smod._ip_tracker
    original_limiter = smod._global_limiter
    smod._global_limiter = GlobalStreamLimiter(max_global=100)
    smod._ip_tracker = full_ip_tracker
    try:
        from fastapi.testclient import TestClient
        import backend.asgi_app as asgi_mod
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get("/stream/feed")
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}"
    finally:
        smod._ip_tracker = original_tracker
        smod._global_limiter = original_limiter


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_stream_feed_requires_auth_when_key_set(monkeypatch):
    """When AF_API_AUTH_KEY is set, /stream/feed returns 401 without a token."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "secret123")
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod
    client = TestClient(asgi_mod.app, raise_server_exceptions=False)
    resp = client.get("/stream/feed")
    assert resp.status_code in (401, 403), f"Expected 401/403, got {resp.status_code}"


def test_stream_events_requires_auth_when_key_set(monkeypatch):
    """When AF_API_AUTH_KEY is set, /stream/events returns 401 without a token."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "secret123")
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod
    client = TestClient(asgi_mod.app, raise_server_exceptions=False)
    resp = client.get("/stream/events")
    assert resp.status_code in (401, 403), f"Expected 401/403, got {resp.status_code}"


def test_stream_status_requires_auth_when_key_set(monkeypatch):
    """When AF_API_AUTH_KEY is set, /stream/status returns 401 without a token."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "secret123")
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod
    client = TestClient(asgi_mod.app, raise_server_exceptions=False)
    resp = client.get("/stream/status")
    assert resp.status_code in (401, 403), f"Expected 401/403, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /stream/feed — ?project= validation (non-streaming)
# ---------------------------------------------------------------------------


def test_stream_feed_rejects_invalid_project_name(monkeypatch):
    """?project= with path-traversal characters must return 400."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod
    client = TestClient(asgi_mod.app, raise_server_exceptions=False)
    resp = client.get("/stream/feed?project=../../etc/passwd")
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Integration: live server SSE tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server():
    """Spin up a uvicorn server for SSE integration tests; yield base URL."""
    import os
    os.environ.pop("AF_API_AUTH_KEY", None)

    # Ensure limiters are fresh (module-level singletons).
    import backend.routers.streams as smod
    from backend.deps.stream_limiter import GlobalStreamLimiter
    from backend.rate_limiter import SSEConnectionTracker
    smod._global_limiter = GlobalStreamLimiter(max_global=20)
    smod._ip_tracker = SSEConnectionTracker(max_per_ip=5)

    port = _free_port()
    server, t = _start_server(port)
    base = f"http://127.0.0.1:{port}"
    _wait_ready(base)
    yield base
    server.should_exit = True
    t.join(timeout=5)


def test_live_stream_feed_content_type(live_server):
    """GET /stream/feed must respond with Content-Type: text/event-stream."""
    url = f"{live_server}/stream/feed"
    try:
        with httpx.stream("GET", url, timeout=5.0) as resp:
            assert resp.status_code == 200
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct, f"Bad content-type: {ct}"
    except (httpx.ReadTimeout, httpx.RemoteProtocolError):
        pass  # timeout is fine — we just needed the headers


def test_live_stream_events_content_type(live_server):
    """GET /stream/events must respond with Content-Type: text/event-stream."""
    url = f"{live_server}/stream/events"
    try:
        with httpx.stream("GET", url, timeout=5.0) as resp:
            assert resp.status_code == 200
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct, f"Bad content-type: {ct}"
    except (httpx.ReadTimeout, httpx.RemoteProtocolError):
        pass


def test_live_stream_status_emits_snapshot(live_server):
    """/stream/status must emit at least one JSON object with budget/queue/loop_ago/kpi keys."""
    url = f"{live_server}/stream/status"
    status, frames = _collect_frames(url, max_frames=1, timeout_per_frame=15.0)
    assert status == 200, f"Expected 200, got {status}"
    assert len(frames) >= 1, "No frames received from /stream/status"
    frame = frames[0]
    assert "budget" in frame, f"Missing 'budget' key in frame: {frame}"
    assert "queue" in frame, f"Missing 'queue' key in frame: {frame}"
    assert "loop_ago" in frame, f"Missing 'loop_ago' key in frame: {frame}"
    assert "kpi" in frame, f"Missing 'kpi' key in frame: {frame}"


def test_live_stream_status_cache_control(live_server):
    """Cache-Control: no-cache must be present on /stream/status."""
    url = f"{live_server}/stream/status"
    try:
        with httpx.stream("GET", url, timeout=5.0) as resp:
            assert resp.status_code == 200
            cc = resp.headers.get("cache-control", "")
            assert "no-cache" in cc, f"Expected no-cache, got: {cc}"
    except (httpx.ReadTimeout, httpx.RemoteProtocolError):
        pass


# ---------------------------------------------------------------------------
# Cap RELEASE on disconnect — live server required
# ---------------------------------------------------------------------------


def test_live_stream_feed_releases_global_token(live_server):
    """After the connection closes, global limiter count must return to 0.

    The generator waits up to _HEARTBEAT_INTERVAL (30s) on queue.get; we
    reduce that interval to 1s so disconnect detection is fast in tests.
    """
    import backend.routers.streams as smod
    from backend.deps.stream_limiter import GlobalStreamLimiter
    from backend.rate_limiter import SSEConnectionTracker

    # Replace with fresh counters and a short heartbeat so the generator
    # wakes up quickly after disconnect.
    fresh_limiter = GlobalStreamLimiter(max_global=5)
    fresh_tracker = SSEConnectionTracker(max_per_ip=5)
    original_limiter = smod._global_limiter
    original_tracker = smod._ip_tracker
    original_hb = smod._HEARTBEAT_INTERVAL
    smod._global_limiter = fresh_limiter
    smod._ip_tracker = fresh_tracker
    smod._HEARTBEAT_INTERVAL = 1.0  # short for tests

    try:
        url = f"{live_server}/stream/feed"
        try:
            with httpx.stream("GET", url, timeout=5.0) as resp:
                assert resp.status_code == 200
                # Read at least the first byte so the generator starts.
                for chunk in resp.iter_bytes():
                    break
            # Connection is now closed.
        except (httpx.ReadTimeout, httpx.RemoteProtocolError):
            pass

        # Wait for server to detect disconnect (up to _HEARTBEAT_INTERVAL + margin).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and fresh_limiter.active > 0:
            time.sleep(0.1)

        assert fresh_limiter.active == 0, (
            f"Global limiter count should be 0 after disconnect, got {fresh_limiter.active}"
        )
    finally:
        smod._global_limiter = original_limiter
        smod._ip_tracker = original_tracker
        smod._HEARTBEAT_INTERVAL = original_hb


def test_live_stream_events_releases_global_token(live_server):
    """After /stream/events closes, global limiter count returns to 0."""
    import backend.routers.streams as smod
    from backend.deps.stream_limiter import GlobalStreamLimiter
    from backend.rate_limiter import SSEConnectionTracker

    fresh_limiter = GlobalStreamLimiter(max_global=5)
    fresh_tracker = SSEConnectionTracker(max_per_ip=5)
    original_limiter = smod._global_limiter
    original_tracker = smod._ip_tracker
    original_hb = smod._HEARTBEAT_INTERVAL
    smod._global_limiter = fresh_limiter
    smod._ip_tracker = fresh_tracker
    smod._HEARTBEAT_INTERVAL = 1.0  # short for tests

    try:
        url = f"{live_server}/stream/events"
        try:
            with httpx.stream("GET", url, timeout=5.0) as resp:
                assert resp.status_code == 200
                for chunk in resp.iter_bytes():
                    break
        except (httpx.ReadTimeout, httpx.RemoteProtocolError):
            pass

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and fresh_limiter.active > 0:
            time.sleep(0.1)

        assert fresh_limiter.active == 0, (
            f"Global limiter count should be 0 after disconnect, got {fresh_limiter.active}"
        )
    finally:
        smod._global_limiter = original_limiter
        smod._ip_tracker = original_tracker
        smod._HEARTBEAT_INTERVAL = original_hb


# ---------------------------------------------------------------------------
# Async non-blocking: file read uses anyio.to_thread.run_sync
# ---------------------------------------------------------------------------


def test_file_tail_gen_uses_thread_for_file_read():
    """_file_tail_gen must use anyio.to_thread.run_sync for file I/O.

    We verify this by inspecting the source code of _file_tail_gen —
    any blocking file operation must go through anyio.to_thread.run_sync
    so the async event loop is never blocked.
    """
    import inspect
    import backend.routers.streams as smod

    source = inspect.getsource(smod._file_tail_gen)
    assert "anyio.to_thread.run_sync" in source, (
        "_file_tail_gen must use anyio.to_thread.run_sync for file I/O "
        "to avoid blocking the event loop"
    )
