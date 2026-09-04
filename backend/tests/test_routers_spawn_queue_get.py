"""
Tests for backend/routers/spawn_queue_get.py

Covers:
  GET /spawn-queue           — queue status
  GET /spawn-queue/pending   — pending requests list
  GET /spawn-queue/active    — active agents list
  GET /spawn-blocks          — bare spawn-blocks events feed

Auth parity: all four routes require bearer auth (401 without header,
403 with wrong token). Matches the legacy api.py _check_auth + _check_rbac gate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def authed_client(monkeypatch, tmp_path):
    """TestClient with AF_API_AUTH_KEY set to a known token."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "test-spawn-queue-token")
    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def auth_headers():
    return {"Authorization": "Bearer test-spawn-queue-token"}


@pytest.fixture()
def mock_spawn_queue():
    """Return a mock SpawnQueue with controllable return values."""
    sq = MagicMock()
    sq.status.return_value = {
        "pending": 2,
        "active_total": 1,
        "total_limit": 6,
        "utilization_pct": 16,
        "by_role": {"executor": {"active": 1, "limit": 2}},
        "completed": 5,
        "failed": 0,
    }
    sq.list_pending.return_value = [
        {"id": "abc12345", "role": "executor", "discussion": 42, "priority": 20},
    ]
    sq.list_active.return_value = [
        {"id": "def67890", "role": "code-reviewer", "discussion": 7, "priority": 10},
    ]
    return sq


# ---------------------------------------------------------------------------
# Auth gate tests (401/403)
# ---------------------------------------------------------------------------


class TestAuthGate:
    def test_spawn_queue_401_no_token(self, monkeypatch):
        """GET /spawn-queue without auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
        from backend.asgi_app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/spawn-queue")
        assert resp.status_code == 401

    def test_spawn_queue_403_wrong_token(self, monkeypatch):
        """GET /spawn-queue with wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
        from backend.asgi_app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/spawn-queue", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 403

    def test_spawn_queue_pending_401_no_token(self, monkeypatch):
        """GET /spawn-queue/pending without auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
        from backend.asgi_app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/spawn-queue/pending")
        assert resp.status_code == 401

    def test_spawn_queue_active_401_no_token(self, monkeypatch):
        """GET /spawn-queue/active without auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
        from backend.asgi_app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/spawn-queue/active")
        assert resp.status_code == 401

    def test_spawn_blocks_401_no_token(self, monkeypatch):
        """GET /spawn-blocks without auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "secret")
        from backend.asgi_app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/spawn-blocks")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /spawn-queue
# ---------------------------------------------------------------------------


class TestSpawnQueueStatus:
    def test_returns_status_shape(self, authed_client, auth_headers, mock_spawn_queue):
        """GET /spawn-queue returns queue status dict from get_spawn_queue().status()."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            resp = authed_client.get("/spawn-queue", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "pending" in data
        assert "active_total" in data
        assert "utilization_pct" in data
        assert "by_role" in data

    def test_calls_status_method(self, authed_client, auth_headers, mock_spawn_queue):
        """Verifies get_spawn_queue().status() is actually called."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            authed_client.get("/spawn-queue", headers=auth_headers)
        mock_spawn_queue.status.assert_called_once()

    def test_status_values_passed_through(self, authed_client, auth_headers, mock_spawn_queue):
        """Verifies exact values from status() reach the response."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            resp = authed_client.get("/spawn-queue", headers=auth_headers)

        data = resp.json()
        assert data["pending"] == 2
        assert data["active_total"] == 1
        assert data["utilization_pct"] == 16


# ---------------------------------------------------------------------------
# GET /spawn-queue/pending
# ---------------------------------------------------------------------------


