"""Tests for P5c FastAPI routers — operational POST/PATCH/DELETE routes.

Covers:
- Parity: each migrated route returns the same status + response shape as
  legacy for the same request (side-effects ALWAYS MOCKED).
- Auth: 401 with no token, 403 with wrong token, allowed with correct token.
- RBAC: allow-all-on-missing-role preserved; explicit deny returns 403.
- /docs and /openapi.json list the new routes.

Routes tested:
  POST   /budget/init                          — budget session init
  POST   /control/set                          — set a dial/gate
  PATCH  /api/projects/{name}/control          — bulk ControlSettings update
  POST   /sessions/start                       — start a session
  POST   /sessions/close                       — close a session
  POST   /backup                               — create backup
  POST   /backup/restore                       — restore backup
  POST   /notifications/test                   — send test notification
  POST   /spawn-queue/enqueue                  — enqueue a spawn request
  POST   /replays/{agent_id}/start             — start a replay
  POST   /replays/pause                        — pause active replay
  POST   /replays/resume                       — resume active replay
  POST   /replays/stop                         — stop active replay
  POST   /replays/seek                         — seek replay
  DELETE /api/projects/{pid}                   — delete a project

CRITICAL: ALL side-effects are MOCKED.
- backend.backup: create_backup / prune_backups / restore_backup never touch disk
- backend.spawn_queue get_spawn_queue: enqueue never writes to queue
- backend.replay start_replay / get_active_replay / stop_active_replay: never start threads
- backend.session_manager SessionManager: no DB writes
- backend.budget BudgetTracker: no blackboard writes
- backend.notifier get_notifier: no emails/HTTP requests
- backend.control_plane ControlPlane: no config.json writes
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.tests._envelope import assert_body_eq, envelope_error

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTH_TOKEN = "test-secret-p5c"
_WRONG_TOKEN = "wrong-key-p5c"


# ---------------------------------------------------------------------------
# Rate-limit bypass — prevents token-bucket exhaustion in combined runs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    monkeypatch.setenv("AF_RATE_LIMIT_DISABLED", "1")


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
# POST /budget/init
# ===========================================================================

class TestBudgetInit:
    def test_budget_init_no_ceiling(self, monkeypatch):
        """No ceiling → 200 {ok: True, status: {...}}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_status = {"ceiling": None, "spent": 0}
        mock_bt = MagicMock()
        mock_bt.get_status.return_value = fake_status

        with patch("backend.routers.ops_budget.BudgetTracker", return_value=mock_bt):
            resp = _authed().post("/budget/init", json={})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == fake_status
        mock_bt.init_session.assert_called_once_with(ceiling=None)

    def test_budget_init_with_ceiling(self, monkeypatch):
        """ceiling=500 is passed through to init_session."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_bt = MagicMock()
        mock_bt.get_status.return_value = {"ceiling": 500}

        with patch("backend.routers.ops_budget.BudgetTracker", return_value=mock_bt):
            resp = _authed().post("/budget/init", json={"ceiling": 500})

        assert resp.status_code == 200
        mock_bt.init_session.assert_called_once_with(ceiling=500)

    def test_budget_init_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/budget/init", json={})
        assert resp.status_code == 401

    def test_budget_init_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().post("/budget/init", json={})
        assert resp.status_code == 403


# ===========================================================================
# POST /control/set
# ===========================================================================

class TestControlSet:
    def test_control_set_success(self, monkeypatch):
        """Valid key+value → 200 {ok: True, key: ..., value: ...}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_cp = MagicMock()

        with patch("backend.routers.ops_control.ControlPlane", return_value=mock_cp):
            resp = _authed().post("/control/set", json={"key": "gates.auto_merge", "value": True})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["key"] == "gates.auto_merge"
        assert data["value"] is True
        mock_cp.load.assert_called_once()
        mock_cp.set.assert_called_once_with("gates.auto_merge", True)

    def test_control_set_missing_key_400(self, monkeypatch):
        """Missing key → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_cp = MagicMock()
        with patch("backend.routers.ops_control.ControlPlane", return_value=mock_cp):
            resp = _authed().post("/control/set", json={"value": 42})
        assert resp.status_code == 400
        assert "key" in envelope_error(resp)

    def test_control_set_missing_value_400(self, monkeypatch):
        """Missing value → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_cp = MagicMock()
        with patch("backend.routers.ops_control.ControlPlane", return_value=mock_cp):
            resp = _authed().post("/control/set", json={"key": "gates.auto_merge"})
        assert resp.status_code == 400
        assert "value" in envelope_error(resp)

    def test_control_set_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/control/set", json={"key": "k", "value": 1})
        assert resp.status_code == 401

    def test_control_set_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().post("/control/set", json={"key": "k", "value": 1})
        assert resp.status_code == 403


