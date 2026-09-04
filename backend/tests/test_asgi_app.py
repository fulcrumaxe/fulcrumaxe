"""Tests for backend/asgi_app.py — P1 FastAPI app hub.

Covers:
- AC2: /docs and /openapi.json return 200
- AC4: /health (public route) returns 200 without auth token
- AC7: threadpool total_tokens is raised at startup
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    """TestClient with auth disabled so we can test the public routes directly."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    # Import after env manipulation so _get_auth_key() reads the patched env.
    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def authed_client(monkeypatch):
    """TestClient with auth enabled; all requests include a valid Bearer token."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "test-secret-token")
    from backend.asgi_app import app

    with TestClient(
        app,
        raise_server_exceptions=True,
        headers={"Authorization": "Bearer test-secret-token"},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# AC2: /docs and /openapi.json return 200
# ---------------------------------------------------------------------------


def test_docs_endpoint_returns_200(client):
    r = client.get("/docs")
    assert r.status_code == 200, f"/docs returned {r.status_code}"


def test_openapi_json_returns_200(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200, f"/openapi.json returned {r.status_code}"
    data = r.json()
    assert "openapi" in data
    assert "paths" in data


# ---------------------------------------------------------------------------
# AC4: /health is public (no token needed)
# ---------------------------------------------------------------------------


def test_health_returns_200_without_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_returns_200_with_auth_enabled(authed_client):
    r = authed_client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# AC7: threadpool sizing
# ---------------------------------------------------------------------------


def test_threadpool_tokens_applied_at_startup(monkeypatch):
    """After app startup, anyio default thread limiter == AF_THREADPOOL_TOKENS.

    The lifespan hook stores the configured value in app.state.threadpool_tokens.
    """
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "300")
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.asgi_app import app, _threadpool_tokens

    # The _threadpool_tokens() function must reflect the env var.
    assert _threadpool_tokens() == 300

    # The lifespan hook must store the applied value in app.state.
    with TestClient(app):
        assert hasattr(app.state, "threadpool_tokens"), (
            "lifespan did not set app.state.threadpool_tokens"
        )
        assert app.state.threadpool_tokens == 300, (
            f"Expected 300 but got {app.state.threadpool_tokens}"
        )


def test_threadpool_tokens_default_is_256(monkeypatch):
    """Default threadpool size is 256 when env var is not set."""
    monkeypatch.delenv("AF_THREADPOOL_TOKENS", raising=False)
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.asgi_app import _threadpool_tokens

    assert _threadpool_tokens() == 256


def test_threadpool_tokens_capped_at_512(monkeypatch):
    """Values above 512 are silently capped to 512."""
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "9999")

    from backend.asgi_app import _threadpool_tokens

    assert _threadpool_tokens() == 512


def test_threadpool_tokens_bad_value_falls_back_to_default(monkeypatch):
    """Non-numeric AF_THREADPOOL_TOKENS falls back to 256."""
    monkeypatch.setenv("AF_THREADPOOL_TOKENS", "not-a-number")

    from backend.asgi_app import _threadpool_tokens

    assert _threadpool_tokens() == 256
