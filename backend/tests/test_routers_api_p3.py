"""Tests for P3 FastAPI routers — /api/* GET routes.

Covers:
- Parity: each migrated route returns the same shape as legacy
- Auth: pre-auth routes work without a token
- RBAC: allow-all-on-missing-role preserved; explicit deny works
- /api/config: loopback gate + cross-origin gate (exact legacy messages)
- /api/sessions/current: loopback free, remote needs token
- /docs and /openapi.json list the new routes

Routes tested:
  GET /api/config             (localhost-only gate)
  GET /api/projects           (pre-auth)
  GET /api/projects/{pid}     (pre-auth)
  GET /api/projects/{pid}/loop/runs          (pre-auth)
  GET /api/projects/{pid}/loop/runs/{run_id} (pre-auth)
  GET /api/sessions/current   (loopback free / remote needs token)
  GET /api/sessions           (pre-auth)
  GET /api/events             (pre-auth, plain JSON)
  GET /api/fleet/projects     (pre-auth)
  GET /api/ideas              (pre-auth)
  GET /api/spawn-blocks       (pre-auth)
  GET /api/loop/runs          (pre-auth)
  GET /api/loop/runs/{id}     (pre-auth)
  GET /api/innovate           (pre-auth)
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(token: str | None = None) -> TestClient:
    """Import the asgi app fresh and wrap it in a TestClient.

    Imports are deferred so environment patches apply before module-level
    globals (like RBACManager) are constructed.
    """
    # Clear any cached module state so re-imports pick up monkeypatch.
    import sys
    for mod in list(sys.modules.keys()):
        if mod.startswith("backend.routers.api_") or mod == "backend.asgi_app":
            pass  # Keep cached to avoid reimport cost in this session

    from backend.asgi_app import app
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return TestClient(app, headers=headers, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# /api/config — loopback gate
# ---------------------------------------------------------------------------

class TestApiConfig:
    """Loopback gate behaviour for GET /api/config."""

    def test_loopback_returns_200(self, monkeypatch):
        """Caller from 127.0.0.1 gets 200 with dashboard config.

        TestClient uses 'testclient' as the peer address, not 127.0.0.1, so
        we patch _is_loopback to simulate a real loopback connection.
        """
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        dummy_config = {"rpcBaseUrl": "http://localhost:8765", "rpcToken": "", "dashboardVersion": "0.0.1"}

        with patch("backend.routers.api_config._is_loopback", return_value=True), \
             patch("backend.routers.api_config._get_dashboard_config", return_value=dummy_config):
            client = _make_client()
            resp = client.get("/api/config")

        assert resp.status_code == 200
        body = resp.json()
        assert body["rpcBaseUrl"] == "http://localhost:8765"

    def test_non_loopback_returns_403_with_exact_message(self, monkeypatch):
        """Non-loopback caller gets 403 with the exact legacy error message."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        from backend.routers.api_config import _MSG_NOT_LOCALHOST

        with patch("backend.routers.api_config._is_loopback", return_value=False):
            client = _make_client()
            resp = client.get("/api/config")

        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == _MSG_NOT_LOCALHOST

    def test_cross_origin_returns_403_with_exact_message(self, monkeypatch):
        """Cross-origin request from loopback caller gets 403 with exact message.

        The loopback check must pass first; then the Origin header is evaluated.
        Patch _is_loopback to simulate a real loopback connection.
        """
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        from backend.routers.api_config import _MSG_CROSS_ORIGIN

        dummy_config = {"rpcBaseUrl": "http://localhost:8765", "rpcToken": "", "dashboardVersion": "0.0.1"}

        with patch("backend.routers.api_config._is_loopback", return_value=True), \
             patch("backend.routers.api_config._get_dashboard_config", return_value=dummy_config):
            client = _make_client()
            resp = client.get("/api/config", headers={"Origin": "http://evil.example.com"})

        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == _MSG_CROSS_ORIGIN

    def test_localhost_origin_allowed(self, monkeypatch):
        """Origin: http://localhost:3000 is allowed (same-origin SPA)."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        dummy_config = {"rpcBaseUrl": "http://localhost:8765", "rpcToken": "", "dashboardVersion": "0.0.1"}

        with patch("backend.routers.api_config._is_loopback", return_value=True), \
             patch("backend.routers.api_config._get_dashboard_config", return_value=dummy_config):
            client = _make_client()
            resp = client.get("/api/config", headers={"Origin": "http://localhost:3000"})

        assert resp.status_code == 200

    def test_127_origin_allowed(self, monkeypatch):
        """Origin: http://127.0.0.1:5173 is allowed."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        dummy_config = {"rpcBaseUrl": "http://localhost:8765", "rpcToken": "", "dashboardVersion": "0.0.1"}

        with patch("backend.routers.api_config._is_loopback", return_value=True), \
             patch("backend.routers.api_config._get_dashboard_config", return_value=dummy_config):
            client = _make_client()
            resp = client.get("/api/config", headers={"Origin": "http://127.0.0.1:5173"})

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/projects — pre-auth route
# ---------------------------------------------------------------------------