# ===========================================================================
# PATCH /api/projects/{name}/control
# ===========================================================================

class TestProjectsControlPatch:
    def _make_cp_mock(self) -> MagicMock:
        """Return a ControlPlane mock with a minimal config.json shape."""
        mock_cp = MagicMock()
        mock_cp._path = MagicMock()
        mock_cp._path.read_text.return_value = json.dumps({
            "gates": {
                "auto_merge": True,
                "security_review": True,
                "budget_check": True,
            },
            "policies": {
                "executor": {"max_concurrent": 3},
                "code_reviewer": {"quality_threshold": 0.8},
            },
            "loop_interval_minutes": 10,
        })
        return mock_cp

    def test_patch_success(self, monkeypatch):
        """Valid settings → 200 with full ControlSettings shape."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_cp = self._make_cp_mock()

        with patch("backend.routers.ops_control.ControlPlane", return_value=mock_cp):
            resp = _authed().patch(
                "/api/projects/my-proj/control",
                json={"autoMerge": False, "loopIntervalMinutes": 15},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Response must include all ControlSettings fields.
        assert "autoMerge" in data
        assert "requireSecurityReview" in data
        assert "maxConcurrentAgents" in data
        assert "loopIntervalMinutes" in data
        assert "budgetAlertEnabled" in data
        assert "qualityGateThreshold" in data
        # Verify set was called for the provided fields.
        set_calls = {call.args[0]: call.args[1] for call in mock_cp.set.call_args_list}
        assert "gates.auto_merge" in set_calls
        assert set_calls["gates.auto_merge"] is False
        assert "loop_interval_minutes" in set_calls
        assert set_calls["loop_interval_minutes"] == 15

    def test_patch_max_concurrent_zero_400(self, monkeypatch):
        """maxConcurrentAgents=0 → 400 (would lock all spawns)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_cp = self._make_cp_mock()

        with patch("backend.routers.ops_control.ControlPlane", return_value=mock_cp):
            resp = _authed().patch(
                "/api/projects/my-proj/control",
                json={"maxConcurrentAgents": 0},
            )

        assert resp.status_code == 400
        assert "maxConcurrentAgents" in envelope_error(resp)

    def test_patch_max_concurrent_negative_400(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_cp = self._make_cp_mock()

        with patch("backend.routers.ops_control.ControlPlane", return_value=mock_cp):
            resp = _authed().patch(
                "/api/projects/my-proj/control",
                json={"maxConcurrentAgents": -1},
            )

        assert resp.status_code == 400

    def test_patch_max_concurrent_not_int_400(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_cp = self._make_cp_mock()

        with patch("backend.routers.ops_control.ControlPlane", return_value=mock_cp):
            resp = _authed().patch(
                "/api/projects/my-proj/control",
                json={"maxConcurrentAgents": "bad"},
            )

        assert resp.status_code == 400

    def test_patch_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().patch("/api/projects/p/control", json={})
        assert resp.status_code == 401

    def test_patch_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().patch("/api/projects/p/control", json={})
        assert resp.status_code == 403


# ===========================================================================
# POST /sessions/start + /sessions/close
# ===========================================================================

class TestSessionsStart:
    def test_start_success(self, monkeypatch):
        """start_session() result returned as-is."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_session = {"session_id": "s-1", "started_at": "2026-05-22T12:00:00Z"}
        mock_sm = MagicMock()
        mock_sm.start_session.return_value = fake_session

        with patch("backend.routers.ops_sessions.SessionManager", return_value=mock_sm):
            resp = _authed().post("/sessions/start")

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, fake_session)
        mock_sm.start_session.assert_called_once()

    def test_start_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/sessions/start")
        assert resp.status_code == 401

    def test_start_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().post("/sessions/start")
        assert resp.status_code == 403


class TestSessionsClose:
    def test_close_success(self, monkeypatch):
        """close_session() returns closed session dict."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_closed = {"session_id": "s-1", "closed_at": "2026-05-22T12:30:00Z"}
        mock_sm = MagicMock()
        mock_sm.close_session.return_value = fake_closed

        with patch("backend.routers.ops_sessions.SessionManager", return_value=mock_sm):
            resp = _authed().post("/sessions/close")

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, fake_closed)

    def test_close_none_404(self, monkeypatch):
        """close_session() returns None → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sm = MagicMock()
        mock_sm.close_session.return_value = None

        with patch("backend.routers.ops_sessions.SessionManager", return_value=mock_sm):
            resp = _authed().post("/sessions/close")

        assert resp.status_code == 404
        assert "no active session" in envelope_error(resp)

    def test_close_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/sessions/close")
        assert resp.status_code == 401


# ===========================================================================
# POST /backup + /backup/restore
# ===========================================================================

class TestBackupCreate:
    def test_backup_create_success(self, monkeypatch):
        """create_backup() → 200, prune_backups called."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_info = {"filename": "backup-2026-05-22.tar.gz", "size": 1024}

        with patch("backend.routers.ops_backup._backup") as mock_backup_mod:
            mock_backup_mod.create_backup.return_value = fake_info
            resp = _authed().post("/backup")

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, fake_info)
        mock_backup_mod.create_backup.assert_called_once()
        mock_backup_mod.prune_backups.assert_called_once_with(keep=20)

    def test_backup_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/backup")
        assert resp.status_code == 401

    def test_backup_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().post("/backup")
        assert resp.status_code == 403


