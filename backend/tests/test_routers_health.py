"""Tests for backend/routers/health.py — P2 FastAPI health routes.

Covers:
- Parity: each route returns the same shape as the legacy handler
- Auth: all three routes are PUBLIC (no token needed)
- Schema: routes appear in /openapi.json with correct paths
- Integration: routes are served by FastAPI (not proxied)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    """TestClient with auth disabled."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def authed_client(monkeypatch):
    """TestClient with auth enabled; valid token on all requests."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "test-token")
    from backend.asgi_app import app
    with TestClient(
        app,
        raise_server_exceptions=True,
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# /health — parity + auth
# ---------------------------------------------------------------------------


def test_health_returns_200_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    # Legacy adds loop_metrics fields — ensure they're present
    assert "loop_last_run" in data or "malformed_lines" in data


def test_health_is_public_when_auth_enabled(monkeypatch):
    """GET /health must return 200 even when AF_API_AUTH_KEY is set and no token is sent."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "some-key")
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/health")
    assert r.status_code == 200, f"Expected 200 but got {r.status_code}"


def test_health_includes_loop_metrics(client):
    """GET /health payload must include loop_metrics fields (the real /health, not the stub)."""
    stub_metrics = {
        "loop_last_run": "2026-01-01T00:00:00Z",
        "loop_duration_s": 42,
        "loop_idle_rate": 0.0,
        "malformed_lines": 0,
    }
    with patch("backend.routers.health.get_loop_metrics", return_value=stub_metrics):
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["loop_last_run"] == "2026-01-01T00:00:00Z"
    assert data["loop_duration_s"] == 42
    assert data["loop_idle_rate"] == 0.0


# ---------------------------------------------------------------------------
# /health/loop — parity + auth
# ---------------------------------------------------------------------------


def test_health_loop_returns_200_no_auth(client):
    r = client.get("/health/loop")
    assert r.status_code == 200
    data = r.json()
    # Legacy returns lastRun, status, duration
    assert "status" in data
    assert "duration" in data


def test_health_loop_is_public_when_auth_enabled(monkeypatch):
    monkeypatch.setenv("AF_API_AUTH_KEY", "some-key")
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/health/loop")
    assert r.status_code == 200, f"Expected 200 but got {r.status_code}"


def test_health_loop_shape(client):
    stub_result = {"lastRun": "2026-01-01T00:00:00Z", "status": "ok", "duration": 30}
    with patch("backend.routers.health.get_loop_health_dashboard", return_value=stub_result):
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/health/loop")
    assert r.status_code == 200
    data = r.json()
    assert data["lastRun"] == "2026-01-01T00:00:00Z"
    assert data["status"] == "ok"
    assert data["duration"] == 30


# ---------------------------------------------------------------------------
# /health/modules — parity + auth
# ---------------------------------------------------------------------------


def test_health_modules_returns_200_no_auth(client):
    r = client.get("/health/modules")
    assert r.status_code == 200
    # Response is a dict (module health map)
    assert isinstance(r.json(), dict)


def test_health_modules_is_public_when_auth_enabled(monkeypatch):
    monkeypatch.setenv("AF_API_AUTH_KEY", "some-key")
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/health/modules")
    assert r.status_code == 200, f"Expected 200 but got {r.status_code}"


def test_health_modules_calls_module_health(client):
    stub_result = {"backend.health_monitor": {"ok": True, "import_time_ms": 1.2}}
    with patch("backend.routers.health._module_health.get_cached_module_health", return_value=stub_result):
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/health/modules")
    assert r.status_code == 200
    data = r.json()
    assert "backend.health_monitor" in data


# ---------------------------------------------------------------------------
# Schema: all three routes appear in /openapi.json
# ---------------------------------------------------------------------------


def test_openapi_lists_health_routes(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json().get("paths", {})
    assert "/health" in paths, f"Expected /health in OpenAPI paths, got: {list(paths)}"
    assert "/health/loop" in paths, f"Expected /health/loop in OpenAPI paths"
    assert "/health/modules" in paths, f"Expected /health/modules in OpenAPI paths"


# ---------------------------------------------------------------------------
# Integration: routes are served by FastAPI (response comes from router, not proxy)
# ---------------------------------------------------------------------------


def test_health_served_by_fastapi_not_proxied(client):
    """The /health route is served directly by FastAPI — no proxy call needed."""
    # If the route were proxied, it would try to connect to 127.0.0.1:18099.
    # With httpx's TestClient (no real network), a proxy call would raise ConnectionError.
    # The fact that we get 200 confirms FastAPI is serving it directly.
    with patch("backend.asgi_app._get_proxy_client") as mock_proxy:
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/health")
    assert r.status_code == 200
    # The proxy client should NOT have been used for this request
    mock_proxy.return_value.request.assert_not_called()