class TestApiProjects:
    """GET /api/projects does not require a token."""

    def test_list_projects_no_auth_key(self, monkeypatch):
        """Works without auth key configured."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        with patch("backend.routers.api_projects._list_projects", return_value=[{"id": "p1"}]):
            client = _make_client()
            resp = client.get("/api/projects")

        assert resp.status_code == 200
        assert resp.json() == [{"id": "p1"}]

    def test_list_projects_with_auth_key_no_token(self, monkeypatch):
        """Works without a token even when AF_API_AUTH_KEY is set (pre-auth route)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "secret123")

        with patch("backend.routers.api_projects._list_projects", return_value=[]):
            client = _make_client()  # No token
            resp = client.get("/api/projects")

        assert resp.status_code == 200

    def test_single_project_found(self, monkeypatch):
        """Returns project data when found by id."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        projects = [{"id": "proj-abc", "name": "proj-abc", "status": "active"}]
        with patch("backend.routers.api_projects._load_projects_raw", return_value=projects):
            client = _make_client()
            resp = client.get("/api/projects/proj-abc")

        assert resp.status_code == 200
        assert resp.json()["id"] == "proj-abc"

    def test_single_project_not_found(self, monkeypatch):
        """Returns 404 when project id is unknown."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        with patch("backend.routers.api_projects._load_projects_raw", return_value=[]):
            client = _make_client()
            resp = client.get("/api/projects/no-such-project")

        assert resp.status_code == 404

    def test_invalid_project_id(self, monkeypatch):
        """Returns 400 for project ids that fail the CWE-22 validation."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        client = _make_client()
        resp = client.get("/api/projects/../etc")

        assert resp.status_code in (400, 404, 422)  # path normalised or rejected

    def test_project_loop_runs_list(self, monkeypatch):
        """Returns runs list for a known project."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        projects = [{"id": "proj-abc"}]
        runs = [{"id": "run-1", "status": "done"}]
        with patch("backend.routers.api_projects._load_projects_raw", return_value=projects), \
             patch("backend.routers.api_projects._list_loop_runs", return_value=runs), \
             patch("backend.routers.api_projects._validate_project_id", return_value=True):
            client = _make_client()
            resp = client.get("/api/projects/proj-abc/loop/runs")

        assert resp.status_code == 200
        assert_body_eq(resp, {"runs": runs})

    def test_project_loop_run_detail(self, monkeypatch):
        """Returns run detail for a known run in a known project."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        projects = [{"id": "proj-abc"}]
        run_data = {"id": "run-1", "lines": ["a", "b"]}
        with patch("backend.routers.api_projects._load_projects_raw", return_value=projects), \
             patch("backend.routers.api_projects._get_loop_run", return_value=run_data), \
             patch("backend.routers.api_projects._validate_project_id", return_value=True):
            client = _make_client()
            resp = client.get("/api/projects/proj-abc/loop/runs/run-1")

        assert resp.status_code == 200
        assert resp.json()["id"] == "run-1"

    def test_project_loop_run_not_found(self, monkeypatch):
        """Returns 404 when run is not found."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        projects = [{"id": "proj-abc"}]
        with patch("backend.routers.api_projects._load_projects_raw", return_value=projects), \
             patch("backend.routers.api_projects._get_loop_run", return_value=None), \
             patch("backend.routers.api_projects._validate_project_id", return_value=True):
            client = _make_client()
            resp = client.get("/api/projects/proj-abc/loop/runs/no-such-run")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/sessions — pre-auth and loopback-conditional
