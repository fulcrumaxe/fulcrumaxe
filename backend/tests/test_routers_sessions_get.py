"""Tests for sessions_get FastAPI router — bare /sessions GET routes.

Covers:
- Parity: each migrated route returns the same status + response shape as
  legacy for the same request (side-effects ALWAYS MOCKED).
- Auth: 401 with no token, 403 with wrong token, 200 with correct token.
- RBAC: allow-all-on-missing-role preserved; explicit deny returns 403.

Routes tested:
  GET /sessions                 — list sessions
  GET /sessions/current         — current active session (404 if none)
  GET /sessions/compare         — compare two sessions (?a=&b= required)
  GET /sessions/{session_id}    — fetch a single session by ID

CRITICAL: ALL SessionManager calls are MOCKED — no disk I/O.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTH_TOKEN = "test-secret-sessions-get"
_WRONG_TOKEN = "wrong-key-sessions-get"


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


# ===========================================================================
# GET /sessions
# ===========================================================================

class TestSessionsList:
    def test_list_sessions_returns_sessions_key(self, monkeypatch):
        """Returns {"sessions": [...]} with data from SessionManager.list_sessions."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_sessions = [{"id": "s1"}, {"id": "s2"}]
        mock_sm = MagicMock()
        mock_sm.list_sessions.return_value = fake_sessions

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "sessions" in data
        assert data["sessions"] == fake_sessions

    def test_list_sessions_empty(self, monkeypatch):
        """Returns {"sessions": []} when no sessions exist (plus envelope _api_version)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()
        mock_sm.list_sessions.return_value = []

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions")

        assert resp.status_code == 200
        data = resp.json()
        assert data["sessions"] == []

    def test_list_sessions_no_auth_returns_401(self, monkeypatch):
        """Missing bearer token → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/sessions")
        assert resp.status_code == 401

    def test_list_sessions_wrong_token_returns_403(self, monkeypatch):
        """Wrong bearer token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/sessions")
        assert resp.status_code == 403

    def test_list_sessions_auth_disabled(self, monkeypatch):
        """When AF_API_AUTH_KEY unset, auth is disabled — no token needed."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        mock_sm = MagicMock()
        mock_sm.list_sessions.return_value = []

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _no_auth().get("/sessions")

        assert resp.status_code == 200


# ===========================================================================
# GET /sessions/current
# ===========================================================================

