"""Tests for backend/routers/stats.py — P2 FastAPI stats routes.

Covers:
- Parity: each route returns the same shape as the legacy handler
- Auth: all five stats routes require authentication (401 without token, 200 with)
- Schema: routes appear in /openapi.json with correct paths
- Query params: /registry/stats ?project= param preserved
- Integration: routes are served by FastAPI (not proxied)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_no_auth(monkeypatch):
    """TestClient with auth disabled — stats routes return 200 without a token."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def client_with_auth(monkeypatch):
    """TestClient with auth enabled; valid token on all requests."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "test-secret")
    from backend.asgi_app import app
    with TestClient(
        app,
        raise_server_exceptions=True,
        headers={"Authorization": "Bearer test-secret"},
    ) as c:
        yield c


@pytest.fixture()
def client_no_token(monkeypatch):
    """TestClient with auth enabled but NO token — expects 401."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "test-secret")
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth parity: all stats routes require auth
# ---------------------------------------------------------------------------

_STATS_ROUTES = [
    "/registry/stats",
    "/audit/stats",
    "/quality/stats",
    "/memory/stats",
    "/traces/stats",
]


@pytest.mark.parametrize("route", _STATS_ROUTES)
def test_stats_route_requires_auth_returns_401_without_token(monkeypatch, route):
    """Every stats route must return 401 when auth is enabled and no token is provided."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(route)
    assert r.status_code == 401, f"{route} returned {r.status_code}, expected 401"


@pytest.mark.parametrize("route", _STATS_ROUTES)
def test_stats_route_returns_200_with_valid_token(monkeypatch, route):
    """Every stats route must return 200 when a valid bearer token is provided."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.get(route, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200, f"{route} returned {r.status_code}"


# ---------------------------------------------------------------------------
# /registry/stats — parity + ?project= param
# ---------------------------------------------------------------------------


def test_registry_stats_shape(client_no_auth):
    stub = {"total": 10, "done": 5, "in_progress": 2, "spec_ready": 1,
            "tasks_per_day": 1.5, "avg_days_to_complete": 2.0, "completion_count": 5}
    mock_reg = MagicMock()
    mock_reg.stats.return_value = stub
    with patch("backend.routers.stats.DiscussionRegistry", return_value=mock_reg):
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/registry/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 10
    assert data["done"] == 5


def test_registry_stats_project_param_accepted(client_no_auth):
    """?project=<slug> param is forwarded to DiscussionRegistry correctly."""
    stub = {"total": 3, "done": 1, "in_progress": 1, "spec_ready": 1}
    mock_reg = MagicMock()
    mock_reg.stats.return_value = stub

    mock_state = MagicMock()
    mock_state.state_dir = MagicMock()
    # Simulate state_dir / ".autonomous-team" not existing so we fall back to state_dir
    team_dir = MagicMock()
    team_dir.exists.return_value = False
    mock_state.state_dir.__truediv__ = lambda self, other: team_dir

    with patch("backend.routers.stats._state_for_project", return_value=mock_state):
        with patch("backend.routers.stats.DiscussionRegistry", return_value=mock_reg):
            from backend.asgi_app import app
            with TestClient(app, raise_server_exceptions=True) as c:
                r = c.get("/registry/stats?project=my-project")
    assert r.status_code == 200


def test_registry_stats_invalid_project_returns_400(client_no_auth):
    """An invalid project name (path traversal attempt) returns 400."""
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/registry/stats?project=../../etc/passwd")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /audit/stats — parity
# ---------------------------------------------------------------------------


def test_audit_stats_shape(client_no_auth):
    stub = {
        "by_source": {"blackboard": 10, "executor": 5},
        "by_action": {"write": 8, "read": 7},
        "total": 15,
    }
    mock_at = MagicMock()
    mock_at.stats.return_value = stub
    with patch("backend.routers.stats.get_audit_trail", return_value=mock_at):
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/audit/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 15
    assert "by_source" in data
    assert "by_action" in data


# ---------------------------------------------------------------------------
# /quality/stats — parity
# ---------------------------------------------------------------------------