# ---------------------------------------------------------------------------

class TestApiSessions:
    """Session endpoints auth logic."""

    def test_sessions_list_no_auth(self, monkeypatch):
        """GET /api/sessions returns empty list, no auth needed."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        client = _make_client()
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert_body_eq(resp, {"sessions": []})

    def test_sessions_current_from_loopback(self, monkeypatch):
        """Loopback caller gets a session without a token.

        TestClient peer is 'testclient', so we patch _is_loopback.
        """
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        with patch("backend.routers.api_sessions._is_loopback", return_value=True):
            client = _make_client()
            resp = client.get("/api/sessions/current")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "dev-session"
        assert "createdAt" in body

    def test_sessions_current_remote_no_key_configured(self, monkeypatch):
        """Remote caller is denied 401 when no auth key is configured (CWE-306)."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        with patch("backend.routers.api_sessions._is_loopback", return_value=False):
            client = _make_client()
            resp = client.get("/api/sessions/current")

        assert resp.status_code == 401

    def test_sessions_current_remote_with_valid_token(self, monkeypatch):
        """Remote caller with valid token gets a session."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "secret-key")

        with patch("backend.routers.api_sessions._is_loopback", return_value=False):
            client = _make_client(token="secret-key")
            resp = client.get("/api/sessions/current")

        assert resp.status_code == 200

    def test_sessions_current_remote_with_wrong_token(self, monkeypatch):
        """Remote caller with wrong token gets 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "secret-key")

        with patch("backend.routers.api_sessions._is_loopback", return_value=False):
            client = _make_client(token="wrong-token")
            resp = client.get("/api/sessions/current")

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /api/events — plain JSON GET (NOT SSE)
# ---------------------------------------------------------------------------

class TestApiEvents:
    """GET /api/events is plain JSON, no auth required."""

    def test_events_returns_json(self, monkeypatch):
        """Returns {events, next_since} JSON without a token."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        with patch("backend.routers.api_events._read_audit_events", return_value=[]):
            client = _make_client()
            resp = client.get("/api/events")

        assert resp.status_code == 200
        body = resp.json()
        assert "events" in body
        assert "next_since" in body

    def test_events_strips_seq_from_wire_payload(self, monkeypatch):
        """Internal _seq field is stripped from the wire payload."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        raw_events = [{"_seq": 5, "kind": "foo", "msg": "bar"}]
        with patch("backend.routers.api_events._read_audit_events", return_value=raw_events):
            client = _make_client()
            resp = client.get("/api/events")

        body = resp.json()
        assert body["next_since"] == 5
        assert len(body["events"]) == 1
        assert "_seq" not in body["events"][0]

    def test_events_content_type_is_json_not_sse(self, monkeypatch):
        """Content-Type must be application/json, NOT text/event-stream."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        with patch("backend.routers.api_events._read_audit_events", return_value=[]):
            client = _make_client()
            resp = client.get("/api/events")

        assert "application/json" in resp.headers.get("content-type", "")

    def test_events_no_auth_needed_with_key_set(self, monkeypatch):
        """Pre-auth route: works without a token even when AF_API_AUTH_KEY is set."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "somekey")

        with patch("backend.routers.api_events._read_audit_events", return_value=[]):
            client = _make_client()  # No token
            resp = client.get("/api/events")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/fleet/projects
# ---------------------------------------------------------------------------

