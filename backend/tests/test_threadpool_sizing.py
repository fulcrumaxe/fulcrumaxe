"""Tests for anyio threadpool sizing in backend/asgi_app.py.

Covers AC7: total_tokens is raised to configurable value defaulting to 256
(capped at 512), set at startup via the lifespan hook.
"""

from __future__ import annotations

import pytest


def test_threadpool_default_256(monkeypatch):
    """Default threadpool size is 256."""
    monkeypatch.delenv("AF_THREADPOOL_TOKENS", raising=False)
    from backend.asgi_app import _threadpool_tokens

    assert _threadpool_tokens() == 256


def test_threadpool_custom_value(monkeypatch):
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "400")
    from backend.asgi_app import _threadpool_tokens

    assert _threadpool_tokens() == 400


def test_threadpool_cap_at_512(monkeypatch):
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "1000")
    from backend.asgi_app import _threadpool_tokens

    assert _threadpool_tokens() == 512


def test_threadpool_minimum_1(monkeypatch):
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "0")
    from backend.asgi_app import _threadpool_tokens

    assert _threadpool_tokens() == 1


def test_threadpool_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "garbage")
    from backend.asgi_app import _threadpool_tokens

    assert _threadpool_tokens() == 256


def test_threadpool_applied_at_startup(monkeypatch):
    """The lifespan hook must actually apply total_tokens on the anyio limiter.

    The hook stores the configured value in app.state.threadpool_tokens so tests
    can verify it without querying the limiter from a sync context.
    """
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "333")
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.asgi_app import app
    from fastapi.testclient import TestClient

    with TestClient(app):
        assert app.state.threadpool_tokens == 333, (
            f"Expected total_tokens=333 but got {app.state.threadpool_tokens}"
        )


def test_threadpool_active_not_pinned_at_cap(monkeypatch):
    """borrowed_tokens must not be >= total_tokens at idle (no threadpool starvation).

    We verify via /health response: if the threadpool were pinned, /health would
    block or time out. A successful 200 response implies borrowed < total at idle.
    """
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "256")
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.asgi_app import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        # If the threadpool is pinned, this sync def route would block indefinitely.
        r = client.get("/health")
    assert r.status_code == 200, "Expected /health to return 200 (threadpool not pinned)"
