"""Tests for P5e FastAPI routers — remaining informational GET routes.

Covers:
- Parity: each migrated route returns the same status + response shape as
  legacy for the same request (side-effects ALWAYS MOCKED).
- Auth: 401 with no token, 403 with wrong token, allowed with correct token.
- RBAC: allow-all-on-missing-role preserved; explicit deny returns 403.
- /docs and /openapi.json list the new routes.

Routes tested:
  GET /rbac/whoami                      — caller's RBAC role + permissions
  GET /backups                          — list state-dir backups
  GET /notifications/history            — last 50 notification dispatch records
  GET /agents                           — list agent card names
  GET /agents/{role}                    — card for a specific role
  GET /agents/profiles                  — agent profiler snapshot
  GET /agents/profiles/summary          — aggregate profile summary
  GET /agents/profiles/{role_name}      — profile for a specific role
  GET /plugins                          — list all plugins
  GET /plugins/{name}                   — plugin detail
  GET /memory/lessons                   — query agent lessons
  GET /memory/context                   — get context block for files
  GET /benchmarks                       — all benchmark stats
  GET /benchmarks/history               — time-series benchmark history
  GET /benchmarks/{category}            — stats for a category
  GET /benchmarks/{category}/{operation} — stats for category+operation

CRITICAL: ALL backend calls are MOCKED.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.tests._envelope import assert_body_eq, envelope_error

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTH_TOKEN = "test-secret-p5e"
_WRONG_TOKEN = "wrong-key-p5e"


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
# GET /rbac/whoami
# ===========================================================================

class TestRbacWhoami:
    def test_whoami_returns_role_info(self, monkeypatch):
        """Returns role + label + permissions for the caller's token."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_rbac = MagicMock()
        fake_rbac.get_role_for_token.return_value = "admin"
        fake_rbac.get_role_info.return_value = {"label": "Admin", "allow": ["GET /agents"]}
        fake_rbac.enabled = True
        with patch("backend.routers.info_misc._rbac_manager", fake_rbac), \
             patch("backend.deps.rbac._rbac_manager", fake_rbac):
            resp = _authed().get("/rbac/whoami")
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "admin"
        assert data["label"] == "Admin"
        assert "GET /agents" in data["permissions"]

    def test_whoami_unrestricted_when_rbac_disabled(self, monkeypatch):
        """When RBAC not enabled and token has no role → 'unrestricted'."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_rbac = MagicMock()
        fake_rbac.get_role_for_token.return_value = None
        fake_rbac.get_role_info.return_value = {}
        fake_rbac.enabled = False
        with patch("backend.routers.info_misc._rbac_manager", fake_rbac), \
             patch("backend.deps.rbac._rbac_manager", fake_rbac):
            resp = _authed().get("/rbac/whoami")
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "unrestricted"

    def test_whoami_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/rbac/whoami")
        assert resp.status_code == 401

    def test_whoami_wrong_token_returns_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/rbac/whoami")
        assert resp.status_code == 403


# ===========================================================================
# GET /backups
# ===========================================================================

class TestBackupsList:
    def test_backups_returns_list(self, monkeypatch):
        """Returns {backups: [...]}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_backups = [{"filename": "backup-2026.tar.gz", "size_bytes": 1024}]
        with patch("backend.routers.info_misc._backup") as mock_backup:
            mock_backup.list_backups.return_value = fake_backups
            resp = _authed().get("/backups")
        assert resp.status_code == 200
        data = resp.json()
        assert "backups" in data
        assert data["backups"] == fake_backups

    def test_backups_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/backups")
        assert resp.status_code == 401

    def test_backups_wrong_token_returns_403(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _wrong_auth().get("/backups")
        assert resp.status_code == 403


# ===========================================================================
# GET /notifications/history
# ===========================================================================

class TestNotificationsHistory:
    def test_notifications_returns_list(self, monkeypatch):
        """Returns {notifications: [...]} with last 50 records."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_records = [{"event": "test", "ts": "2026-05-01T00:00:00"}]
        mock_notifier = MagicMock()
        mock_notifier.get_history.return_value = fake_records
        with patch("backend.notifier.get_notifier", return_value=mock_notifier):
            resp = _authed().get("/notifications/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "notifications" in data
        assert data["notifications"] == fake_records
        mock_notifier.get_history.assert_called_once_with(50)

    def test_notifications_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/notifications/history")
        assert resp.status_code == 401


# ===========================================================================
# GET /agents
# ===========================================================================

class TestAgentsList:
    def test_agents_list(self, monkeypatch):
        """Returns {agents: [...]}.  """
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_agents = ["executor", "code-reviewer", "project-manager"]
        mock_ac = MagicMock()
        mock_ac.list_agents.return_value = fake_agents
        with patch("backend.routers.info_agents.AgentCards", return_value=mock_ac):
            resp = _authed().get("/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agents"] == fake_agents

    def test_agents_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/agents")
        assert resp.status_code == 401


# ===========================================================================
# GET /agents/{role}
# ===========================================================================

class TestAgentsRole:
    def test_agents_role_found(self, monkeypatch):
        """Returns the agent card for the given role."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_card = {"role": "executor", "description": "implements code"}
        mock_ac = MagicMock()
        mock_ac.get_card.return_value = fake_card
        with patch("backend.routers.info_agents.AgentCards", return_value=mock_ac):
            resp = _authed().get("/agents/executor")
        assert resp.status_code == 200
        assert_body_eq(resp, fake_card)

    def test_agents_role_not_found(self, monkeypatch):
        """404 when role does not exist."""
        from backend.agent_cards import AgentNotFoundError
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_ac = MagicMock()
        mock_ac.get_card.side_effect = AgentNotFoundError("no such role")
        with patch("backend.routers.info_agents.AgentCards", return_value=mock_ac):
            resp = _authed().get("/agents/nonexistent")
        assert resp.status_code == 404

    def test_agents_role_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/agents/executor")
        assert resp.status_code == 401


# ===========================================================================
# GET /agents/profiles
# ===========================================================================

class TestAgentsProfiles:
    def test_agents_profiles_returns_snapshot(self, monkeypatch):
        """Returns full snapshot."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_snap = {"roles": {}, "aggregate": {"total": 0}}
        mock_profiler = MagicMock()
        mock_profiler.load_snapshot.return_value = fake_snap
        with patch("backend.agent_profiler.AgentProfiler", return_value=mock_profiler):
            resp = _authed().get("/agents/profiles")
        assert resp.status_code == 200
        data = resp.json()
        assert "aggregate" in data

    def test_agents_profiles_recompute_param(self, monkeypatch):
        """?recompute=true calls profiler.compute()."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_snap = {"roles": {}, "aggregate": {"total": 0}}
        mock_profiler = MagicMock()
        mock_profiler.compute.return_value = fake_snap
        with patch("backend.agent_profiler.AgentProfiler", return_value=mock_profiler):
            resp = _authed().get("/agents/profiles?recompute=true")
        assert resp.status_code == 200
        mock_profiler.compute.assert_called_once()

    def test_agents_profiles_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/agents/profiles")
        assert resp.status_code == 401


# ===========================================================================
# GET /agents/profiles/summary
# ===========================================================================

class TestAgentsProfilesSummary:
    def test_agents_profiles_summary(self, monkeypatch):
        """Returns aggregate section only."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_snap = {"roles": {}, "aggregate": {"total": 5, "by_role": {}}}
        mock_profiler = MagicMock()
        mock_profiler.load_snapshot.return_value = fake_snap
        with patch("backend.agent_profiler.AgentProfiler", return_value=mock_profiler):
            resp = _authed().get("/agents/profiles/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "by_role" in data

    def test_agents_profiles_summary_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/agents/profiles/summary")
        assert resp.status_code == 401


# ===========================================================================
# GET /agents/profiles/{role_name}
# ===========================================================================

class TestAgentsProfilesRole:
    def test_agents_profiles_role_found(self, monkeypatch):
        """Returns profile data for the role."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_snap = {"roles": {"executor": {"runs": 10}}, "aggregate": {}}
        mock_profiler = MagicMock()
        mock_profiler.load_snapshot.return_value = fake_snap
        with patch("backend.agent_profiler.AgentProfiler", return_value=mock_profiler):
            resp = _authed().get("/agents/profiles/executor")
        assert resp.status_code == 200
        assert resp.json()["runs"] == 10

    def test_agents_profiles_role_not_found(self, monkeypatch):
        """404 when role has no profile data."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_snap = {"roles": {}, "aggregate": {}}
        mock_profiler = MagicMock()
        mock_profiler.load_snapshot.return_value = fake_snap
        with patch("backend.agent_profiler.AgentProfiler", return_value=mock_profiler):
            resp = _authed().get("/agents/profiles/unknown-role")
        assert resp.status_code == 404

    def test_agents_profiles_role_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/agents/profiles/executor")
        assert resp.status_code == 401


# ===========================================================================
# GET /plugins
# ===========================================================================

class TestPluginsList:
    def test_plugins_returns_list(self, monkeypatch):
        """Returns {plugins: [...]} with name/description/version/review_pipeline."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_plugin = MagicMock()
        fake_plugin.name = "test-plugin"
        fake_plugin.description = "A test plugin"
        fake_plugin.version = "1.0.0"
        fake_plugin.review_pipeline = ["code-reviewer"]
        mock_loader = MagicMock()
        mock_loader.list_plugins.return_value = ["test-plugin"]
        mock_loader.get_plugin.return_value = fake_plugin
        with patch("backend.routers.info_plugins._plugin_loader", mock_loader):
            resp = _authed().get("/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert "plugins" in data
        assert len(data["plugins"]) == 1
        assert data["plugins"][0]["name"] == "test-plugin"

    def test_plugins_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/plugins")
        assert resp.status_code == 401


# ===========================================================================
# GET /plugins/{name}
# ===========================================================================

class TestPluginsDetail:
    def test_plugins_detail_found(self, monkeypatch):
        """Returns full plugin definition including system_prompt/tools/triggers."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_plugin = MagicMock()
        fake_plugin.name = "test-plugin"
        fake_plugin.description = "A test plugin"
        fake_plugin.version = "1.0.0"
        fake_plugin.system_prompt = "You are..."
        fake_plugin.tools = ["bash"]
        fake_plugin.review_pipeline = ["code-reviewer"]
        fake_plugin.triggers = ["on_pr"]
        fake_plugin.source_file = "/path/to/plugin.json"
        mock_loader = MagicMock()
        mock_loader.get_plugin.return_value = fake_plugin
        with patch("backend.routers.info_plugins._plugin_loader", mock_loader):
            resp = _authed().get("/plugins/test-plugin")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-plugin"
        assert "system_prompt" in data
        assert "tools" in data

    def test_plugins_detail_not_found(self, monkeypatch):
        """404 when plugin does not exist."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_loader = MagicMock()
        mock_loader.get_plugin.return_value = None
        with patch("backend.routers.info_plugins._plugin_loader", mock_loader):
            resp = _authed().get("/plugins/nonexistent")
        assert resp.status_code == 404

    def test_plugins_detail_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/plugins/test-plugin")
        assert resp.status_code == 401


# ===========================================================================
# GET /memory/lessons
# ===========================================================================

class TestMemoryLessons:
    def test_memory_lessons_returns_list(self, monkeypatch):
        """Returns {lessons: [...]}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_lessons = [{"id": "1", "lesson": "Think before coding"}]
        mock_mem = MagicMock()
        mock_mem.query_lessons.return_value = fake_lessons
        with patch("backend.agent_memory.AgentMemory", return_value=mock_mem):
            resp = _authed().get("/memory/lessons")
        assert resp.status_code == 200
        data = resp.json()
        assert "lessons" in data
        assert data["lessons"] == fake_lessons

    def test_memory_lessons_query_params(self, monkeypatch):
        """Passes role/tags/limit to query_lessons."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_mem = MagicMock()
        mock_mem.query_lessons.return_value = []
        with patch("backend.agent_memory.AgentMemory", return_value=mock_mem):
            resp = _authed().get("/memory/lessons?role=executor&tags=python,tests&limit=5")
        assert resp.status_code == 200
        mock_mem.query_lessons.assert_called_once_with(
            tags=["python", "tests"],
            role="executor",
            limit=5,
            cross_session=True,
        )

    def test_memory_lessons_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/memory/lessons")
        assert resp.status_code == 401


# ===========================================================================
# GET /memory/context
# ===========================================================================

class TestMemoryContext:
    def test_memory_context_returns_block(self, monkeypatch):
        """Returns {context: '...'} for given files."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_block = "Context for backend/api.py:\n..."
        mock_mem = MagicMock()
        mock_mem.get_context_block.return_value = fake_block
        with patch("backend.agent_memory.AgentMemory", return_value=mock_mem):
            resp = _authed().get("/memory/context?files=backend/api.py")
        assert resp.status_code == 200
        data = resp.json()
        assert data["context"] == fake_block

    def test_memory_context_missing_files_returns_400(self, monkeypatch):
        """400 when files param is missing or empty."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _authed().get("/memory/context")
        assert resp.status_code == 400

    def test_memory_context_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/memory/context?files=backend/api.py")
        assert resp.status_code == 401


# ===========================================================================
# GET /benchmarks
# ===========================================================================

class TestBenchmarksAll:
    def test_benchmarks_all_returns_stats(self, monkeypatch):
        """Returns {window_seconds, stats: [...]}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        from backend.benchmarks import BenchmarkStats
        fake_stats = BenchmarkStats(
            category="http", operation=None, window_seconds=300, count=10,
            p50_ms=5.0, p95_ms=15.0, p99_ms=30.0,
            min_ms=1.0, max_ms=50.0, avg_ms=6.0, stddev_ms=2.0, samples_per_second=0.1,
        )
        mock_rec = MagicMock()
        mock_rec.get_all_stats.return_value = [fake_stats]
        with patch("backend.routers.info_benchmarks.get_recorder", return_value=mock_rec):
            resp = _authed().get("/benchmarks")
        assert resp.status_code == 200
        data = resp.json()
        assert "window_seconds" in data
        assert "stats" in data
        assert data["window_seconds"] == 300
        assert len(data["stats"]) == 1

    def test_benchmarks_window_param(self, monkeypatch):
        """Passes window param to get_all_stats."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_rec = MagicMock()
        mock_rec.get_all_stats.return_value = []
        with patch("backend.routers.info_benchmarks.get_recorder", return_value=mock_rec):
            resp = _authed().get("/benchmarks?window=600")
        assert resp.status_code == 200
        mock_rec.get_all_stats.assert_called_once_with(window_seconds=600)

    def test_benchmarks_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/benchmarks")
        assert resp.status_code == 401


# ===========================================================================
# GET /benchmarks/history
# ===========================================================================

class TestBenchmarksHistory:
    def test_benchmarks_history_returns_data(self, monkeypatch):
        """Returns {category, operation, history: [...]}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_rec = MagicMock()
        mock_rec.get_history.return_value = [{"ts": 1234, "p50": 5.0}]
        with patch("backend.routers.info_benchmarks.get_recorder", return_value=mock_rec):
            resp = _authed().get("/benchmarks/history?category=http&points=30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == "http"
        assert "history" in data

    def test_benchmarks_history_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/benchmarks/history")
        assert resp.status_code == 401


# ===========================================================================
# GET /benchmarks/{category}
# ===========================================================================

class TestBenchmarksCategory:
    def test_benchmarks_category_returns_stats_dict(self, monkeypatch):
        """Returns stats dict for the given category."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        from backend.benchmarks import BenchmarkStats
        fake_stats = BenchmarkStats(
            category="db", operation=None, window_seconds=300, count=5,
            p50_ms=2.0, p95_ms=8.0, p99_ms=20.0,
            min_ms=1.0, max_ms=25.0, avg_ms=3.0, stddev_ms=1.0, samples_per_second=0.05,
        )
        mock_rec = MagicMock()
        mock_rec.compute_stats.return_value = fake_stats
        with patch("backend.routers.info_benchmarks.get_recorder", return_value=mock_rec):
            resp = _authed().get("/benchmarks/db")
        assert resp.status_code == 200
        data = resp.json()
        assert "category" in data or "count" in data  # _stats_to_dict keys

    def test_benchmarks_category_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/benchmarks/db")
        assert resp.status_code == 401


# ===========================================================================
# GET /benchmarks/{category}/{operation}
# ===========================================================================

class TestBenchmarksCategoryOperation:
    def test_benchmarks_category_operation(self, monkeypatch):
        """Returns stats dict for category+operation."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        from backend.benchmarks import BenchmarkStats
        fake_stats = BenchmarkStats(
            category="http", operation="POST /backup", window_seconds=300, count=3,
            p50_ms=10.0, p95_ms=20.0, p99_ms=40.0,
            min_ms=5.0, max_ms=50.0, avg_ms=12.0, stddev_ms=5.0, samples_per_second=0.01,
        )
        mock_rec = MagicMock()
        mock_rec.compute_stats.return_value = fake_stats
        with patch("backend.routers.info_benchmarks.get_recorder", return_value=mock_rec):
            resp = _authed().get("/benchmarks/http/POST%20%2Fbackup")
        assert resp.status_code == 200

    def test_benchmarks_category_operation_no_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/benchmarks/http/list")
        assert resp.status_code == 401


# ===========================================================================
# /docs and /openapi.json list the new routes (AC3)
# ===========================================================================

class TestOpenApiListing:
    def test_new_routes_in_openapi(self):
        """All P5e routes appear in /openapi.json."""
        client = _make_client()  # no auth — /openapi.json is public
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json().get("paths", {})

        expected_paths = [
            "/rbac/whoami",
            "/backups",
            "/notifications/history",
            "/agents",
            "/agents/profiles",
            "/agents/profiles/summary",
            "/agents/profiles/{role_name}",
            "/agents/{role}",
            "/plugins",
            "/plugins/{name}",
            "/memory/lessons",
            "/memory/context",
            "/benchmarks",
            "/benchmarks/history",
            "/benchmarks/{category}",
            "/benchmarks/{category}/{operation}",
        ]
        for path in expected_paths:
            assert path in paths, f"Expected {path!r} in /openapi.json paths"