class TestApiFleet:
    def test_fleet_projects(self, monkeypatch):
        """Returns {projects: [...]} without auth."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        fleet_data = [{"name": "proj-a", "alive": True}]
        with patch("backend.fleet.runtime.discover_running_projects", return_value=fleet_data):
            client = _make_client()
            resp = client.get("/api/fleet/projects")

        assert resp.status_code == 200
        assert_body_eq(resp, {"projects": fleet_data})

    def test_fleet_projects_redacts_state_dir_repo_and_ports(self, monkeypatch):
        """D#2239: this is the unauthenticated, host-wide route the Discussion's
        own repro named. discover_running_projects() legitimately returns
        state_dir/repo/ports for internal callers, but none of the three may
        reach an unauthenticated caller of this route.
        """
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        fleet_data = [
            {
                "name": "gatekeep",
                "repo": "fulcrumaxe/gatekeep",
                "state_dir": "/home/user/.gatekeep-state",
                "ports": {"vite": 5173, "api": 18099, "rpc": 18100, "sse": 18101},
                "pids": {"api": 1234, "vite": 1235},
                "started_at": "2026-05-18T16:00:00Z",
                "alive": True,
                "ok": True,
            },
        ]
        with patch("backend.fleet.runtime.discover_running_projects", return_value=fleet_data):
            client = _make_client()
            resp = client.get("/api/fleet/projects")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["projects"]) == 1
        record = body["projects"][0]
        for forbidden in ("state_dir", "repo", "ports", "pids"):
            assert forbidden not in record, f"{forbidden!r} leaked in {record!r}"
        assert record["name"] == "gatekeep"
        assert record["alive"] is True
        assert record["ok"] is True


# ---------------------------------------------------------------------------
# /api/ideas and /api/spawn-blocks
# ---------------------------------------------------------------------------

class TestApiIdeas:
    def test_ideas_returns_shape(self, monkeypatch):
        """Returns {ideas, source_empty, fetched_at} without auth."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        with patch("backend.routers.api_ideas._load_ideas", return_value=([], True)):
            client = _make_client()
            resp = client.get("/api/ideas")

        assert resp.status_code == 200
        body = resp.json()
        assert "ideas" in body
        assert "source_empty" in body
        assert "fetched_at" in body

    def test_spawn_blocks_returns_list(self, monkeypatch):
        """Returns a list of spawn-block events without auth."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        block_data = [{"id": "b1", "reason": "dial_denied"}]
        with patch("backend.routers.api_ideas._spawn_blocks_list", return_value=block_data):
            client = _make_client()
            resp = client.get("/api/spawn-blocks")

        assert resp.status_code == 200
        assert resp.json() == block_data

    def test_spawn_blocks_limit_clamped(self, monkeypatch):
        """Limit is clamped to 1-100."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        captured_limit: list[int] = []

        def _fake_blocks(limit: int) -> list:
            captured_limit.append(limit)
            return []

        with patch("backend.routers.api_ideas._spawn_blocks_list", side_effect=_fake_blocks):
            client = _make_client()
            client.get("/api/spawn-blocks?limit=9999")

        assert captured_limit[0] == 100  # clamped to max


# ---------------------------------------------------------------------------
# /api/loop/runs
# ---------------------------------------------------------------------------

class TestApiLoop:
    def test_loop_runs_list(self, monkeypatch):
        """Returns {runs: [...]} without auth."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        runs = [{"id": "r1", "status": "done"}]
        with patch("backend.routers.api_loop._list_loop_runs", return_value=runs):
            client = _make_client()
            resp = client.get("/api/loop/runs")

        assert resp.status_code == 200
        assert_body_eq(resp, {"runs": runs})

    def test_loop_run_detail(self, monkeypatch):
        """Returns run detail for a known run id."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        run = {"id": "r1", "lines": ["line1"]}
        with patch("backend.routers.api_loop._get_loop_run", return_value=run):
            client = _make_client()
            resp = client.get("/api/loop/runs/r1")

        assert resp.status_code == 200
        assert resp.json()["id"] == "r1"

    def test_loop_run_not_found(self, monkeypatch):
        """Returns 404 for an unknown run id."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        with patch("backend.routers.api_loop._get_loop_run", return_value=None):
            client = _make_client()
            resp = client.get("/api/loop/runs/no-such-run")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/innovate
# ---------------------------------------------------------------------------

class TestApiInnovate:
    def test_innovate_state(self, monkeypatch):
        """Returns innovate toggle state without auth."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        state = {"enabled": True, "tick": 42}
        with patch("backend.routers.api_innovate._innovate_state", return_value=state):
            client = _make_client()
            resp = client.get("/api/innovate")

        assert resp.status_code == 200
        assert_body_eq(resp, state)