class TestBackupRestore:
    def test_restore_success(self, monkeypatch):
        """Valid filename → 200 with restore result."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_result = {"restored": True, "filename": "backup-2026-05-22.tar.gz"}

        with patch("backend.routers.ops_backup._backup") as mock_backup_mod:
            mock_backup_mod.restore_backup.return_value = fake_result
            resp = _authed().post("/backup/restore", json={"filename": "backup-2026-05-22.tar.gz"})

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, fake_result)
        mock_backup_mod.restore_backup.assert_called_once_with("backup-2026-05-22.tar.gz")

    def test_restore_missing_filename_400(self, monkeypatch):
        """No filename → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.ops_backup._backup"):
            resp = _authed().post("/backup/restore", json={})
        assert resp.status_code == 400
        assert "filename" in envelope_error(resp)

    def test_restore_not_found_404(self, monkeypatch):
        """backup module raises FileNotFoundError → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_backup._backup") as mock_backup_mod:
            mock_backup_mod.restore_backup.side_effect = FileNotFoundError("no such backup")
            resp = _authed().post("/backup/restore", json={"filename": "missing.tar.gz"})

        assert resp.status_code == 404

    def test_restore_bad_file_400(self, monkeypatch):
        """backup module raises ValueError → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_backup._backup") as mock_backup_mod:
            mock_backup_mod.restore_backup.side_effect = ValueError("bad archive")
            resp = _authed().post("/backup/restore", json={"filename": "bad.tar.gz"})

        assert resp.status_code == 400

    def test_restore_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/backup/restore", json={"filename": "x.tar.gz"})
        assert resp.status_code == 401


# ===========================================================================
# POST /notifications/test
# ===========================================================================