class TestSpawnQueuePending:
    def test_returns_pending_list_wrapped(self, authed_client, auth_headers, mock_spawn_queue):
        """GET /spawn-queue/pending returns {"pending": [...]} shape."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            resp = authed_client.get("/spawn-queue/pending", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "pending" in data
        assert isinstance(data["pending"], list)

    def test_pending_entries_present(self, authed_client, auth_headers, mock_spawn_queue):
        """Entries from list_pending() appear in the response."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            resp = authed_client.get("/spawn-queue/pending", headers=auth_headers)

        data = resp.json()
        assert len(data["pending"]) == 1
        assert data["pending"][0]["id"] == "abc12345"
        assert data["pending"][0]["role"] == "executor"

    def test_calls_list_pending(self, authed_client, auth_headers, mock_spawn_queue):
        """Verifies list_pending() is called (not status())."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            authed_client.get("/spawn-queue/pending", headers=auth_headers)
        mock_spawn_queue.list_pending.assert_called_once()
        mock_spawn_queue.status.assert_not_called()


# ---------------------------------------------------------------------------
# GET /spawn-queue/active
# ---------------------------------------------------------------------------


class TestSpawnQueueActive:
    def test_returns_active_list_wrapped(self, authed_client, auth_headers, mock_spawn_queue):
        """GET /spawn-queue/active returns {"active": [...]} shape."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            resp = authed_client.get("/spawn-queue/active", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "active" in data
        assert isinstance(data["active"], list)

    def test_active_entries_present(self, authed_client, auth_headers, mock_spawn_queue):
        """Entries from list_active() appear in the response."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            resp = authed_client.get("/spawn-queue/active", headers=auth_headers)

        data = resp.json()
        assert len(data["active"]) == 1
        assert data["active"][0]["id"] == "def67890"
        assert data["active"][0]["role"] == "code-reviewer"

    def test_calls_list_active(self, authed_client, auth_headers, mock_spawn_queue):
        """Verifies list_active() is called (not list_pending() or status())."""
        with patch("backend.routers.spawn_queue_get.get_spawn_queue", return_value=mock_spawn_queue):
            authed_client.get("/spawn-queue/active", headers=auth_headers)
        mock_spawn_queue.list_active.assert_called_once()
        mock_spawn_queue.status.assert_not_called()
        mock_spawn_queue.list_pending.assert_not_called()


# ---------------------------------------------------------------------------
# GET /spawn-blocks
# ---------------------------------------------------------------------------


class TestSpawnBlocks:
    def _write_feed(self, path: Path, events: list[dict]) -> None:
        """Write events as newline-delimited JSON to path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def test_returns_list_when_feed_missing(self, authed_client, auth_headers, tmp_path, monkeypatch):
        """If agent-feed.jsonl doesn't exist, returns empty list (no error)."""
        # Redirect _REPO_ROOT to a tmp dir without the feed file
        monkeypatch.setattr("backend.routers.spawn_queue_get._REPO_ROOT", tmp_path)
        resp = authed_client.get("/spawn-blocks", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_spawn_blocked_events(self, authed_client, auth_headers, tmp_path, monkeypatch):
        """Returns events where event_type == spawn_blocked."""
        feed = tmp_path / ".autonomous-team" / "agent-feed.jsonl"
        self._write_feed(feed, [
            {"event_type": "spawn_blocked", "role": "executor", "reason": "budget", "ts": "t1", "discussion": 42},
            {"event_type": "spawn_attempt", "role": "reviewer", "ts": "t2"},  # ignored
            {"event_type": "spawn_blocked", "role": "pm", "reason": "cap", "ts": "t3", "discussion": None},
        ])
        monkeypatch.setattr("backend.routers.spawn_queue_get._REPO_ROOT", tmp_path)

        resp = authed_client.get("/spawn-blocks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        # Only spawn_blocked events, returned newest-first (reversed order)
        roles = [e["role"] for e in data]
        assert "pm" in roles
        assert "executor" in roles
        assert "reviewer" not in roles

    def test_limit_param_respected(self, authed_client, auth_headers, tmp_path, monkeypatch):
        """?limit=1 returns at most 1 event."""
        feed = tmp_path / ".autonomous-team" / "agent-feed.jsonl"
        events = [
            {"event_type": "spawn_blocked", "role": f"role{i}", "reason": "x", "ts": f"t{i}"}
            for i in range(5)
        ]
        self._write_feed(feed, events)
        monkeypatch.setattr("backend.routers.spawn_queue_get._REPO_ROOT", tmp_path)

        resp = authed_client.get("/spawn-blocks?limit=1", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_default_limit_is_10(self, authed_client, auth_headers, tmp_path, monkeypatch):
        """Default limit is 10; 15 events → only 10 returned."""
        feed = tmp_path / ".autonomous-team" / "agent-feed.jsonl"
        events = [
            {"event_type": "spawn_blocked", "role": f"role{i}", "reason": "x", "ts": f"t{i:02d}"}
            for i in range(15)
        ]
        self._write_feed(feed, events)
        monkeypatch.setattr("backend.routers.spawn_queue_get._REPO_ROOT", tmp_path)

        resp = authed_client.get("/spawn-blocks", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 10

    def test_response_shape_matches_legacy(self, authed_client, auth_headers, tmp_path, monkeypatch):
        """Each entry has role, reason, ts, discussion fields (matching legacy shape)."""
        feed = tmp_path / ".autonomous-team" / "agent-feed.jsonl"
        self._write_feed(feed, [
            {"event_type": "spawn_blocked", "role": "executor", "reason": "budget", "ts": "2026-05-22T10:00:00Z", "discussion": 42},
        ])
        monkeypatch.setattr("backend.routers.spawn_queue_get._REPO_ROOT", tmp_path)

        resp = authed_client.get("/spawn-blocks", headers=auth_headers)
        data = resp.json()
        assert len(data) == 1
        entry = data[0]
        assert set(entry.keys()) == {"role", "reason", "ts", "discussion"}
        assert entry["role"] == "executor"
        assert entry["reason"] == "budget"
        assert entry["ts"] == "2026-05-22T10:00:00Z"
        assert entry["discussion"] == 42


# ---------------------------------------------------------------------------
# Coverage test: all 4 routes are natively registered (not proxy-only)
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    def _get_fastapi_paths(self):
        from backend.asgi_app import app
        from fastapi.routing import APIRoute

        return {r.path for r in app.routes if isinstance(r, APIRoute)}

    def test_spawn_queue_registered(self):
        assert "/spawn-queue" in self._get_fastapi_paths()

    def test_spawn_queue_pending_registered(self):
        assert "/spawn-queue/pending" in self._get_fastapi_paths()

    def test_spawn_queue_active_registered(self):
        assert "/spawn-queue/active" in self._get_fastapi_paths()

    def test_spawn_blocks_registered(self):
        assert "/spawn-blocks" in self._get_fastapi_paths()