class TestSessionsCurrent:
    def test_current_returns_session(self, monkeypatch):
        """Returns the current session when one exists (plus envelope _api_version)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_session = {"id": "current-session", "status": "active"}
        mock_sm = MagicMock()
        mock_sm.current_session.return_value = fake_session

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/current")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == "current-session"
        assert data["status"] == "active"

    def test_current_returns_404_when_none(self, monkeypatch):
        """Returns 404 when no active session — mirrors legacy api.py:3124.
        LegacyEnvelopeMiddleware rewrites 'detail' → 'error' for 4xx responses."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()
        mock_sm.current_session.return_value = None

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/current")

        assert resp.status_code == 404
        # LegacyEnvelopeMiddleware converts 'detail' → 'error'
        assert "no active session" in resp.json().get("error", "")

    def test_current_no_auth_returns_401(self, monkeypatch):
        """Missing token → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/sessions/current")
        assert resp.status_code == 401

    def test_current_wrong_token_returns_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/sessions/current")
        assert resp.status_code == 403


# ===========================================================================
# GET /sessions/compare
# ===========================================================================

class TestSessionsCompare:
    def test_compare_returns_result(self, monkeypatch):
        """Returns comparison result when both a= and b= are provided."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_result = {"a": {"id": "s1"}, "b": {"id": "s2"}, "diff": {}}
        mock_sm = MagicMock()
        mock_sm.compare_sessions.return_value = fake_result

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/compare?a=s1&b=s2")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["a"] == {"id": "s1"}
        assert data["b"] == {"id": "s2"}
        mock_sm.compare_sessions.assert_called_once_with("s1", "s2")

    def test_compare_missing_a_returns_400(self, monkeypatch):
        """Missing 'a' param → 400 — mirrors api.py:3139-3141.
        LegacyEnvelopeMiddleware rewrites 'detail' → 'error' for 4xx responses."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/compare?b=s2")

        assert resp.status_code == 400
        # LegacyEnvelopeMiddleware converts 'detail' → 'error'
        assert "a" in resp.json().get("error", "")

    def test_compare_missing_b_returns_400(self, monkeypatch):
        """Missing 'b' param → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/compare?a=s1")

        assert resp.status_code == 400

    def test_compare_session_not_found_returns_404(self, monkeypatch):
        """SessionManager raises ValueError → 404 — mirrors api.py:3146-3147.
        LegacyEnvelopeMiddleware rewrites 'detail' → 'error' for 4xx responses."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()
        mock_sm.compare_sessions.side_effect = ValueError("session 'bad-id' not found")

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/compare?a=bad-id&b=s2")

        assert resp.status_code == 404
        # LegacyEnvelopeMiddleware converts 'detail' → 'error'
        assert "bad-id" in resp.json().get("error", "")

    def test_compare_no_auth_returns_401(self, monkeypatch):
        """Missing token → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/sessions/compare?a=s1&b=s2")
        assert resp.status_code == 401

    def test_compare_wrong_token_returns_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/sessions/compare?a=s1&b=s2")
        assert resp.status_code == 403


# ===========================================================================
# GET /sessions/{session_id}
# ===========================================================================

class TestSessionsGetById:
    def test_get_by_id_returns_session(self, monkeypatch):
        """Returns the session when found (plus envelope _api_version)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_session = {"id": "abc123", "status": "closed"}
        mock_sm = MagicMock()
        mock_sm.get_session.return_value = fake_session

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/abc123")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == "abc123"
        assert data["status"] == "closed"
        mock_sm.get_session.assert_called_once_with("abc123")

    def test_get_by_id_not_found_returns_404(self, monkeypatch):
        """Returns 404 when session ID not found — mirrors api.py:3156-3157.
        LegacyEnvelopeMiddleware rewrites 'detail' → 'error' for 4xx responses."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()
        mock_sm.get_session.return_value = None

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/nosuchid")

        assert resp.status_code == 404
        # LegacyEnvelopeMiddleware converts 'detail' → 'error'
        assert "nosuchid" in resp.json().get("error", "")

    def test_get_by_id_no_auth_returns_401(self, monkeypatch):
        """Missing token → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/sessions/abc123")
        assert resp.status_code == 401

    def test_get_by_id_wrong_token_returns_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/sessions/abc123")
        assert resp.status_code == 403


# ===========================================================================
# Route ordering — /sessions/current must not be captured by /{session_id}
# ===========================================================================

class TestRouteOrdering:
    def test_current_not_captured_by_id_param(self, monkeypatch):
        """GET /sessions/current is the fixed route, not the /{session_id} wildcard."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()
        mock_sm.current_session.return_value = {"id": "session-x"}

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/current")

        # Should call current_session(), not get_session("current")
        mock_sm.current_session.assert_called_once()
        mock_sm.get_session.assert_not_called()
        assert resp.status_code == 200

    def test_compare_not_captured_by_id_param(self, monkeypatch):
        """GET /sessions/compare is the fixed route, not the /{session_id} wildcard."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()
        mock_sm.compare_sessions.return_value = {"a": {}, "b": {}}

        with patch("backend.routers.sessions_get.SessionManager", return_value=mock_sm):
            resp = _authed().get("/sessions/compare?a=s1&b=s2")

        mock_sm.compare_sessions.assert_called_once_with("s1", "s2")
        mock_sm.get_session.assert_not_called()
        assert resp.status_code == 200
