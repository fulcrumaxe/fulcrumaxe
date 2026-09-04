"""Tests for PR2: /feed + /events SSE aliases, /dashboard, /, shared stream limiter.

Covers:
- /feed returns SSE (text/event-stream)
- /events returns SSE (text/event-stream)
- ?token= auth works for both (401 without, 403 wrong, 200 valid)
- /dashboard returns HTML 200
- / returns JSON 200
- Single shared limiter: /stream/*, /feed, /events all use the same instance
- Default cap == 40 when AF_GLOBAL_STREAM_CAP unset
- AF_GLOBAL_STREAM_CAP env var honoured by shared limiter
- /feed + /stream/feed share the cap (mixed-path 503 enforcement)

All tests are bounded with timeouts. No full suite run.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Optional
from unittest.mock import patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int):
    """Start a uvicorn server on *port* in a daemon thread."""
    import uvicorn
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


# ---------------------------------------------------------------------------
# Unit tests — no server needed (TestClient / fast path)
# ---------------------------------------------------------------------------


def test_feed_route_exists():
    """GET /feed must be registered on the FastAPI app.

    Verified via 401 (missing token when auth key is set) — fast check
    that proves the route exists without starting the SSE stream.
    """
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod

    os.environ["AF_API_AUTH_KEY"] = "probe-key"
    try:
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get("/feed")
        # 401 (no token) proves the route exists.
        assert resp.status_code != 404, f"GET /feed returned 404 — route not registered"
        assert resp.status_code != 405, f"GET /feed returned 405 — method not allowed"
    finally:
        os.environ.pop("AF_API_AUTH_KEY", None)


def test_events_route_exists():
    """GET /events must be registered on the FastAPI app.

    Verified via 401 (missing token when auth key is set) — fast check.
    """
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod

    os.environ["AF_API_AUTH_KEY"] = "probe-key"
    try:
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get("/events")
        assert resp.status_code != 404, f"GET /events returned 404 — route not registered"
        assert resp.status_code != 405, f"GET /events returned 405 — method not allowed"
    finally:
        os.environ.pop("AF_API_AUTH_KEY", None)


def test_dashboard_returns_html():
    """GET /dashboard must return 200 with text/html content."""
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod

    client = TestClient(asgi_mod.app, raise_server_exceptions=False)
    os.environ.pop("AF_API_AUTH_KEY", None)
    resp = client.get("/dashboard")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    ct = resp.headers.get("content-type", "")
    assert "text/html" in ct, f"Expected text/html, got {ct}"
    # Content sanity check.
    assert "fulcrumaxe" in resp.text.lower(), "Dashboard HTML missing project name"


def test_root_returns_json():
    """GET / must return 200 with JSON."""
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod

    client = TestClient(asgi_mod.app, raise_server_exceptions=False)
    os.environ.pop("AF_API_AUTH_KEY", None)
    resp = client.get("/")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    ct = resp.headers.get("content-type", "")
    assert "application/json" in ct, f"Expected application/json, got {ct}"
    body = resp.json()
    assert "name" in body or "status" in body, f"Root response missing expected keys: {body}"


# ---------------------------------------------------------------------------
# Auth tests for /feed and /events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/feed", "/events"])
def test_sse_alias_no_auth_no_key_passes(path):
    """When AF_API_AUTH_KEY is unset, /feed and /events pass without token.

    Uses a cap-full limiter to get a fast 503 (proves auth was accepted).
    """
    from fastapi.testclient import TestClient
    from backend.deps.stream_limiter import GlobalStreamLimiter
    import backend.routers.streams as smod
    import backend.routers.rpc_sse as rpc_sse_mod
    import backend.deps.shared_limiter as shl
    import backend.asgi_app as asgi_mod

    os.environ.pop("AF_API_AUTH_KEY", None)

    # Exhaust the cap so we get a fast 503 instead of hanging on the stream.
    tiny = GlobalStreamLimiter(max_global=1)
    tiny.acquire()
    original_smod = smod._global_limiter
    original_shl = shl._limiter
    smod._global_limiter = tiny
    shl._limiter = tiny
    try:
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get(path)
        # 503 (cap full, but auth passed) is the expected fast response.
        # 401/403 would mean auth failed — that's the bug.
        assert resp.status_code not in (401, 403), (
            f"Expected pass (no auth key set) → 503, got {resp.status_code} for {path}"
        )
    finally:
        smod._global_limiter = original_smod
        shl._limiter = original_shl


@pytest.mark.parametrize("path", ["/feed", "/events"])
def test_sse_alias_missing_token_returns_401(path):
    """When AF_API_AUTH_KEY is set and no token provided, returns 401."""
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod

    os.environ["AF_API_AUTH_KEY"] = "test-secret-key"
    try:
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get(path)
        assert resp.status_code == 401, (
            f"Expected 401 for missing token on {path}, got {resp.status_code}"
        )
    finally:
        os.environ.pop("AF_API_AUTH_KEY", None)


@pytest.mark.parametrize("path", ["/feed", "/events"])
def test_sse_alias_wrong_token_returns_403(path):
    """When ?token= is wrong, returns 403."""
    from fastapi.testclient import TestClient
    import backend.asgi_app as asgi_mod

    os.environ["AF_API_AUTH_KEY"] = "correct-key"
    try:
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get(f"{path}?token=wrong-key")
        assert resp.status_code == 403, (
            f"Expected 403 for wrong token on {path}, got {resp.status_code}"
        )
    finally:
        os.environ.pop("AF_API_AUTH_KEY", None)


@pytest.mark.parametrize("path", ["/feed", "/events"])
def test_sse_alias_valid_query_token_passes(path):
    """When ?token= matches the key, request is authorized.

    Uses a cap-full limiter to get a fast 503 (proves token was accepted).
    """
    from fastapi.testclient import TestClient
    from backend.deps.stream_limiter import GlobalStreamLimiter
    import backend.routers.streams as smod
    import backend.deps.shared_limiter as shl
    import backend.asgi_app as asgi_mod

    os.environ["AF_API_AUTH_KEY"] = "valid-key"
    tiny = GlobalStreamLimiter(max_global=1)
    tiny.acquire()
    original_smod = smod._global_limiter
    original_shl = shl._limiter
    smod._global_limiter = tiny
    shl._limiter = tiny
    try:
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get(f"{path}?token=valid-key")
        # 503 (cap) proves auth passed, not 401/403.
        assert resp.status_code not in (401, 403), (
            f"Expected auth pass for {path}?token=valid-key, got {resp.status_code}"
        )
    finally:
        smod._global_limiter = original_smod
        shl._limiter = original_shl
        os.environ.pop("AF_API_AUTH_KEY", None)


@pytest.mark.parametrize("path", ["/feed", "/events"])
def test_sse_alias_valid_bearer_header_passes(path):
    """Authorization: Bearer <key> header also works for /feed and /events.

    Uses a cap-full limiter to get a fast 503.
    """
    from fastapi.testclient import TestClient
    from backend.deps.stream_limiter import GlobalStreamLimiter
    import backend.routers.streams as smod
    import backend.deps.shared_limiter as shl
    import backend.asgi_app as asgi_mod

    os.environ["AF_API_AUTH_KEY"] = "bearer-key"
    tiny = GlobalStreamLimiter(max_global=1)
    tiny.acquire()
    original_smod = smod._global_limiter
    original_shl = shl._limiter
    smod._global_limiter = tiny
    shl._limiter = tiny
    try:
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)
        resp = client.get(path, headers={"Authorization": "Bearer bearer-key"})
        assert resp.status_code not in (401, 403), (
            f"Expected bearer auth pass for {path}, got {resp.status_code}"
        )
    finally:
        smod._global_limiter = original_smod
        shl._limiter = original_shl
        os.environ.pop("AF_API_AUTH_KEY", None)


# ---------------------------------------------------------------------------
# Shared limiter — identity + default cap
# ---------------------------------------------------------------------------


def test_shared_limiter_id_identity():
    """streams._global_limiter IS the same object as get_shared_limiter().

    Both modules must return the same Python object (id() equality) so that
    acquiring on one automatically affects the other.  This is the core
    invariant of the PR2 limiter-fragmentation fix.
    """
    import backend.deps.shared_limiter as shl
    import backend.routers.streams as smod

    # After normal import (no reload), both sides must share the same instance.
    # get_shared_limiter() returns shl._limiter; smod._global_limiter is
    # initialised from get_shared_limiter() at streams.py import time.
    # If they are different objects the fragmentation bug is still present.
    streams_limiter = smod._global_limiter
    shared_limiter = shl.get_shared_limiter()
    assert streams_limiter is shared_limiter, (
        "streams._global_limiter is NOT the shared singleton — limiter fragmentation still present. "
        f"streams id={id(streams_limiter)}, shared id={id(shared_limiter)}"
    )


def test_default_cap_is_40():
    """Default cap is 40 when AF_GLOBAL_STREAM_CAP is unset."""
    os.environ.pop("AF_GLOBAL_STREAM_CAP", None)
    from backend.deps.shared_limiter import DEFAULT_GLOBAL_STREAM_CAP
    assert DEFAULT_GLOBAL_STREAM_CAP == 40, (
        f"Expected default cap 40, got {DEFAULT_GLOBAL_STREAM_CAP}"
    )


def test_env_var_controls_cap(monkeypatch):
    """AF_GLOBAL_STREAM_CAP env var controls the shared limiter cap."""
    import importlib
    import backend.deps.shared_limiter as shl

    monkeypatch.setenv("AF_GLOBAL_STREAM_CAP", "7")
    # Reset module state so the env var is re-read.
    importlib.reload(shl)
    assert shl.get_shared_limiter().max_global == 7, (
        f"Expected cap 7 from env, got {shl.get_shared_limiter().max_global}"
    )


# ---------------------------------------------------------------------------
# Mixed-path cap enforcement
# ---------------------------------------------------------------------------


def test_mixed_path_cap_enforcement(monkeypatch):
    """/feed and /stream/feed both count against the same cap → 503 across paths."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.deps.stream_limiter import GlobalStreamLimiter
    import backend.routers.streams as smod
    import backend.routers.rpc_sse as rpc_sse_mod
    import backend.deps.shared_limiter as shl

    # Create a cap-1 limiter and inject it everywhere.
    tiny_limiter = GlobalStreamLimiter(max_global=1)
    tiny_limiter.acquire()  # exhaust the single slot

    # Patch the shared singleton so all routes see the same cap-1 limiter.
    original_smod = smod._global_limiter
    original_shl = shl._limiter

    smod._global_limiter = tiny_limiter
    shl._limiter = tiny_limiter

    try:
        from fastapi.testclient import TestClient
        import backend.asgi_app as asgi_mod
        client = TestClient(asgi_mod.app, raise_server_exceptions=False)

        # /stream/feed should be capped.
        resp1 = client.get("/stream/feed")
        assert resp1.status_code == 503, (
            f"/stream/feed: expected 503, got {resp1.status_code}"
        )

        # /feed (alias) should also be capped — same limiter.
        resp2 = client.get("/feed")
        assert resp2.status_code == 503, (
            f"/feed: expected 503, got {resp2.status_code}"
        )

        # /events (alias) should also be capped.
        resp3 = client.get("/events")
        assert resp3.status_code == 503, (
            f"/events: expected 503, got {resp3.status_code}"
        )
    finally:
        smod._global_limiter = original_smod
        shl._limiter = original_shl


