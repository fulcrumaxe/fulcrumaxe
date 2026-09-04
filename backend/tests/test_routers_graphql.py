"""Tests for P5f FastAPI router — POST /graphql.

Covers:
- Parity: same status + response shape as legacy for the same query (resolvers mocked).
- Missing/empty "query" key → 400 with exact legacy message "'query' is required".
- Auth: 401 with no token, 403 with wrong token.
- RBAC: allow-all-on-missing-role preserved; explicit deny returns 403.
- /docs and /openapi.json list POST /graphql.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.tests._envelope import assert_body_eq, envelope_error, API_VERSION


# ---------------------------------------------------------------------------
# Rate-limit bypass — prevents token-bucket exhaustion in combined runs
# ---------------------------------------------------------------------------
# The RateLimitMiddleware singleton drains its 60-token bucket across test
# files when they run together (burst=60, >60 requests per combined run).
# Setting AF_RATE_LIMIT_DISABLED=1 tells the middleware to pass everything
# through — this is the same bypass used by the --no-rate-limit CLI flag.

@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    monkeypatch.setenv("AF_RATE_LIMIT_DISABLED", "1")

_AUTH_TOKEN = "test-secret-key"
_WRONG_TOKEN = "wrong-key"


def _make_client(token: str | None = None) -> TestClient:
    from backend.asgi_app import app
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return TestClient(app, headers=headers, raise_server_exceptions=False)


def _authed() -> TestClient:
    return _make_client(token=_AUTH_TOKEN)


def _no_auth() -> TestClient:
    return _make_client(token=None)


def _wrong_auth() -> TestClient:
    return _make_client(token=_WRONG_TOKEN)


# ---------------------------------------------------------------------------
# Missing / empty query → 400 exact legacy message
# ---------------------------------------------------------------------------

class TestMissingQuery:
    def test_no_body_400(self, monkeypatch):
        """Empty body → 400 "'query' is required"."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _authed()
        resp = client.post("/graphql", json={})
        assert resp.status_code == 400
        assert envelope_error(resp) == "'query' is required"

    def test_null_query_400(self, monkeypatch):
        """Explicit null query → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _authed()
        resp = client.post("/graphql", json={"query": None})
        assert resp.status_code == 400
        assert envelope_error(resp) == "'query' is required"

    def test_empty_string_query_400(self, monkeypatch):
        """Empty string query → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _authed()
        resp = client.post("/graphql", json={"query": ""})
        assert resp.status_code == 400
        assert envelope_error(resp) == "'query' is required"


# ---------------------------------------------------------------------------
# Parity — valid query returns execute() result
# ---------------------------------------------------------------------------

class TestParity:
    def test_valid_query_returns_data(self, monkeypatch):
        """Valid query → 200 with data from graphql_api.execute()."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        expected = {"data": {"health": {"ok": True}}}
        with patch("backend.routers.graphql_route._graphql.execute", return_value=expected) as mock_exec:
            client = _authed()
            resp = client.post("/graphql", json={"query": "{ health { ok } }"})
        assert resp.status_code == 200
        assert_body_eq(resp, expected)
        mock_exec.assert_called_once_with("{ health { ok } }")

    def test_graphql_error_response_passes_through(self, monkeypatch):
        """When execute() returns errors, they pass through with 200 status (GraphQL convention)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        error_resp = {"errors": [{"message": "unknown field 'badField'"}]}
        with patch("backend.routers.graphql_route._graphql.execute", return_value=error_resp):
            client = _authed()
            resp = client.post("/graphql", json={"query": "{ badField { x } }"})
        assert resp.status_code == 200
        assert_body_eq(resp, error_resp)

    def test_partial_data_with_errors_passes_through(self, monkeypatch):
        """Partial data + errors both pass through unchanged."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mixed = {"data": {"health": {"ok": True}}, "errors": [{"message": "partial"}]}
        with patch("backend.routers.graphql_route._graphql.execute", return_value=mixed):
            client = _authed()
            resp = client.post("/graphql", json={"query": "{ health { ok unknownField } }"})
        assert resp.status_code == 200
        assert_body_eq(resp, mixed)


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

class TestAuth:
    def test_no_token_401(self, monkeypatch):
        """No auth token → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _no_auth()
        resp = client.post("/graphql", json={"query": "{ health { ok } }"})
        assert resp.status_code == 401

    def test_wrong_token_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _wrong_auth()
        resp = client.post("/graphql", json={"query": "{ health { ok } }"})
        assert resp.status_code == 403

    def test_auth_disabled_allows_request(self, monkeypatch):
        """No AF_API_AUTH_KEY → auth disabled → request allowed."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        expected = {"data": {"health": {"ok": True}}}
        with patch("backend.routers.graphql_route._graphql.execute", return_value=expected):
            client = _no_auth()
            resp = client.post("/graphql", json={"query": "{ health { ok } }"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# RBAC gate
# ---------------------------------------------------------------------------

class TestRBAC:
    def test_rbac_explicit_deny_403(self, monkeypatch):
        """Token with a role that explicitly denies POST /graphql → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        from backend.deps import rbac as rbac_mod
        from unittest.mock import MagicMock

        mock_manager = MagicMock()
        mock_manager.enabled = True
        mock_manager.get_role_for_token.return_value = "readonly"
        mock_manager.check.return_value = False  # deny POST /graphql

        monkeypatch.setattr(rbac_mod, "_rbac_manager", mock_manager)

        client = _authed()
        resp = client.post("/graphql", json={"query": "{ health { ok } }"})
        assert resp.status_code == 403

    def test_rbac_allow_all_on_missing_role(self, monkeypatch):
        """Token not in RBAC key table → legacy allow-all behaviour → 200."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        from backend.deps import rbac as rbac_mod
        from unittest.mock import MagicMock

        mock_manager = MagicMock()
        mock_manager.enabled = True
        mock_manager.get_role_for_token.return_value = None  # token not listed

        monkeypatch.setattr(rbac_mod, "_rbac_manager", mock_manager)

        expected = {"data": {"health": {"ok": True}}}
        with patch("backend.routers.graphql_route._graphql.execute", return_value=expected):
            client = _authed()
            resp = client.post("/graphql", json={"query": "{ health { ok } }"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# OpenAPI / docs registration
# ---------------------------------------------------------------------------

class TestOpenAPI:
    def test_post_graphql_in_openapi(self, monkeypatch):
        """/openapi.json includes POST /graphql."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        from backend.asgi_app import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        paths = schema.get("paths", {})
        assert "/graphql" in paths
        assert "post" in paths["/graphql"]

    def test_docs_page_loads(self, monkeypatch):
        """/docs returns 200."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        from backend.asgi_app import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/docs")
        assert resp.status_code == 200