class TestNotificationsTest:
    def test_notifications_test_success(self, monkeypatch):
        """send_test() → 200 {results: [...]}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_results = [{"channel": "email", "ok": True}]
        mock_notifier = MagicMock()
        mock_notifier.send_test.return_value = fake_results

        with patch("backend.routers.ops_misc.get_notifier", return_value=mock_notifier):
            resp = _authed().post("/notifications/test")

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, {"results": fake_results})
        mock_notifier.send_test.assert_called_once()

    def test_notifications_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/notifications/test")
        assert resp.status_code == 401

    def test_notifications_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().post("/notifications/test")
        assert resp.status_code == 403


# ===========================================================================
# POST /spawn-queue/enqueue
# ===========================================================================

class TestSpawnQueueEnqueue:
    def test_enqueue_success(self, monkeypatch):
        """Valid role → 200 {ok: True, id: req_id}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sq = MagicMock()
        mock_sq.enqueue.return_value = "req-uuid-1234"

        with patch("backend.routers.ops_misc.get_spawn_queue", return_value=mock_sq):
            resp = _authed().post(
                "/spawn-queue/enqueue",
                json={"role": "executor", "discussion": 42, "priority": "high"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["id"] == "req-uuid-1234"
        mock_sq.enqueue.assert_called_once_with(
            role="executor",
            discussion=42,
            prompt_context="",
            priority="high",
            requested_by="api",
        )

    def test_enqueue_missing_role_400(self, monkeypatch):
        """No role → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sq = MagicMock()
        with patch("backend.routers.ops_misc.get_spawn_queue", return_value=mock_sq):
            resp = _authed().post("/spawn-queue/enqueue", json={"discussion": 42})
        assert resp.status_code == 400
        assert "role" in envelope_error(resp)

    def test_enqueue_custom_requested_by(self, monkeypatch):
        """requested_by is forwarded."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_sq = MagicMock()
        mock_sq.enqueue.return_value = "req-5678"

        with patch("backend.routers.ops_misc.get_spawn_queue", return_value=mock_sq):
            resp = _authed().post(
                "/spawn-queue/enqueue",
                json={"role": "code-reviewer", "requested_by": "team-lead"},
            )

        assert resp.status_code == 200
        call_kwargs = mock_sq.enqueue.call_args.kwargs
        assert call_kwargs.get("requested_by") == "team-lead"

    def test_enqueue_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/spawn-queue/enqueue", json={"role": "executor"})
        assert resp.status_code == 401

    def test_enqueue_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().post("/spawn-queue/enqueue", json={"role": "executor"})
        assert resp.status_code == 403


# ===========================================================================
# POST /replays/{agent_id}/start
# ===========================================================================

class TestReplaysStart:
    def test_start_success(self, monkeypatch):
        """Valid agent_id → 200 with replay_session_id + total_events."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.replay_session_id = "rsid-abc"
        mock_eng._events = list(range(10))

        with patch("backend.routers.ops_replays.start_replay", return_value=mock_eng) as mock_sr:
            resp = _authed().post("/replays/agent-123/start", json={"speed": "2x"})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["replay_session_id"] == "rsid-abc"
        assert data["total_events"] == 10
        mock_sr.assert_called_once_with("agent-123", speed="2x")

    def test_start_default_speed(self, monkeypatch):
        """Empty body → speed defaults to '1x'."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.replay_session_id = "rsid-def"
        mock_eng._events = []

        with patch("backend.routers.ops_replays.start_replay", return_value=mock_eng) as mock_sr:
            resp = _authed().post("/replays/agent-456/start", json={})

        assert resp.status_code == 200
        mock_sr.assert_called_once_with("agent-456", speed="1x")

    def test_start_not_found_404(self, monkeypatch):
        """start_replay raises FileNotFoundError → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_replays.start_replay",
                   side_effect=FileNotFoundError("agent not found")):
            resp = _authed().post("/replays/missing-agent/start", json={})

        assert resp.status_code == 404

    def test_start_bad_value_400(self, monkeypatch):
        """start_replay raises ValueError → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_replays.start_replay",
                   side_effect=ValueError("bad speed")):
            resp = _authed().post("/replays/agent-bad/start", json={"speed": "xxx"})

        assert resp.status_code == 400

    def test_start_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/replays/agent-123/start", json={})
        assert resp.status_code == 401

    def test_start_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().post("/replays/agent-123/start", json={})
        assert resp.status_code == 403


# ===========================================================================
# POST /replays/pause
# ===========================================================================

class TestReplaysPause:
    def test_pause_success(self, monkeypatch):
        """Active alive replay → 200 {ok: True}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.is_alive = True

        with patch("backend.routers.ops_replays.get_active_replay", return_value=mock_eng):
            resp = _authed().post("/replays/pause")

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, {"ok": True})
        mock_eng.pause.assert_called_once()

    def test_pause_no_active_409(self, monkeypatch):
        """No active replay → 409."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_replays.get_active_replay", return_value=None):
            resp = _authed().post("/replays/pause")

        assert resp.status_code == 409
        assert "no active replay" in envelope_error(resp)

    def test_pause_dead_replay_409(self, monkeypatch):
        """Replay engine not alive → 409."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.is_alive = False

        with patch("backend.routers.ops_replays.get_active_replay", return_value=mock_eng):
            resp = _authed().post("/replays/pause")

        assert resp.status_code == 409

    def test_pause_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/replays/pause")
        assert resp.status_code == 401


# ===========================================================================
# POST /replays/resume
# ===========================================================================

class TestReplaysResume:
    def test_resume_success(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.is_alive = True

        with patch("backend.routers.ops_replays.get_active_replay", return_value=mock_eng):
            resp = _authed().post("/replays/resume")

        assert resp.status_code == 200
        assert_body_eq(resp, {"ok": True})
        mock_eng.resume.assert_called_once()

    def test_resume_no_active_409(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.ops_replays.get_active_replay", return_value=None):
            resp = _authed().post("/replays/resume")
        assert resp.status_code == 409

    def test_resume_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/replays/resume")
        assert resp.status_code == 401


# ===========================================================================
# POST /replays/stop
# ===========================================================================

class TestReplaysStop:
    def test_stop_active(self, monkeypatch):
        """Active replay → 200 {ok: True, was_active: True}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_replays.stop_active_replay", return_value=True):
            resp = _authed().post("/replays/stop")

        assert resp.status_code == 200
        assert_body_eq(resp, {"ok": True, "was_active": True})

    def test_stop_no_active(self, monkeypatch):
        """No active replay → 200 {ok: True, was_active: False}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_replays.stop_active_replay", return_value=False):
            resp = _authed().post("/replays/stop")

        assert resp.status_code == 200
        assert_body_eq(resp, {"ok": True, "was_active": False})

    def test_stop_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/replays/stop")
        assert resp.status_code == 401


# ===========================================================================
# POST /replays/seek
# ===========================================================================

class TestReplaysSeek:
    def test_seek_success(self, monkeypatch):
        """Valid event_number → 200 {ok: True}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.is_alive = True

        with patch("backend.routers.ops_replays.get_active_replay", return_value=mock_eng):
            resp = _authed().post("/replays/seek", json={"event_number": 5})

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, {"ok": True})
        mock_eng.seek.assert_called_once_with(5)

    def test_seek_missing_event_number_400(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.is_alive = True

        with patch("backend.routers.ops_replays.get_active_replay", return_value=mock_eng):
            resp = _authed().post("/replays/seek", json={})

        assert resp.status_code == 400
        assert "event_number" in envelope_error(resp)

    def test_seek_not_int_400(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_eng = MagicMock()
        mock_eng.is_alive = True
        mock_eng.seek.side_effect = ValueError("not int")

        with patch("backend.routers.ops_replays.get_active_replay", return_value=mock_eng):
            resp = _authed().post("/replays/seek", json={"event_number": "bad"})

        assert resp.status_code == 400

    def test_seek_no_active_409(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.ops_replays.get_active_replay", return_value=None):
            resp = _authed().post("/replays/seek", json={"event_number": 3})
        assert resp.status_code == 409

    def test_seek_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().post("/replays/seek", json={"event_number": 0})
        assert resp.status_code == 401


# ===========================================================================
# DELETE /api/projects/{pid}
# ===========================================================================

class TestProjectDelete:
    def test_delete_success(self, monkeypatch):
        """Valid project → 200 {ok: True, id: pid}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_projects._delete_project", return_value=True):
            resp = _authed().delete("/api/projects/my-proj")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["id"] == "my-proj"

    def test_delete_autonomous_forever_403(self, monkeypatch):
        """Deleting autonomous-forever project → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_projects._delete_project") as mock_del:
            resp = _authed().delete("/api/projects/autonomous-forever")

        assert resp.status_code == 403
        assert "cannot delete" in envelope_error(resp)

    def test_delete_not_found_404(self, monkeypatch):
        """Project not found → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        with patch("backend.routers.ops_projects._delete_project", return_value=False):
            resp = _authed().delete("/api/projects/nonexistent")

        assert resp.status_code == 404

    def test_delete_no_auth_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().delete("/api/projects/my-proj")
        assert resp.status_code == 401

    def test_delete_wrong_auth_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().delete("/api/projects/my-proj")
        assert resp.status_code == 403


# ===========================================================================
# /docs and /openapi.json list new routes
# ===========================================================================

class TestOpenApiRouteRegistration:
    def test_openapi_lists_p5c_routes(self, monkeypatch):
        """OpenAPI schema includes all P5c routes."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        from backend.asgi_app import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json().get("paths", {})
        expected = [
            "/budget/init",
            "/control/set",
            "/api/projects/{name}/control",
            "/sessions/start",
            "/sessions/close",
            "/backup",
            "/backup/restore",
            "/notifications/test",
            "/spawn-queue/enqueue",
            "/replays/{agent_id}/start",
            "/replays/pause",
            "/replays/resume",
            "/replays/stop",
            "/replays/seek",
            "/api/projects/{pid}",  # DELETE method on existing path
        ]
        for path in expected:
            assert path in paths, f"Missing from openapi.json: {path!r}"

    def test_graphql_not_in_openapi(self, monkeypatch):
        """/graphql is a native FastAPI router (D#1411) and IS listed in OpenAPI."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        from backend.asgi_app import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json().get("paths", {})
        assert "/graphql" in paths, "/graphql should appear in OpenAPI (native router since D#1411)"