# ---------------------------------------------------------------------------
# SSE response headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/feed", "/events"])
def test_sse_alias_content_type(path):
    """When authorized and cap not full, /feed and /events emit text/event-stream."""
    import socket
    import threading
    import time

    port = _free_port()
    os.environ.pop("AF_API_AUTH_KEY", None)
    server, _ = _start_server(port)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(base, timeout=10.0)
        # Open the SSE stream briefly and check content-type.
        with httpx.stream("GET", f"{base}{path}", timeout=5.0) as resp:
            assert resp.status_code == 200, (
                f"Expected 200 for {path}, got {resp.status_code}"
            )
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct, (
                f"Expected text/event-stream for {path}, got {ct!r}"
            )
    finally:
        server.should_exit = True
        os.environ.pop("AF_API_AUTH_KEY", None)


@pytest.mark.timeout(15)
def test_feed_emits_connected_frame():
    """GET /feed emits data: {"type":"connected"} as the first SSE frame."""
    port = _free_port()
    os.environ.pop("AF_API_AUTH_KEY", None)
    server, _ = _start_server(port)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(base, timeout=10.0)
        frames = []
        deadline = time.monotonic() + 8.0
        with httpx.stream("GET", f"{base}/feed", timeout=5.0) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if time.monotonic() > deadline:
                    break
                line = line.strip()
                if line.startswith("data: "):
                    import json as _json
                    try:
                        frames.append(_json.loads(line[6:]))
                    except Exception:
                        pass
                    if frames:
                        break
        assert frames, "No SSE frames received from /feed"
        assert frames[0].get("type") == "connected", (
            f"Expected first frame type=connected, got {frames[0]}"
        )
    finally:
        server.should_exit = True
        os.environ.pop("AF_API_AUTH_KEY", None)


@pytest.mark.timeout(15)
def test_events_emits_connected_frame():
    """GET /events emits data: {"type":"connected"} as the first SSE frame."""
    port = _free_port()
    os.environ.pop("AF_API_AUTH_KEY", None)
    server, _ = _start_server(port)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(base, timeout=10.0)
        import json as _json
        frames = []
        deadline = time.monotonic() + 8.0
        with httpx.stream("GET", f"{base}/events", timeout=5.0) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if time.monotonic() > deadline:
                    break
                line = line.strip()
                if line.startswith("data: "):
                    try:
                        frames.append(_json.loads(line[6:]))
                    except Exception:
                        pass
                    if frames:
                        break
        assert frames, "No SSE frames received from /events"
        assert frames[0].get("type") == "connected", (
            f"Expected first frame type=connected, got {frames[0]}"
        )
    finally:
        server.should_exit = True
        os.environ.pop("AF_API_AUTH_KEY", None)