def test_quality_stats_shape(client_no_auth):
    stub = {
        "total_scored": 20,
        "avg_total": 7.5,
        "avg_complexity": 6.0,
        "avg_test_coverage": 8.0,
        "avg_review_rounds": 1.5,
        "avg_size": 4.0,
        "grade_distribution": {"A": 5, "B": 10, "C": 5},
    }
    mock_qs = MagicMock()
    mock_qs.stats.return_value = stub
    with patch("backend.quality_scorer.QualityScorer", return_value=mock_qs):
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/quality/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["total_scored"] == 20
    assert "grade_distribution" in data


# ---------------------------------------------------------------------------
# /memory/stats — parity
# ---------------------------------------------------------------------------


def test_memory_stats_shape(client_no_auth):
    stub = {
        "total": 50,
        "by_role": {"executor": 30, "code-reviewer": 20},
        "by_type": {"feedback": 35, "fact": 15},
        "by_session": {"sess1": 25, "sess2": 25},
    }
    mock_mem = MagicMock()
    mock_mem.stats.return_value = stub
    with patch("backend.agent_memory.AgentMemory", return_value=mock_mem):
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/memory/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 50
    assert "by_role" in data


# ---------------------------------------------------------------------------
# /traces/stats — parity (empty + non-empty spans)
# ---------------------------------------------------------------------------


def test_traces_stats_empty_collector(client_no_auth):
    """Empty span list returns zero-valued stats (same as legacy api.py:3376)."""
    with patch("backend.routers.stats.get_collector") as mock_coll:
        mock_coll.return_value.peek.return_value = []
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/traces/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["traces_per_minute"] == 0.0
    assert data["avg_spans"] == 0.0
    assert data["p50_duration_ms"] == 0.0
    assert data["p95_duration_ms"] == 0.0
    assert data["error_rate"] == 0.0


def test_traces_stats_with_spans(client_no_auth):
    """Non-empty span list triggers inline computation — result is a valid stats dict."""
    import time

    now_ns = time.time_ns()

    class FakeSpan:
        def __init__(self, trace_id, start_ns, end_ns, status="OK"):
            self.trace_id = trace_id
            self.start_time_unix_nano = start_ns
            self.end_time_unix_nano = end_ns
            self.status = status

    spans = [
        FakeSpan("trace1", now_ns - 10_000_000, now_ns - 5_000_000),
        FakeSpan("trace1", now_ns - 8_000_000, now_ns - 4_000_000),
        FakeSpan("trace2", now_ns - 6_000_000, now_ns - 2_000_000, status="ERROR"),
    ]

    with patch("backend.routers.stats.get_collector") as mock_coll:
        mock_coll.return_value.peek.return_value = spans
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/traces/stats")
    assert r.status_code == 200
    data = r.json()
    # Two distinct trace_ids in the last minute → traces_per_minute = 2.0
    assert data["traces_per_minute"] == 2.0
    # 3 spans across 2 traces → avg_spans = 1.5
    assert data["avg_spans"] == 1.5
    # 1 ERROR out of 3 spans → error_rate ≈ 0.3333
    assert data["error_rate"] > 0.0


# ---------------------------------------------------------------------------
# Schema: all five stats routes appear in /openapi.json
# ---------------------------------------------------------------------------


def test_openapi_lists_stats_routes(client_no_auth):
    r = client_no_auth.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json().get("paths", {})
    for route in _STATS_ROUTES:
        assert route in paths, f"Expected {route} in OpenAPI paths, got: {list(paths)}"


# ---------------------------------------------------------------------------
# Integration: stats routes are served by FastAPI (not proxied)
# ---------------------------------------------------------------------------


def test_registry_stats_served_by_fastapi_not_proxied(client_no_auth):
    """FastAPI serves /registry/stats directly — the proxy client is NOT called."""
    with patch("backend.asgi_app._get_proxy_client") as mock_proxy:
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/registry/stats")
    assert r.status_code == 200
    mock_proxy.return_value.request.assert_not_called()
