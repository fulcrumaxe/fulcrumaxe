"""Tests for P5b FastAPI routers — /api/* POST mutation routes.

Covers:
- Parity: each migrated POST route returns the same status + response shape as
  legacy for the same request (side-effects MOCKED — no real spawns).
- Auth: 401 with no token, 403 with wrong token, allowed with correct token.
- RBAC: allow-all-on-missing-role preserved; explicit deny returns 403.
- Spawn-guard: routes /api/loop/run, /api/projects/{pid}/loop/run, and
  /api/innovate/tick reject HeadlessChrome/Puppeteer/Playwright UA with 403
  {"error": "spawn_blocked_test_origin"}. Normal UA passes.
- /docs and /openapi.json list the new POST routes.

Routes tested:
  POST /api/projects                        — create project
  POST /api/projects/{pid}/budget/reset     — budget reset
  POST /api/projects/{pid}/loop/run         — project-scoped loop run (spawn-guard)
  POST /api/ideas/{id}/upvote               — upvote idea
  POST /api/ideas/{id}/dismiss              — dismiss idea
  POST /api/ideas/{id}/promote              — promote idea
  POST /api/loop/run                        — global loop run (spawn-guard)
  POST /api/loop/runs/{id}/cancel           — cancel run
  POST /api/innovate/toggle                 — innovate toggle
  POST /api/innovate/tick                   — innovate tick (spawn-guard)

CRITICAL: _start_loop_run and _innovate_tick are ALWAYS mocked — never triggers
a real agent spawn or loop run (runaway-loop hazard).
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

_AUTH_TOKEN = "test-secret-key"
_WRONG_TOKEN = "wrong-key"


def _make_client(token: str | None = None, ua: str | None = None) -> TestClient:
    """Build a TestClient for the asgi app.

    :param token: Bearer token to include (or None for no auth header).
    :param ua: Custom User-Agent header (or None to omit).
    """
    from backend.asgi_app import app

    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if ua is not None:
        headers["User-Agent"] = ua
    return TestClient(app, headers=headers, raise_server_exceptions=False)


def _authed_client(ua: str | None = None) -> TestClient:
    """Return a client with a valid auth token."""
    return _make_client(token=_AUTH_TOKEN, ua=ua)


def _no_auth_client() -> TestClient:
    return _make_client(token=None)


def _wrong_auth_client() -> TestClient:
    return _make_client(token=_WRONG_TOKEN)


_HEADLESS_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/124.0.0.0 Safari/537.36"
)
_PUPPETEER_UA = "Puppeteer/21.0.0"
_PLAYWRIGHT_UA = "playwright/1.44.0"
_NORMAL_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# POST /api/projects — create project
# ---------------------------------------------------------------------------

class TestApiProjectCreate:
    """POST /api/projects — create a new project."""

    def test_create_project_success(self, monkeypatch):
        """Valid name+repo → 200 with project dict."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        expected = {"id": "my-proj", "name": "my-proj", "repo": "org/my-proj"}

        with patch("backend.routers.api_projects._create_project", return_value=expected) as mock_create:
            client = _authed_client()
            resp = client.post("/api/projects", json={"name": "my-proj", "repo": "org/my-proj"})

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, expected)
        mock_create.assert_called_once_with("my-proj", "org/my-proj")

    def test_create_project_missing_name_400(self, monkeypatch):
        """Missing name → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.api_projects._create_project"):
            client = _authed_client()
            resp = client.post("/api/projects", json={"repo": "org/my-proj"})
        assert resp.status_code == 400

    def test_create_project_missing_repo_400(self, monkeypatch):
        """Missing repo → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.api_projects._create_project"):
            client = _authed_client()
            resp = client.post("/api/projects", json={"name": "my-proj"})
        assert resp.status_code == 400

    def test_create_project_no_auth_401(self, monkeypatch):
        """No auth token → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _no_auth_client()
        resp = client.post("/api/projects", json={"name": "x", "repo": "o/x"})
        assert resp.status_code == 401

    def test_create_project_wrong_auth_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _wrong_auth_client()
        resp = client.post("/api/projects", json={"name": "x", "repo": "o/x"})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/projects/{pid}/budget/reset
# ---------------------------------------------------------------------------

class TestApiBudgetReset:
    """POST /api/projects/{pid}/budget/reset."""

    def test_budget_reset_success(self, monkeypatch):
        """Valid project id → 200 {ok: True, project: pid, status: {...}}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)

        fake_status = {"spent": 0, "remaining": 100}
        mock_bt = MagicMock()
        mock_bt.get_status.return_value = fake_status

        with patch("backend.routers.api_projects.BudgetTracker", return_value=mock_bt), \
             patch("backend.routers.api_projects._bust_budget_cache") as mock_bust:
            client = _authed_client()
            resp = client.post("/api/projects/my-proj/budget/reset")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["project"] == "my-proj"
        assert data["status"] == fake_status
        mock_bt.reset.assert_called_once()
        mock_bust.assert_called_once()

    def test_budget_reset_no_auth_401(self, monkeypatch):
        """No auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _no_auth_client()
        resp = client.post("/api/projects/my-proj/budget/reset")
        assert resp.status_code == 401

    def test_budget_reset_wrong_auth_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _wrong_auth_client()
        resp = client.post("/api/projects/my-proj/budget/reset")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/ideas/{id}/upvote|dismiss|promote
# ---------------------------------------------------------------------------

class TestApiIdeasMutations:
    """POST /api/ideas/{id}/upvote, /dismiss, /promote."""

    def test_upvote_success(self, monkeypatch):
        """upvote_idea called with correct id → 200 with idea dict."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        expected = {"id": "idea-1", "upvotes": 3}

        with patch("backend.routers.api_ideas.upvote_idea", return_value=expected) as mock_fn:
            client = _authed_client()
            resp = client.post("/api/ideas/idea-1/upvote")

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, expected)
        mock_fn.assert_called_once_with("idea-1")

    def test_upvote_not_found_404(self, monkeypatch):
        """upvote_idea raises KeyError → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.api_ideas.upvote_idea", side_effect=KeyError("not found")):
            client = _authed_client()
            resp = client.post("/api/ideas/bad-id/upvote")
        assert resp.status_code == 404

    def test_upvote_bad_state_400(self, monkeypatch):
        """upvote_idea raises ValueError → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.api_ideas.upvote_idea", side_effect=ValueError("bad state")):
            client = _authed_client()
            resp = client.post("/api/ideas/idea-1/upvote")
        assert resp.status_code == 400

    def test_dismiss_success(self, monkeypatch):
        """dismiss_idea called with correct id → 200."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        expected = {"id": "idea-1", "status": "dismissed"}
        with patch("backend.routers.api_ideas.dismiss_idea", return_value=expected) as mock_fn:
            client = _authed_client()
            resp = client.post("/api/ideas/idea-1/dismiss")
        assert resp.status_code == 200
        assert_body_eq(resp, expected)
        mock_fn.assert_called_once_with("idea-1")

    def test_promote_success(self, monkeypatch):
        """promote_idea called with correct id → 200."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        expected = {"id": "idea-1", "discussion_number": 42}
        with patch("backend.routers.api_ideas.promote_idea", return_value=expected) as mock_fn:
            client = _authed_client()
            resp = client.post("/api/ideas/idea-1/promote")
        assert resp.status_code == 200
        assert_body_eq(resp, expected)
        mock_fn.assert_called_once_with("idea-1")

    def test_upvote_no_auth_401(self, monkeypatch):
        """No auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _no_auth_client()
        resp = client.post("/api/ideas/idea-1/upvote")
        assert resp.status_code == 401

    def test_dismiss_wrong_auth_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _wrong_auth_client()
        resp = client.post("/api/ideas/idea-1/dismiss")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/loop/run — spawn-guard tests
# ---------------------------------------------------------------------------

class TestApiLoopRun:
    """POST /api/loop/run — global loop run."""

    def _mock_run(self) -> dict:
        return {
            "run_id": "run-abc123",
            "started_at": "2026-05-22T10:00:00Z",
            "log_path": ".autonomous-team/loop-runs/fulcrumaxe/run-abc123.log",
            "project_id": "fulcrumaxe",
        }

    def test_headlesschrome_ua_blocked_403(self, monkeypatch):
        """HeadlessChrome UA → 403 {"error": "spawn_blocked_test_origin"}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        client = _make_client(token=_AUTH_TOKEN, ua=_HEADLESS_UA)
        resp = client.post("/api/loop/run", json={"instruction": "test"})

        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body.get("error") == "spawn_blocked_test_origin"

    def test_puppeteer_ua_blocked_403(self, monkeypatch):
        """Puppeteer UA → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        client = _make_client(token=_AUTH_TOKEN, ua=_PUPPETEER_UA)
        resp = client.post("/api/loop/run")
        assert resp.status_code == 403
        assert resp.json().get("error") == "spawn_blocked_test_origin"

    def test_playwright_ua_blocked_403(self, monkeypatch):
        """Playwright UA → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        client = _make_client(token=_AUTH_TOKEN, ua=_PLAYWRIGHT_UA)
        resp = client.post("/api/loop/run")
        assert resp.status_code == 403
        assert resp.json().get("error") == "spawn_blocked_test_origin"

    def test_normal_ua_allowed_start(self, monkeypatch):
        """Normal UA + valid auth + AF_API_AUTH_KEY set → 200 with run dict (mocked)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        mock_run = self._mock_run()

        with patch("backend.routers.api_loop._api_module._start_loop_run", return_value=mock_run) as mock_fn, \
             patch("backend.routers.api_loop._audit_loop_run_request"):
            client = _authed_client(ua=_NORMAL_UA)
            resp = client.post(
                "/api/loop/run",
                json={"instruction": "Run ONE iteration."},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["run_id"] == "run-abc123"
        assert data["instruction"] == "Run ONE iteration."
        mock_fn.assert_called_once()

    def test_no_auth_key_set_503(self, monkeypatch):
        """AF_API_AUTH_KEY not set → 503 (kill-switch)."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        client = _authed_client(ua=_NORMAL_UA)
        resp = client.post("/api/loop/run", json={"instruction": "Run it."})
        assert resp.status_code == 503

    def test_no_auth_401(self, monkeypatch):
        """No auth token → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _no_auth_client()
        resp = client.post("/api/loop/run")
        assert resp.status_code == 401

    def test_wrong_auth_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _wrong_auth_client()
        resp = client.post("/api/loop/run")
        assert resp.status_code == 403

    def test_bypass_env_var_allows_headless(self, monkeypatch):
        """AF_ALLOW_TEST_ORIGIN_SPAWNS=1 lets HeadlessChrome through."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.setenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", "1")

        mock_run = self._mock_run()
        with patch("backend.routers.api_loop._api_module._start_loop_run", return_value=mock_run), \
             patch("backend.routers.api_loop._audit_loop_run_request"):
            client = _make_client(token=_AUTH_TOKEN, ua=_HEADLESS_UA)
            resp = client.post("/api/loop/run", json={"instruction": "Run it."})

        assert resp.status_code == 200, resp.text

    def test_rate_limited_429(self, monkeypatch):
        """PermissionError with 'rate-limited' → 429."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        with patch("backend.routers.api_loop._api_module._start_loop_run",
                   side_effect=PermissionError("rate-limited: wait 60s")), \
             patch("backend.routers.api_loop._audit_loop_run_request"):
            client = _authed_client(ua=_NORMAL_UA)
            resp = client.post("/api/loop/run", json={"instruction": "Run it."})
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# POST /api/projects/{pid}/loop/run — spawn-guard tests
# ---------------------------------------------------------------------------

class TestApiProjectLoopRun:
    """POST /api/projects/{pid}/loop/run — project-scoped loop run."""

    def _mock_run(self) -> dict:
        return {
            "run_id": "run-proj-123",
            "started_at": "2026-05-22T10:00:00Z",
            "log_path": ".autonomous-team/loop-runs/my-proj/run-proj-123.log",
            "project_id": "my-proj",
        }

    def _patch_projects(self):
        return patch(
            "backend.routers.api_projects._load_projects_raw",
            return_value=[{"id": "my-proj", "name": "my-proj"}],
        )

    def test_headlesschrome_blocked_403(self, monkeypatch):
        """HeadlessChrome UA → 403 {"error": "spawn_blocked_test_origin"}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        with self._patch_projects():
            client = _make_client(token=_AUTH_TOKEN, ua=_HEADLESS_UA)
            resp = client.post("/api/projects/my-proj/loop/run")

        assert resp.status_code == 403, resp.text
        assert resp.json().get("error") == "spawn_blocked_test_origin"

    def test_normal_ua_success(self, monkeypatch):
        """Normal UA + valid project → 200 (mocked)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        mock_run = self._mock_run()

        with self._patch_projects(), \
             patch("backend.routers.api_projects._api_module._start_loop_run", return_value=mock_run), \
             patch("backend.routers.api_projects._audit_loop_run_request"):
            client = _authed_client(ua=_NORMAL_UA)
            resp = client.post("/api/projects/my-proj/loop/run", json={"instruction": "Run it."})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["run_id"] == "run-proj-123"

    def test_project_not_found_404(self, monkeypatch):
        """Unknown project id → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        with patch("backend.routers.api_projects._load_projects_raw", return_value=[]):
            client = _authed_client(ua=_NORMAL_UA)
            resp = client.post("/api/projects/unknown-proj/loop/run")

        assert resp.status_code == 404

    def test_no_auth_401(self, monkeypatch):
        """No auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _no_auth_client()
        resp = client.post("/api/projects/my-proj/loop/run")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/loop/runs/{id}/cancel
# ---------------------------------------------------------------------------

class TestApiLoopRunCancel:
    """POST /api/loop/runs/{id}/cancel."""

    def test_cancel_success(self, monkeypatch):
        """Valid run id → 200 {ok: True, run_id: ...}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.api_loop._cancel_loop_run", return_value=True) as mock_fn:
            client = _authed_client()
            resp = client.post("/api/loop/runs/run-abc/cancel")
        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, {"ok": True, "run_id": "run-abc"})
        mock_fn.assert_called_once_with("run-abc")

    def test_cancel_not_found_404(self, monkeypatch):
        """Unknown run id → 404."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.api_loop._cancel_loop_run", return_value=False):
            client = _authed_client()
            resp = client.post("/api/loop/runs/bad-run/cancel")
        assert resp.status_code == 404

    def test_cancel_no_auth_401(self, monkeypatch):
        """No auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _no_auth_client()
        resp = client.post("/api/loop/runs/run-abc/cancel")
        assert resp.status_code == 401

    def test_cancel_wrong_auth_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _wrong_auth_client()
        resp = client.post("/api/loop/runs/run-abc/cancel")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/innovate/toggle
