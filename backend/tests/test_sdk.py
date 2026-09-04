"""
Unit tests for backend/sdk.py — mocked HTTP, no running server required.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

# Allow imports from repo root
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.sdk import (
    ActiveAgent,
    AgentCard,
    APIError,
    AuditEntry,
    AuditStats,
    AutonomousClient,
    BudgetStatus,
    CostBreakdown,
    CycleTime,
    HealthStatus,
    KPISnapshot,
    LoopHealth,
    ModuleHealth,
    Notification,
    Registry,
    RegistryStats,
    ReplayMeta,
    ReplaySummary,
    SpawnQueueStatus,
    SpawnRequest,
    Velocity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(data: dict | list, status: int = 200) -> MagicMock:
    """Return a mock urllib response that yields JSON."""
    raw = json.dumps(data).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_http_error(status: int, body: dict | str) -> urllib.error.HTTPError:
    """Return a mock HTTPError with a readable body."""
    if isinstance(body, dict):
        raw = json.dumps(body).encode("utf-8")
    else:
        raw = body.encode("utf-8")
    fp = io.BytesIO(raw)
    return urllib.error.HTTPError(
        url="http://localhost:8080/test",
        code=status,
        msg="Error",
        hdrs={},  # type: ignore[arg-type]
        fp=fp,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClientConstruction(unittest.TestCase):
    def test_basic_construction(self):
        client = AutonomousClient("http://localhost:8080", token="test")
        self.assertEqual(client.base_url, "http://localhost:8080")
        self.assertEqual(client.token, "test")

    def test_trailing_slash_stripped(self):
        client = AutonomousClient("http://localhost:8080/", token="t")
        self.assertEqual(client.base_url, "http://localhost:8080")

    def test_token_from_env_var(self):
        with patch.dict(os.environ, {"AF_API_TOKEN": "env-token"}):
            client = AutonomousClient("http://localhost:8080")
        self.assertEqual(client.token, "env-token")

    def test_explicit_token_overrides_env(self):
        with patch.dict(os.environ, {"AF_API_TOKEN": "env-token"}):
            client = AutonomousClient("http://localhost:8080", token="explicit")
        self.assertEqual(client.token, "explicit")

    def test_context_manager(self):
        with AutonomousClient("http://localhost:8080", token="t") as c:
            self.assertIsInstance(c, AutonomousClient)


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="test")

    @patch("urllib.request.urlopen")
    def test_health_returns_dataclass(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"ok": True, "version": "1.0"})
        result = self.client.health()
        self.assertIsInstance(result, HealthStatus)
        self.assertTrue(result.ok)

    @patch("urllib.request.urlopen")
    def test_health_loop(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"healthy": True, "age_seconds": 42.0, "threshold_seconds": 600.0}
        )
        result = self.client.health_loop()
        self.assertIsInstance(result, LoopHealth)
        self.assertTrue(result.healthy)
        self.assertEqual(result.age_seconds, 42.0)

    @patch("urllib.request.urlopen")
    def test_health_modules_list(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            [{"name": "budget", "ok": True}, {"name": "registry", "ok": False, "error": "import error"}]
        )
        result = self.client.health_modules()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], ModuleHealth)
        self.assertEqual(result[0].name, "budget")
        self.assertFalse(result[1].ok)


class TestBudgetAndCost(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="test")

    @patch("urllib.request.urlopen")
    def test_budget_status(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"ceiling": 100, "spent": 12.5, "remaining": 87.5, "model": "claude-3"}
        )
        result = self.client.budget_status()
        self.assertIsInstance(result, BudgetStatus)
        self.assertEqual(result.ceiling, 100)
        self.assertEqual(result.model, "claude-3")

    @patch("urllib.request.urlopen")
    def test_cost(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"session_total": 5.0, "per_agent": {"executor": 3.0}, "per_discussion": {}}
        )
        result = self.client.cost()
        self.assertIsInstance(result, CostBreakdown)
        self.assertEqual(result.session_total, 5.0)
        self.assertEqual(result.per_agent["executor"], 3.0)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="test")

    @patch("urllib.request.urlopen")
    def test_registry_returns_discussions_list(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({
            "discussions": [
                {"number": 1, "title": "First", "status": "DONE"},
                {"number": 2, "title": "Second", "status": "SPEC_READY"},
            ]
        })
        result = self.client.registry()
        self.assertIsInstance(result, Registry)
        self.assertEqual(len(result.discussions), 2)
        self.assertEqual(result.discussions[0].number, 1)
        self.assertEqual(result.discussions[1].title, "Second")


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="test")

    @patch("urllib.request.urlopen")
    def test_audit_with_params(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([
            {"source": "api", "action": "GET /health", "actor": "sdk", "timestamp": "2026-01-01T00:00:00Z"}
        ])
        result = self.client.audit(source="api", limit=5)
        # Verify query params were encoded in the URL
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertIn("source=api", req.full_url)
        self.assertIn("limit=5", req.full_url)
        # Verify result
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], AuditEntry)
        self.assertEqual(result[0].source, "api")

    @patch("urllib.request.urlopen")
    def test_audit_none_params_excluded(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([])
        self.client.audit(source="api", action=None, limit=None)
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertNotIn("action=", req.full_url)
        self.assertNotIn("limit=", req.full_url)


class TestAgents(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="test")

    @patch("urllib.request.urlopen")
    def test_agent_found(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"role": "executor", "description": "Writes code", "capabilities": ["implement"]}
        )
        result = self.client.agent("executor")
        self.assertIsInstance(result, AgentCard)
        self.assertEqual(result.role, "executor")

    @patch("urllib.request.urlopen")
    def test_agent_not_found_raises_api_error(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(404, {"error": "role not found"})
        with self.assertRaises(APIError) as ctx:
            self.client.agent("nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("not found", ctx.exception.message)


class TestErrorHandling(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="test")

    @patch("urllib.request.urlopen")
    def test_401_raises_api_error(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(401, {"error": "unauthorized"})
        with self.assertRaises(APIError) as ctx:
            self.client.health()
        self.assertEqual(ctx.exception.status_code, 401)

    @patch("urllib.request.urlopen")
    def test_500_raises_api_error_with_message(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(500, {"error": "internal server error"})
        with self.assertRaises(APIError) as ctx:
            self.client.health()
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("internal server error", ctx.exception.message)


class TestKPI(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="test")

    @patch("urllib.request.urlopen")
    def test_kpi(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"velocity": {"prs_per_day": 2.5}, "cycle_time": {"median_hours": 4.0}}
        )
        result = self.client.kpi()
        self.assertIsInstance(result, KPISnapshot)
        self.assertEqual(result.velocity["prs_per_day"], 2.5)

    @patch("urllib.request.urlopen")
    def test_kpi_velocity(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"prs_per_day": 3.0})
        result = self.client.kpi_velocity()
        self.assertIsInstance(result, Velocity)
        self.assertEqual(result.prs_per_day, 3.0)

    @patch("urllib.request.urlopen")
    def test_kpi_cycle_time(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"median_hours": 6.0})
        result = self.client.kpi_cycle_time()
        self.assertIsInstance(result, CycleTime)
        self.assertEqual(result.median_hours, 6.0)


class TestSpawnQueue(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="test")

    @patch("urllib.request.urlopen")
    def test_spawn_queue(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"pending_count": 2, "active_count": 1, "utilization_pct": 50.0}
        )
        result = self.client.spawn_queue()
        self.assertIsInstance(result, SpawnQueueStatus)
        self.assertEqual(result.pending_count, 2)

    @patch("urllib.request.urlopen")
    def test_spawn_queue_pending(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([
            {"role": "executor", "prompt": "implement X", "discussion": 42}
        ])
        result = self.client.spawn_queue_pending()
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], SpawnRequest)
        self.assertEqual(result[0].role, "executor")
        self.assertEqual(result[0].discussion, 42)

    @patch("urllib.request.urlopen")
    def test_spawn_enqueue(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"queued": True, "id": "abc123"})
        result = self.client.spawn_enqueue("executor", "implement feature X", discussion=206)
        self.assertEqual(result["queued"], True)


class TestDataclassRepr(unittest.TestCase):
    """Verify all key dataclasses have meaningful __repr__."""

    def test_health_status_repr(self):
        h = HealthStatus(ok=True)
        self.assertIn("HealthStatus", repr(h))
        self.assertIn("True", repr(h))

    def test_agent_card_repr(self):
        a = AgentCard(role="executor")
        self.assertIn("executor", repr(a))

    def test_api_error_repr(self):
        e = APIError(404, "not found")
        self.assertIn("404", repr(e))
        self.assertIn("not found", repr(e))

    def test_audit_entry_repr(self):
        entry = AuditEntry(source="api", action="GET /health", actor="sdk")
        r = repr(entry)
        self.assertIn("api", r)
        self.assertIn("GET /health", r)

    def test_spawn_queue_status_repr(self):
        s = SpawnQueueStatus(pending_count=3, active_count=1)
        self.assertIn("3", repr(s))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# New tests — untested client methods
# ---------------------------------------------------------------------------


from backend.sdk import (  # noqa: E402
    ControlPlane,
    CostSummary,
    Gate,
    ReplayEvent,
    ReplaySummary,
    SpawnRequest,
    ActiveAgent,
    Notification,
    ReplayMeta,
)


class TestBudgetMethods(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="tok")

    @patch("urllib.request.urlopen")
    def test_budget_init(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"ok": True})
        result = self.client.budget_init(ceiling=500, model="claude-3")
        self.assertEqual(result["ok"], True)
        req = mock_urlopen.call_args[0][0]
        self.assertIn("/budget/init", req.full_url)
        self.assertEqual(req.method, "POST")

    @patch("urllib.request.urlopen")
    def test_cost_summary(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"total": 42.5, "model_breakdown": {"claude-3": 42.5}}
        )
        result = self.client.cost_summary()
        self.assertIsInstance(result, CostSummary)
        self.assertEqual(result.total, 42.5)
        self.assertEqual(result.model_breakdown["claude-3"], 42.5)


class TestControlPlaneMethods(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="tok")

    @patch("urllib.request.urlopen")
    def test_control(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"gates": [{"name": "auto_merge", "enabled": True}], "policies": {}}
        )
        result = self.client.control()
        self.assertIsInstance(result, ControlPlane)
        self.assertEqual(len(result.gates), 1)

    @patch("urllib.request.urlopen")
    def test_control_gates(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([
            {"name": "auto_merge", "enabled": True},
            {"name": "require_review", "enabled": False},
        ])
        result = self.client.control_gates()
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], Gate)
        self.assertEqual(result[0].name, "auto_merge")
        self.assertTrue(result[0].enabled)
        self.assertFalse(result[1].enabled)

    @patch("urllib.request.urlopen")
    def test_control_set(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"ok": True})
        result = self.client.control_set("auto_merge", True)
        self.assertEqual(result["ok"], True)
        req = mock_urlopen.call_args[0][0]
        self.assertIn("/control/set", req.full_url)
        self.assertEqual(req.method, "POST")

    @patch("urllib.request.urlopen")
    def test_control_audit(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([
            {"source": "api", "action": "set auto_merge=True", "actor": "team-lead", "timestamp": "2026-01-01T00:00:00Z"}
        ])
        result = self.client.control_audit()
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], AuditEntry)
        self.assertEqual(result[0].action, "set auto_merge=True")

    @patch("urllib.request.urlopen")
    def test_control_audit_empty(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([])
        result = self.client.control_audit()
        self.assertEqual(result, [])


class TestRegistryStatsMethods(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="tok")

    @patch("urllib.request.urlopen")
    def test_registry_stats(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"total": 15, "by_status": {"DONE": 10, "SPEC_READY": 5}}
        )
        result = self.client.registry_stats()
        self.assertIsInstance(result, RegistryStats)
        self.assertEqual(result.total, 15)
        self.assertEqual(result.by_status["DONE"], 10)


class TestReplayMethods(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="tok")

    @patch("urllib.request.urlopen")
    def test_replays_list(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([
            {"agent_id": "exec-42", "role": "executor", "started_at": "2026-01-01T00:00:00Z"},
        ])
        result = self.client.replays()
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], ReplayMeta)
        self.assertEqual(result[0].agent_id, "exec-42")

    @patch("urllib.request.urlopen")
    def test_replay_events(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([
            {"event_type": "prompt", "timestamp": "2026-01-01T00:00:00Z", "content": "implement"},
            {"event_type": "response", "timestamp": "2026-01-01T00:00:01Z", "content": "done"},
        ])
        result = self.client.replay("exec-42")
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], ReplayEvent)
        self.assertEqual(result[0].event_type, "prompt")
        self.assertEqual(result[1].event_type, "response")

    @patch("urllib.request.urlopen")
    def test_replay_summary(self, mock_urlopen):
        mock_urlopen.return_value = _make_response(
            {"agent_id": "exec-42", "role": "executor", "event_count": 20}
        )
        result = self.client.replay_summary("exec-42")
        self.assertIsInstance(result, ReplaySummary)
        self.assertEqual(result.agent_id, "exec-42")
        self.assertEqual(result.event_count, 20)

    @patch("urllib.request.urlopen")
    def test_replays_with_limit_param(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([])
        self.client.replays(limit=5)
        req = mock_urlopen.call_args[0][0]
        self.assertIn("limit=5", req.full_url)


class TestSpawnQueueActiveMethods(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="tok")

    @patch("urllib.request.urlopen")
    def test_spawn_queue_active(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([
            {"agent_id": "ag-1", "role": "executor", "started_at": "2026-01-01T00:00:00Z"},
        ])
        result = self.client.spawn_queue_active()
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], ActiveAgent)
        self.assertEqual(result[0].agent_id, "ag-1")
        self.assertEqual(result[0].role, "executor")

    @patch("urllib.request.urlopen")
    def test_spawn_queue_active_empty(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([])
        result = self.client.spawn_queue_active()
        self.assertEqual(result, [])


class TestNotificationMethods(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="tok")

    @patch("urllib.request.urlopen")
    def test_notifications_history(self, mock_urlopen):
        mock_urlopen.return_value = _make_response([
            {"channel": "slack", "message": "Deploy done", "sent_at": "2026-01-01T00:00:00Z", "success": True},
        ])
        result = self.client.notifications_history()
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], Notification)
        self.assertEqual(result[0].channel, "slack")
        self.assertTrue(result[0].success)

    @patch("urllib.request.urlopen")
    def test_notifications_test(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"sent": True})
        result = self.client.notifications_test()
        self.assertEqual(result["sent"], True)
        req = mock_urlopen.call_args[0][0]
        self.assertIn("/notifications/test", req.full_url)
        self.assertEqual(req.method, "POST")


# ---------------------------------------------------------------------------
# Dataclass from_dict tests
# ---------------------------------------------------------------------------


class TestDataclassFromDict(unittest.TestCase):
    def test_cost_summary_from_dict(self):
        d = {"total": 10.0, "model_breakdown": {"claude": 10.0}, "extra_field": "ignored"}
        cs = CostSummary.from_dict(d)
        self.assertEqual(cs.total, 10.0)
        self.assertEqual(cs.model_breakdown["claude"], 10.0)
        # extra fields go into .extra
        self.assertEqual(cs.extra.get("extra_field"), "ignored")

    def test_control_plane_from_dict(self):
        d = {"gates": [{"name": "g1", "enabled": True}], "policies": {"p": "v"}}
        cp = ControlPlane.from_dict(d)
        self.assertEqual(len(cp.gates), 1)
        self.assertEqual(cp.policies["p"], "v")

    def test_gate_from_dict(self):
        d = {"name": "auto_merge", "enabled": True}
        g = Gate.from_dict(d)
        self.assertEqual(g.name, "auto_merge")
        self.assertTrue(g.enabled)

    def test_replay_meta_from_dict(self):
        d = {"agent_id": "ag-1", "role": "executor", "started_at": "2026-01-01T00:00:00Z"}
        rm = ReplayMeta.from_dict(d)
        self.assertEqual(rm.agent_id, "ag-1")
        self.assertEqual(rm.role, "executor")

    def test_replay_event_from_dict(self):
        d = {"event_type": "prompt", "timestamp": "2026-01-01T00:00:00Z", "content": "text"}
        re_ = ReplayEvent.from_dict(d)
        self.assertEqual(re_.event_type, "prompt")
        self.assertEqual(re_.content, "text")

    def test_replay_summary_from_dict(self):
        d = {"agent_id": "ag-1", "role": "executor", "event_count": 5}
        rs = ReplaySummary.from_dict(d)
        self.assertEqual(rs.agent_id, "ag-1")
        self.assertEqual(rs.event_count, 5)

    def test_spawn_request_from_dict(self):
        d = {"role": "executor", "prompt": "do work", "discussion": 42}
        sr = SpawnRequest.from_dict(d)
        self.assertEqual(sr.role, "executor")
        self.assertEqual(sr.discussion, 42)

    def test_active_agent_from_dict(self):
        d = {"agent_id": "ag-2", "role": "code-reviewer", "started_at": "2026-01-01T00:00:00Z"}
        aa = ActiveAgent.from_dict(d)
        self.assertEqual(aa.agent_id, "ag-2")
        self.assertEqual(aa.role, "code-reviewer")

    def test_notification_from_dict(self):
        d = {"channel": "email", "message": "hi", "sent_at": "now", "success": False}
        n = Notification.from_dict(d)
        self.assertEqual(n.channel, "email")
        self.assertFalse(n.success)


# ---------------------------------------------------------------------------
# Error edge cases
# ---------------------------------------------------------------------------


class TestErrorEdgeCases(unittest.TestCase):
    def setUp(self):
        self.client = AutonomousClient("http://localhost:8080", token="tok",
                                       timeout_connect=1, timeout_read=1)

    @patch("urllib.request.urlopen")
    def test_connection_timeout_propagates(self, mock_urlopen):
        import socket
        mock_urlopen.side_effect = socket.timeout("timed out")
        with self.assertRaises(socket.timeout):
            self.client.health()

    @patch("urllib.request.urlopen")
    def test_non_json_error_response_body(self, mock_urlopen):
        import io
        import urllib.error
        fp = io.BytesIO(b"Internal Server Error (not JSON)")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://localhost:8080/health",
            code=500,
            msg="Internal Server Error",
            hdrs={},  # type: ignore[arg-type]
            fp=fp,
        )
        with self.assertRaises(APIError) as ctx:
            self.client.health()
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("not JSON", ctx.exception.message)

    @patch("urllib.request.urlopen")
    def test_429_rate_limit_raises_api_error(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(429, {"error": "rate limited"})
        with self.assertRaises(APIError) as ctx:
            self.client.health()
        self.assertEqual(ctx.exception.status_code, 429)
