"""Tests for the global SSE/WS stream limiter in backend/asgi_app.py.

Covers AC8: GlobalStreamLimiter — reaching the global cap returns 503 on the
next stream open. Unit-tested with the stub /stub/stream endpoint.
"""

from __future__ import annotations

import threading

import pytest


# ---------------------------------------------------------------------------
# Unit tests for GlobalStreamLimiter
# ---------------------------------------------------------------------------


def test_global_stream_limiter_allows_up_to_cap():
    from backend.deps.stream_limiter import GlobalStreamLimiter

    limiter = GlobalStreamLimiter(max_global=3)
    assert limiter.acquire() is True
    assert limiter.acquire() is True
    assert limiter.acquire() is True
    # Should be at cap now.
    assert limiter.acquire() is False


def test_global_stream_limiter_allows_after_release():
    from backend.deps.stream_limiter import GlobalStreamLimiter

    limiter = GlobalStreamLimiter(max_global=1)
    assert limiter.acquire() is True
    assert limiter.acquire() is False
    limiter.release()
    assert limiter.acquire() is True


def test_global_stream_limiter_active_count():
    from backend.deps.stream_limiter import GlobalStreamLimiter

    limiter = GlobalStreamLimiter(max_global=10)
    assert limiter.active == 0
    limiter.acquire()
    limiter.acquire()
    assert limiter.active == 2
    limiter.release()
    assert limiter.active == 1


def test_global_stream_limiter_release_below_zero_is_safe():
    from backend.deps.stream_limiter import GlobalStreamLimiter

    limiter = GlobalStreamLimiter(max_global=5)
    # Should not go below 0.
    limiter.release()
    assert limiter.active == 0


def test_global_stream_limiter_thread_safe():
    """Concurrent acquire/release calls should not corrupt the counter."""
    from backend.deps.stream_limiter import GlobalStreamLimiter

    cap = 50
    limiter = GlobalStreamLimiter(max_global=cap)
    errors: list[str] = []

    def worker():
        for _ in range(20):
            if limiter.acquire():
                limiter.release()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if limiter.active != 0:
        errors.append(f"active count not 0 after all releases: {limiter.active}")
    assert not errors, errors


# ---------------------------------------------------------------------------
# Integration: stub /stub/stream endpoint returns 503 when cap is full
# ---------------------------------------------------------------------------


def test_stub_stream_503_when_cap_full(monkeypatch):
    """When the global stream limiter is exhausted, /stub/stream returns 503."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.deps.stream_limiter import GlobalStreamLimiter

    # Artificially fill the cap by replacing the module-level limiter.
    full_limiter = GlobalStreamLimiter(max_global=1)
    full_limiter.acquire()  # exhaust it

    import backend.asgi_app as asgi_mod

    original = asgi_mod.stream_limiter
    asgi_mod.stream_limiter = full_limiter
    try:
        from fastapi.testclient import TestClient

        with TestClient(asgi_mod.app, raise_server_exceptions=True) as client:
            r = client.get("/stub/stream")
        assert r.status_code == 503, (
            f"Expected 503 when cap is full, got {r.status_code}"
        )
    finally:
        asgi_mod.stream_limiter = original


def test_stub_stream_200_when_cap_not_full(monkeypatch):
    """When below the cap, /stub/stream acquires a slot and accepts the connection.

    We verify this by checking that acquire() returns True before a request,
    meaning the limiter has capacity; and that the 503 path is only taken
    when the cap is exhausted (covered by the previous test).
    """
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.deps.stream_limiter import GlobalStreamLimiter

    # With a fresh limiter, acquire() should succeed.
    fresh_limiter = GlobalStreamLimiter(max_global=10)
    assert fresh_limiter.acquire() is True, "Expected acquire() to succeed below cap"
    fresh_limiter.release()

    # The 503 path is tested in test_stub_stream_503_when_cap_full.
    # For the 200 path, we verify the response status + content-type using
    # a live server via real HTTP (avoids TestClient streaming hang).
    import socket
    import threading
    import time

    import uvicorn

    import backend.asgi_app as asgi_mod

    original = asgi_mod.stream_limiter
    asgi_mod.stream_limiter = GlobalStreamLimiter(max_global=10)

    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

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

        # Wait for ready.
        import httpx
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{base}/health", timeout=1.0)
                break
            except Exception:
                time.sleep(0.05)

        # Stream the first few bytes and check headers.
        with httpx.stream("GET", f"{base}/stub/stream", timeout=5.0) as r:
            assert r.status_code == 200, f"Expected 200, got {r.status_code}"
            ct = r.headers.get("content-type", "")
            assert "text/event-stream" in ct, f"Expected SSE content-type, got {ct}"
            # Read just the first chunk; then close.
            for chunk in r.iter_bytes():
                assert b"keepalive" in chunk
                break

    finally:
        server.should_exit = True
        t.join(timeout=3)
        asgi_mod.stream_limiter = original
