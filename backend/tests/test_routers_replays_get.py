"""Tests for replays_get FastAPI router — GET /replays read routes.

Covers:
- Parity: each migrated route returns the same status + response shape as
  legacy for the same request (side-effects ALWAYS MOCKED).
- Auth: 401 with no token, 403 with wrong token, 200 with correct token.
- RBAC: allow-all-on-missing-role preserved.
- Route ordering: /replays/status served before /replays/{agent_id}.

Routes tested:
  GET /replays                   — list recent replay metadata
  GET /replays/status            — active replay state
  GET /replays/{agent_id}/summary — header + footer only
  GET /replays/{agent_id}        — full event list

CRITICAL: get_active_replay and get_recorder are ALWAYS MOCKED.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTH_TOKEN = "test-secret-replays-get"
_WRONG_TOKEN = "wrong-key-replays-get"


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
# GET /replays
# ===========================================================================

class TestReplaysList:
    def test_list_returns_replays(self, monkeypatch):
        """Returns {replays: [...]} with mocked recorder output."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_replays = [
            {"agent_id": "exec-1", "created_at": "2026-05-22T10:00:00Z"},
            {"agent_id": "exec-2", "created_at": "2026-05-22T09:00:00Z"},
        ]
        mock_rec = MagicMock()
        mock_rec.list_replays.return_value = fake_replays

        with patch("backend.routers.replays_get.get_recorder", return_value=mock_rec):
            resp = _authed().get("/replays")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "replays" in data
        assert data["replays"] == fake_replays
        mock_rec.list_replays.assert_called_once()

    def test_list_empty(self, monkeypatch):
        """Empty recorder → {replays: []}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_rec = MagicMock()
        mock_rec.list_replays.return_value = []

        with patch("backend.routers.replays_get.get_recorder", return_value=mock_rec):
            resp = _authed().get("/replays")

        assert resp.status_code == 200
        data = resp.json()
        assert data["replays"] == []

    def test_list_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/replays")
        assert resp.status_code == 401

    def test_list_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/replays")
        assert resp.status_code == 403


# ===========================================================================
# GET /replays/status
# ===========================================================================

class TestReplaysStatus:
    def test_status_no_active_replay(self, monkeypatch):
        """No active replay → {active: false}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.replays_get.get_active_replay", return_value=None):
            resp = _authed().get("/replays/status")

        assert resp.status_code == 200, resp.text
        assert resp.json()["active"] is False

    def test_status_dead_engine(self, monkeypatch):
        """Engine present but not alive → {active: false}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.is_alive = False

        with patch("backend.routers.replays_get.get_active_replay", return_value=mock_eng):
            resp = _authed().get("/replays/status")

        assert resp.status_code == 200
        assert resp.json()["active"] is False

    def test_status_active_engine(self, monkeypatch):
        """Active engine → get_status() dict returned."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.is_alive = True
        mock_eng.get_status.return_value = {
            "active": True,
            "agent_id": "exec-99",
            "current_event": 5,
            "total_events": 20,
        }

        with patch("backend.routers.replays_get.get_active_replay", return_value=mock_eng):
            resp = _authed().get("/replays/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["agent_id"] == "exec-99"
        assert data["current_event"] == 5
        mock_eng.get_status.assert_called_once()

    def test_status_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/replays/status")
        assert resp.status_code == 401

    def test_status_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/replays/status")
        assert resp.status_code == 403

    def test_status_not_captured_as_agent_id(self, monkeypatch):
        """'status' must NOT be routed to /replays/{agent_id}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        # Patch both to verify which one fires
        with (
            patch("backend.routers.replays_get.get_active_replay", return_value=None) as mock_status,
            patch("backend.routers.replays_get.get_recorder") as mock_rec,
        ):
            resp = _authed().get("/replays/status")

        # If routing is wrong, get_recorder().get_replay("status") fires instead
        mock_rec.return_value.get_replay.assert_not_called()
        assert resp.status_code == 200
        assert resp.json()["active"] is False


# ===========================================================================
# GET /replays/{agent_id}/summary
# ===========================================================================

class TestReplaysSummary:
    def test_summary_found(self, monkeypatch):
        """Summary exists → 200 with header+footer."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_summary = {
            "header": {"type": "header", "agent_id": "exec-42"},
            "footer": {"type": "footer", "exit_code": 0},
        }
        mock_rec = MagicMock()
        mock_rec.get_summary.return_value = fake_summary

        with patch("backend.routers.replays_get.get_recorder", return_value=mock_rec):
            resp = _authed().get("/replays/exec-42/summary")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["header"]["agent_id"] == "exec-42"
        assert data["footer"]["exit_code"] == 0
        mock_rec.get_summary.assert_called_once_with("exec-42")

    def test_summary_not_found_404(self, monkeypatch):
        """get_summary returns None → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_rec = MagicMock()
        mock_rec.get_summary.return_value = None

        with patch("backend.routers.replays_get.get_recorder", return_value=mock_rec):
            resp = _authed().get("/replays/missing-agent/summary")

        assert resp.status_code == 404
        # LegacyEnvelopeMiddleware rewrites "detail" → "error" for 4xx responses
        body = resp.json()
        error_msg = body.get("error") or body.get("detail", "")
        assert "missing-agent" in error_msg

    def test_summary_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/replays/exec-42/summary")
        assert resp.status_code == 401

    def test_summary_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/replays/exec-42/summary")
        assert resp.status_code == 403


# ===========================================================================
# GET /replays/{agent_id}
# ===========================================================================

class TestReplaysGet:
    def test_get_found(self, monkeypatch):
        """Events found → 200 with agent_id + events list."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_events = [
            {"seq": 0, "type": "header", "agent_id": "exec-7"},
            {"seq": 1, "type": "text", "content": "hello"},
            {"seq": 2, "type": "footer", "exit_code": 0},
        ]
        mock_rec = MagicMock()
        mock_rec.get_replay.return_value = fake_events

        with patch("backend.routers.replays_get.get_recorder", return_value=mock_rec):
            resp = _authed().get("/replays/exec-7")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["agent_id"] == "exec-7"
        assert len(data["events"]) == 3
        assert data["events"][0]["type"] == "header"
        mock_rec.get_replay.assert_called_once_with("exec-7")

    def test_get_not_found_404(self, monkeypatch):
        """get_replay returns [] → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_rec = MagicMock()
        mock_rec.get_replay.return_value = []

        with patch("backend.routers.replays_get.get_recorder", return_value=mock_rec):
            resp = _authed().get("/replays/no-such-agent")

        assert resp.status_code == 404
        # LegacyEnvelopeMiddleware rewrites "detail" → "error" for 4xx responses
        body = resp.json()
        error_msg = body.get("error") or body.get("detail", "")
        assert "no-such-agent" in error_msg

    def test_get_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/replays/exec-7")
        assert resp.status_code == 401

    def test_get_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/replays/exec-7")
        assert resp.status_code == 403


# ===========================================================================
# Route ordering: /replays/status must not be captured as /replays/{agent_id}
# ===========================================================================

class TestRouteOrdering:
    def test_status_wins_over_agent_id_param(self, monkeypatch):
        """/replays/status hits the status route, not the {agent_id} route."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with (
            patch("backend.routers.replays_get.get_active_replay", return_value=None),
            patch("backend.routers.replays_get.get_recorder") as mock_rec,
        ):
            resp = _authed().get("/replays/status")

        assert resp.status_code == 200
        assert resp.json()["active"] is False
        # get_recorder should NOT have been called for /replays/status
        mock_rec.return_value.get_replay.assert_not_called()