# ---------------------------------------------------------------------------
# RBAC parity — allow-all-on-missing-role preserved
#
# Vehicle route: /replays/status (auth + RBAC, single-function dependency,
# easy to patch and assert on without touching real files or state).
# ---------------------------------------------------------------------------

class TestRbacParity:
    """Verify RBAC allow-all-on-missing-role behaviour is preserved."""

    def test_no_rbac_config_allows_all(self, tmp_path, monkeypatch):
        """When no rbac section in config, every authenticated request passes."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        # Import fresh rbac manager with empty config
        from backend.deps.rbac import make_require_rbac, _make_rbac_manager
        empty_cfg = tmp_path / "config.json"
        empty_cfg.write_text("{}")

        with patch("backend.deps.rbac._rbac_manager", _make_rbac_manager(empty_cfg)):
            with patch("backend.routers.replays_get.get_active_replay", return_value=None):
                client = _make_client()
                resp = client.get("/replays/status")

        assert resp.status_code == 200

    def test_token_not_in_rbac_table_allows(self, tmp_path, monkeypatch):
        """Token not listed in RBAC table → pass (single-key model compatibility)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", "shared-key")

        import json
        # RBAC section present but our token hash is NOT in the keys table.
        rbac_config = {
            "rbac": {
                "roles": {"viewer": {"allow": ["GET *"]}},
                "keys": {}  # No keys registered — our token has no role
            }
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(rbac_config))

        from backend.deps.rbac import _make_rbac_manager
        with patch("backend.deps.rbac._rbac_manager", _make_rbac_manager(cfg_file)):
            with patch("backend.routers.replays_get.get_active_replay", return_value=None):
                client = _make_client(token="shared-key")
                resp = client.get("/replays/status")

        assert resp.status_code == 200, "Token not in RBAC table should pass (allow-all-on-missing-role)"

    def test_rbac_explicit_deny(self, tmp_path, monkeypatch):
        """Token with a role that denies the route gets 403."""
        import json
        import hashlib

        deny_token = "deny-me-token"
        token_hash = hashlib.sha256(deny_token.encode()).hexdigest()

        rbac_config = {
            "rbac": {
                "roles": {"no-replays": {"allow": ["GET /api/projects"]}},
                "keys": {token_hash: "no-replays"}
            }
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(rbac_config))

        monkeypatch.setenv("AF_API_AUTH_KEY", deny_token)

        from backend.deps.rbac import _make_rbac_manager
        with patch("backend.deps.rbac._rbac_manager", _make_rbac_manager(cfg_file)):
            client = _make_client(token=deny_token)
            resp = client.get("/replays/status")

        assert resp.status_code == 403, "Role without GET /replays/status should be denied"


# ---------------------------------------------------------------------------
# OpenAPI lists the new routes
# ---------------------------------------------------------------------------

class TestOpenApiRoutes:
    def test_openapi_includes_api_config(self, monkeypatch):
        """/openapi.json should list /api/config."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        client = _make_client()
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json().get("paths", {})
        assert "/api/config" in paths

    def test_openapi_includes_api_projects(self, monkeypatch):
        """/openapi.json should list /api/projects."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        client = _make_client()
        resp = client.get("/openapi.json")
        paths = resp.json().get("paths", {})
        assert "/api/projects" in paths

    def test_docs_200(self, monkeypatch):
        """/docs returns 200."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        client = _make_client()
        resp = client.get("/docs")
        assert resp.status_code == 200