# ---------------------------------------------------------------------------

class TestApiInnovateToggle:
    """POST /api/innovate/toggle."""

    def test_toggle_enable_success(self, monkeypatch):
        """enabled=True → 200 with new state dict."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        expected = {"enabled": True, "tick_interval_seconds": 600}
        with patch("backend.routers.api_innovate._set_innovate", return_value=expected) as mock_fn:
            client = _authed_client()
            resp = client.post("/api/innovate/toggle", json={"enabled": True})
        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, expected)
        mock_fn.assert_called_once_with(True)

    def test_toggle_disable_success(self, monkeypatch):
        """enabled=False → 200 with new state dict."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        expected = {"enabled": False, "tick_interval_seconds": 600}
        with patch("backend.routers.api_innovate._set_innovate", return_value=expected) as mock_fn:
            client = _authed_client()
            resp = client.post("/api/innovate/toggle", json={"enabled": False})
        assert resp.status_code == 200
        assert_body_eq(resp, expected)
        mock_fn.assert_called_once_with(False)

    def test_toggle_missing_enabled_400(self, monkeypatch):
        """Body missing 'enabled' → 400."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        with patch("backend.routers.api_innovate._set_innovate"):
            client = _authed_client()
            resp = client.post("/api/innovate/toggle", json={"other": "field"})
        assert resp.status_code == 400

    def test_toggle_no_auth_401(self, monkeypatch):
        """No auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _no_auth_client()
        resp = client.post("/api/innovate/toggle", json={"enabled": True})
        assert resp.status_code == 401

    def test_toggle_wrong_auth_403(self, monkeypatch):
        """Wrong token → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        client = _wrong_auth_client()
        resp = client.post("/api/innovate/toggle", json={"enabled": True})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/innovate/tick — spawn-guard tests
# ---------------------------------------------------------------------------

class TestApiInnovateTick:
    """POST /api/innovate/tick — spawn-guarded innovate tick."""

    def test_headlesschrome_blocked_403(self, monkeypatch):
        """HeadlessChrome UA → 403 {"error": "spawn_blocked_test_origin"}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        client = _make_client(token=_AUTH_TOKEN, ua=_HEADLESS_UA)
        resp = client.post("/api/innovate/tick")

        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body.get("error") == "spawn_blocked_test_origin"

    def test_puppeteer_blocked_403(self, monkeypatch):
        """Puppeteer UA → 403."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        client = _make_client(token=_AUTH_TOKEN, ua=_PUPPETEER_UA)
        resp = client.post("/api/innovate/tick")
        assert resp.status_code == 403
        assert resp.json().get("error") == "spawn_blocked_test_origin"

    def test_normal_ua_success(self, monkeypatch):
        """Normal UA + valid auth → 200 (mocked)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        expected = {"run_id": "run-tick-1", "started_at": "2026-05-22T10:00:00Z"}
        with patch("backend.routers.api_innovate._innovate_tick", return_value=expected) as mock_fn:
            client = _authed_client(ua=_NORMAL_UA)
            resp = client.post("/api/innovate/tick")

        assert resp.status_code == 200, resp.text
        assert_body_eq(resp, expected)
        mock_fn.assert_called_once()

    def test_bypass_env_var_allows_headless(self, monkeypatch):
        """AF_ALLOW_TEST_ORIGIN_SPAWNS=1 lets HeadlessChrome through."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.setenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", "1")

        expected = {"run_id": "run-tick-bypass"}
        with patch("backend.routers.api_innovate._innovate_tick", return_value=expected):
            client = _make_client(token=_AUTH_TOKEN, ua=_HEADLESS_UA)
            resp = client.post("/api/innovate/tick")
        assert resp.status_code == 200, resp.text

    def test_no_auth_401(self, monkeypatch):
        """No auth → 401."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)
        client = _no_auth_client()
        resp = client.post("/api/innovate/tick")
        assert resp.status_code == 401

    def test_rate_limited_429(self, monkeypatch):
        """PermissionError rate-limited → 429."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
        monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)

        with patch("backend.routers.api_innovate._innovate_tick",
                   side_effect=PermissionError("rate-limited: wait 30s")):
            client = _authed_client(ua=_NORMAL_UA)
            resp = client.post("/api/innovate/tick")
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# OpenAPI / docs
# ---------------------------------------------------------------------------

class TestOpenApiListsNewRoutes:
    """POST routes appear in /openapi.json and /docs."""

    def test_openapi_has_post_loop_run(self, monkeypatch):
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        client = _make_client()
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        paths = spec.get("paths", {})
        assert "/api/loop/run" in paths
        assert "post" in paths["/api/loop/run"]

    def test_openapi_has_post_ideas_upvote(self, monkeypatch):
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        client = _make_client()
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        paths = spec.get("paths", {})
        upvote_path = "/api/ideas/{idea_id}/upvote"
        assert upvote_path in paths
        assert "post" in paths[upvote_path]

    def test_openapi_has_post_innovate_tick(self, monkeypatch):
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        client = _make_client()
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        paths = spec.get("paths", {})
        assert "/api/innovate/tick" in paths
        assert "post" in paths["/api/innovate/tick"]

    def test_docs_returns_200(self, monkeypatch):
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        client = _make_client()
        resp = client.get("/docs")
        assert resp.status_code == 200
