"""Tests for backend/routers/traces_get.py — GET /traces and GET /traces/{trace_id}.

Covers:
- Parity: each route returns the same response shape as the legacy api.py handler
- Auth: both routes require authentication (401 without token, 200 with)
- GET /traces: empty response when no spans, paginated with ?limit=N
- GET /traces/{trace_id}: 200 with matched spans; 404 when trace not found
- Integration: routes are served by FastAPI natively (not proxied)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Minimal Span stub — enough for the router logic to exercise
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    trace_id: str
    span_id: str = "span-001"
    start_time_unix_nano: int = 1_000_000_000_000_000_000
    end_time_unix_nano: int = 1_000_001_000_000_000_000
    status: str = "OK"
    name: str = "test-op"
    attributes: dict = field(default_factory=dict)


def _make_spans(*trace_ids: str) -> list[_Span]:
    return [_Span(trace_id=tid) for tid in trace_ids]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_collector(spans: list[_Span]) -> MagicMock:
    """Return a mock collector whose .peek() returns *spans*."""
    collector = MagicMock()
    collector.peek.return_value = spans
    return collector


def _mock_export(spans: Any) -> dict:
    """Minimal stand-in for trace_export.export_spans."""
    return {"resourceSpans": [{"spans": [s.span_id for s in spans]}]}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_no_auth(monkeypatch):
    """TestClient with auth disabled — routes accessible without a token."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def client_with_auth(monkeypatch):
    """TestClient with a valid bearer token."""
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
    """TestClient with auth enabled but no token — expects 401."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "test-secret")
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth parity tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["/traces", "/traces/abc123"])
def test_routes_require_auth_returns_401_without_token(monkeypatch, route):
    """Both GET /traces routes must return 401 when auth is enabled and no token is supplied."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(route)
    assert r.status_code == 401, f"{route} returned {r.status_code}, expected 401"


@pytest.mark.parametrize("route", ["/traces", "/traces/abc123"])
def test_routes_return_200_with_valid_token(monkeypatch, route):
    """Both routes return 200 (not 401/403) when a valid bearer token is provided."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
    from backend.asgi_app import app
    spans = _make_spans("abc123")
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector(spans)),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        with TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer secret"},
        ) as c:
            r = c.get(route)
    assert r.status_code == 200, f"{route} returned {r.status_code}, expected 200"


# ---------------------------------------------------------------------------
# GET /traces parity tests
# ---------------------------------------------------------------------------


def test_list_traces_empty_when_no_spans(client_no_auth, monkeypatch):
    """GET /traces returns {traces: [], count: 0} when the collector has no spans."""
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector([])),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        r = client_no_auth.get("/traces")
    assert r.status_code == 200
    body = r.json()
    assert body["traces"] == []
    assert body["count"] == 0


def test_list_traces_groups_by_trace_id(client_no_auth, monkeypatch):
    """GET /traces groups spans by trace_id and returns one entry per trace."""
    spans = _make_spans("t1", "t1", "t2")  # 2 spans for t1, 1 for t2
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector(spans)),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        r = client_no_auth.get("/traces")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    trace_ids = {t["trace_id"] for t in body["traces"]}
    assert trace_ids == {"t1", "t2"}


def test_list_traces_span_count_field(client_no_auth, monkeypatch):
    """Each trace entry has a correct span_count field."""
    spans = _make_spans("t1", "t1", "t1")
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector(spans)),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        r = client_no_auth.get("/traces")
    body = r.json()
    assert len(body["traces"]) == 1
    assert body["traces"][0]["span_count"] == 3


def test_list_traces_has_resource_spans_field(client_no_auth, monkeypatch):
    """Each trace entry exposes a resourceSpans field from export_spans."""
    spans = _make_spans("t1")
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector(spans)),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        r = client_no_auth.get("/traces")
    body = r.json()
    assert "resourceSpans" in body["traces"][0]


def test_list_traces_limit_query_param(client_no_auth, monkeypatch):
    """?limit=1 restricts the response to at most 1 trace."""
    spans = _make_spans("t1", "t2", "t3")
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector(spans)),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        r = client_no_auth.get("/traces?limit=1")
    body = r.json()
    assert body["count"] == 1
    assert len(body["traces"]) == 1


def test_list_traces_default_limit_is_50(client_no_auth, monkeypatch):
    """Default limit is 50 — collector is peeked at limit*20 to allow grouping."""
    collector = _mock_collector([])
    with (
        patch("backend.routers.traces_get.get_collector", return_value=collector),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        client_no_auth.get("/traces")
    # Default limit=50, so peek is called with 50*20=1000
    collector.peek.assert_called_once_with(1000)


# ---------------------------------------------------------------------------
# GET /traces/{trace_id} parity tests
# ---------------------------------------------------------------------------


def test_get_trace_returns_200_when_found(client_no_auth, monkeypatch):
    """GET /traces/{trace_id} returns 200 with matched spans."""
    spans = _make_spans("abc", "abc", "xyz")
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector(spans)),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        r = client_no_auth.get("/traces/abc")
    assert r.status_code == 200
    body = r.json()
    assert body["trace_id"] == "abc"
    assert body["span_count"] == 2
    assert "resourceSpans" in body


def test_get_trace_returns_404_when_not_found(client_no_auth, monkeypatch):
    """GET /traces/{trace_id} returns 404 when the trace_id is not in the collector."""
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector([])),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        r = client_no_auth.get("/traces/no-such-trace")
    assert r.status_code == 404
    body = r.json()
    # LegacyEnvelopeMiddleware rewrites {"detail": "..."} to {"error": "..."}.
    error_msg = body.get("error") or body.get("detail") or ""
    assert "not found" in error_msg.lower()


def test_get_trace_only_returns_matching_spans(client_no_auth, monkeypatch):
    """GET /traces/{trace_id} filters to only the requested trace's spans."""
    spans = _make_spans("t1", "t2", "t1")
    with (
        patch("backend.routers.traces_get.get_collector", return_value=_mock_collector(spans)),
        patch("backend.routers.traces_get._export_spans", side_effect=_mock_export),
    ):
        r = client_no_auth.get("/traces/t1")
    body = r.json()
    assert body["trace_id"] == "t1"
    assert body["span_count"] == 2  # only the 2 t1 spans


# ---------------------------------------------------------------------------
# Native route assertion — routes must NOT be proxied
# ---------------------------------------------------------------------------


def test_traces_routes_are_registered_natively():
    """GET /traces and GET /traces/{trace_id} must be registered on the FastAPI app.

    Fails if these routes are missing from app.routes (would mean they fall through
    to the catch-all proxy instead of being served natively).
    """
    from backend.asgi_app import app
    from fastapi.routing import APIRoute

    native_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
    }
    assert "/traces" in native_paths, (
        "GET /traces is not registered natively — it will be proxied instead of served "
        "by backend/routers/traces_get.py"
    )
    assert "/traces/{trace_id}" in native_paths, (
        "GET /traces/{trace_id} is not registered natively"
    )


def test_traces_stats_not_duplicated():
    """/traces/stats must NOT be duplicated in traces_get.py — it lives in stats.py."""
    from backend.asgi_app import app
    from fastapi.routing import APIRoute

    traces_stats_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/traces/stats"
    ]
    # Exactly one: the one in stats.py
    assert len(traces_stats_routes) == 1, (
        f"Expected exactly 1 /traces/stats route (from stats.py), found {len(traces_stats_routes)}. "
        "traces_get.py must NOT register /traces/stats."
    )
