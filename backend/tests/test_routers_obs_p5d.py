"""Tests for P5d FastAPI routers — observability GET routes.

Covers:
- Parity: each migrated route returns the same status + response shape as
  the legacy handler for the same request (ALL side-effects MOCKED).
- Auth: public routes need no token; authed routes return 401 without token
  and 200 with a valid token.
- Content-type: /metrics MUST be text/plain; all others must be JSON.
- /docs and /openapi.json list the new routes.

Routes tested:
  GET /metrics          — Prometheus text (PUBLIC)
  GET /budget/status    — budget status (auth)
  GET /cost             — session cost (auth)
  GET /cost/summary     — cost summary (auth)
  GET /registry         — full registry + stats (auth)
  GET /control          — gates + policies (auth)
  GET /control/gates    — gates list (auth)
  GET /control/audit    — audit log (auth)
  GET /audit            — audit trail query (auth)
  GET /kpi              — full KPI (auth)
  GET /kpi/velocity     — velocity sub-key (auth)
  GET /kpi/cycle-time   — cycle-time sub-key (auth)
  GET /deps             — dep graph JSON (auth)
  GET /quality          — quality history (auth)

CRITICAL: ALL side-effects are MOCKED.
- generate_prometheus_metrics: never reads real metrics
- BudgetTracker / CostTracker: never touch blackboard or DB
- DiscussionRegistry: never reads registry.json
- ControlPlane: never reads config.json
- get_audit_trail: returns mock AuditTrail
- kpi_engine: never computes real KPI
- get_cached_dep_graph: returns mock dep graph
- QualityScorer: never reads quality store
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.tests._envelope import assert_body_eq


# ---------------------------------------------------------------------------
# Rate-limit bypass — prevents token-bucket exhaustion in combined runs
# ---------------------------------------------------------------------------
# The RateLimitMiddleware singleton drains its 60-token bucket across test
# files when they run together (burst=60, >60 requests per combined run).
# Setting AF_RATE_LIMIT_DISABLED=1 tells the middleware to pass everything
# through — same bypass used by the --no-rate-limit CLI flag and p5b tests.

@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    monkeypatch.setenv("AF_RATE_LIMIT_DISABLED", "1")


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_AUTH_TOKEN = "test-secret-obs-p5d"
_WRONG_TOKEN = "wrong-key-p5d"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_no_auth(monkeypatch: pytest.MonkeyPatch):
    """TestClient with auth disabled — all routes pass without a token."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def client_authed(monkeypatch: pytest.MonkeyPatch):
    """TestClient with auth enabled and a valid bearer token."""
    monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
    from backend.asgi_app import app
    with TestClient(
        app,
        raise_server_exceptions=True,
        headers={"Authorization": f"Bearer {_AUTH_TOKEN}"},
    ) as c:
        yield c


@pytest.fixture()
def client_no_token(monkeypatch: pytest.MonkeyPatch):
    """TestClient with auth enabled but NO token — expects 401."""
    monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ===========================================================================
# /metrics — Prometheus scrape (PUBLIC)
# ===========================================================================


class TestMetrics:
    def test_metrics_is_public_no_auth_required(self, monkeypatch: pytest.MonkeyPatch):
        """GET /metrics must return 200 even when auth is enabled and no token is sent."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        mock_body = "# HELP af_test Test metric\n# TYPE af_test gauge\naf_test 1\n"
        with patch("backend.routers.obs_metrics.generate_prometheus_metrics", return_value=mock_body):
            from backend.asgi_app import app
            with TestClient(app, raise_server_exceptions=True) as c:
                r = c.get("/metrics")
        assert r.status_code == 200

    def test_metrics_content_type_is_prometheus_text(self, monkeypatch: pytest.MonkeyPatch):
        """GET /metrics content-type MUST be text/plain; version=0.0.4; charset=utf-8."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        mock_body = "# HELP af_test Test metric\naf_test 1\n"
        with patch("backend.routers.obs_metrics.generate_prometheus_metrics", return_value=mock_body):
            from backend.asgi_app import app
            with TestClient(app, raise_server_exceptions=True) as c:
                r = c.get("/metrics")
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "text/plain" in ct, f"Expected text/plain content-type, got: {ct!r}"
        assert "version=0.0.4" in ct, f"Expected version=0.0.4 in content-type, got: {ct!r}"

    def test_metrics_body_passthrough(self, client_no_auth: TestClient):
        """GET /metrics body must be exactly what generate_prometheus_metrics returns."""
        expected = "# HELP af_loop_runs Total loop runs\naf_loop_runs 42\n"
        with patch("backend.routers.obs_metrics.generate_prometheus_metrics", return_value=expected):
            r = client_no_auth.get("/metrics")
        assert r.status_code == 200
        assert r.text == expected

    def test_metrics_in_openapi(self, client_no_auth: TestClient):
        """GET /metrics must appear in /openapi.json."""
        r = client_no_auth.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths", {})
        assert "/metrics" in paths, "/metrics missing from /openapi.json"


# ===========================================================================
# Auth-required routes — parametric tests
# ===========================================================================

_AUTHED_ROUTES = [
    "/budget/status",
    "/cost",
    "/cost/summary",
    "/registry",
    "/control",
    "/control/gates",
    "/control/audit",
    "/audit",
    "/kpi",
    "/kpi/velocity",
    "/kpi/cycle-time",
    "/deps",
    "/quality",
]


@pytest.mark.parametrize("route", _AUTHED_ROUTES)
def test_authed_route_returns_401_without_token(monkeypatch: pytest.MonkeyPatch, route: str):
    """Every authed obs route must return 401 when auth is enabled and no token is provided."""
    monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
    from backend.asgi_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(route)
    assert r.status_code == 401, f"{route} returned {r.status_code}, expected 401"


@pytest.mark.parametrize("route", _AUTHED_ROUTES)
def test_authed_route_returns_403_with_wrong_token(monkeypatch: pytest.MonkeyPatch, route: str):
    """Every authed obs route must return 403 when auth is enabled and token is wrong."""
    monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
    from backend.asgi_app import app
    with TestClient(
        app,
        raise_server_exceptions=False,
        headers={"Authorization": f"Bearer {_WRONG_TOKEN}"},
    ) as c:
        r = c.get(route)
    assert r.status_code == 403, f"{route} returned {r.status_code}, expected 403"


# ===========================================================================
# /budget/status
# ===========================================================================


class TestBudgetStatus:
    def test_budget_status_returns_json(self, client_no_auth: TestClient):
        """GET /budget/status returns JSON dict from BudgetTracker.get_status()."""
        mock_status = {"ceiling_usd": 10.0, "spent_usd": 1.23, "remaining_usd": 8.77}
        with patch("backend.routers.obs_cost.BudgetTracker") as MockBT:
            MockBT.return_value.get_status.return_value = mock_status
            r = client_no_auth.get("/budget/status")
        assert r.status_code == 200
        assert_body_eq(r, mock_status)

    def test_budget_status_content_type_json(self, client_no_auth: TestClient):
        with patch("backend.routers.obs_cost.BudgetTracker") as MockBT:
            MockBT.return_value.get_status.return_value = {}
            r = client_no_auth.get("/budget/status")
        assert "application/json" in r.headers.get("content-type", "")


# ===========================================================================
# /cost and /cost/summary
# ===========================================================================


class TestCost:
    def test_cost_returns_session_cost(self, client_no_auth: TestClient):
        """GET /cost returns what CostTracker.get_session_cost() returns."""
        mock_cost = {"total_usd": 0.42, "tokens": 1000}
        with patch("backend.routers.obs_cost.CostTracker") as MockCT:
            MockCT.return_value.get_session_cost.return_value = mock_cost
            r = client_no_auth.get("/cost")
        assert r.status_code == 200
        assert_body_eq(r, mock_cost)

    def test_cost_summary_returns_summary(self, client_no_auth: TestClient):
        """GET /cost/summary returns what CostTracker.get_summary() returns."""
        mock_summary = {"sessions": 3, "total_usd": 1.23}
        with patch("backend.routers.obs_cost.CostTracker") as MockCT:
            MockCT.return_value.get_summary.return_value = mock_summary
            r = client_no_auth.get("/cost/summary")
        assert r.status_code == 200
        assert_body_eq(r, mock_summary)


# ===========================================================================
# /registry
# ===========================================================================


class TestRegistry:
    def _mock_reg(self) -> MagicMock:
        reg = MagicMock()
        reg.show.return_value = {"discussions": [], "meta": "ok"}
        reg.stats.return_value = {"total": 0, "done": 0, "in_progress": 0, "spec_ready": 0}
        return reg

    def test_registry_returns_discussions_and_stats(self, client_no_auth: TestClient):
        """GET /registry merges show() and stats() into one payload."""
        with patch("backend.routers.obs_registry.DiscussionRegistry", return_value=self._mock_reg()):
            r = client_no_auth.get("/registry")
        assert r.status_code == 200
        body = r.json()
        assert "discussions" in body
        assert "stats" in body

    def test_registry_invalid_project_returns_400(self, client_no_auth: TestClient):
        """GET /registry?project=../../evil must return 400."""
        r = client_no_auth.get("/registry?project=../../evil")
        assert r.status_code == 400

    def test_registry_is_in_openapi(self, client_no_auth: TestClient):
        r = client_no_auth.get("/openapi.json")
        paths = r.json().get("paths", {})
        assert "/registry" in paths


# ===========================================================================
# /control, /control/gates, /control/audit
# ===========================================================================


class TestControl:
    def _mock_cp(self) -> MagicMock:
        cp = MagicMock()
        cp.list_gates.return_value = {"lint_must_pass": True}
        cp.get_policy.return_value = {}
        cp.get_audit_log.return_value = []
        return cp

    def test_control_returns_gates_and_policies(self, client_no_auth: TestClient):
        """GET /control returns {gates: ..., policies: ...}."""
        with patch("backend.routers.obs_control.ControlPlane", return_value=self._mock_cp()):
            r = client_no_auth.get("/control")
        assert r.status_code == 200
        body = r.json()
        assert "gates" in body
        assert "policies" in body
        # All four role keys must be present (mirrors api.py:2766-2770)
        assert set(body["policies"].keys()) == {
            "executor", "code-reviewer", "security-reviewer", "project-manager"
        }

    def test_control_gates(self, client_no_auth: TestClient):
        """GET /control/gates returns just the gates dict."""
        with patch("backend.routers.obs_control.ControlPlane", return_value=self._mock_cp()):
            r = client_no_auth.get("/control/gates")
        assert r.status_code == 200
        assert "lint_must_pass" in r.json()

    def test_control_audit(self, client_no_auth: TestClient):
        """GET /control/audit returns a list."""
        with patch("backend.routers.obs_control.ControlPlane", return_value=self._mock_cp()):
            r = client_no_auth.get("/control/audit")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ===========================================================================
# /audit
# ===========================================================================


class TestAudit:
    def test_audit_returns_entries(self, client_no_auth: TestClient):
        """GET /audit returns list from get_audit_trail().query()."""
        entries = [{"id": "1", "action": "spawn", "actor": "team-lead"}]
        mock_at = MagicMock()
        mock_at.query.return_value = entries
        with patch("backend.routers.obs_audit.get_audit_trail", return_value=mock_at):
            r = client_no_auth.get("/audit")
        assert r.status_code == 200
        assert r.json() == entries

    def test_audit_passes_query_params(self, client_no_auth: TestClient):
        """GET /audit?source=hook&action=spawn passes params to at.query()."""
        mock_at = MagicMock()
        mock_at.query.return_value = []
        with patch("backend.routers.obs_audit.get_audit_trail", return_value=mock_at):
            r = client_no_auth.get("/audit?source=hook&action=spawn&limit=10")
        assert r.status_code == 200
        mock_at.query.assert_called_once_with(
            source="hook", action="spawn", actor=None, since=None, limit=10
        )

    def test_audit_default_limit_is_50(self, client_no_auth: TestClient):
        mock_at = MagicMock()
        mock_at.query.return_value = []
        with patch("backend.routers.obs_audit.get_audit_trail", return_value=mock_at):
            client_no_auth.get("/audit")
        _, kwargs = mock_at.query.call_args
        assert kwargs.get("limit") == 50


# ===========================================================================
# /kpi, /kpi/velocity, /kpi/cycle-time
# ===========================================================================


class TestKPI:
    _FULL_KPI: dict = {
        "version": 1,
        "computed_at": None,
        "velocity": {"last_24h": 3, "all_time_per_day": 1.5, "total_done": 100},
        "estimation_accuracy": {"tasks_with_estimates": 5, "mean_absolute_error_hours": 1.2,
                                 "within_1_5x_pct": 80.0},
        "estimation": {"accuracy": None, "total_measured": 0, "min_samples": 5},
        "idle_rate": {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0},
        "pr_cycle_time": {"mean_hours": 4.0, "median_hours": 3.5, "total_measured": 20},
    }

    def test_kpi_returns_full_payload(self, client_no_auth: TestClient):
        """GET /kpi returns the full KPI dict."""
        with patch("backend.routers.obs_kpi._get_cached_kpi", return_value=self._FULL_KPI):
            r = client_no_auth.get("/kpi")
        assert r.status_code == 200
        body = r.json()
        assert "velocity" in body
        assert "pr_cycle_time" in body

    def test_kpi_velocity_returns_velocity_subkey(self, client_no_auth: TestClient):
        """GET /kpi/velocity returns just the velocity sub-object."""
        with patch("backend.routers.obs_kpi._get_cached_kpi", return_value=self._FULL_KPI):
            r = client_no_auth.get("/kpi/velocity")
        assert r.status_code == 200
        body = r.json()
        assert "last_24h" in body
        assert "total_done" in body

    def test_kpi_cycle_time_returns_cycle_time_subkey(self, client_no_auth: TestClient):
        """GET /kpi/cycle-time returns just the pr_cycle_time sub-object."""
        with patch("backend.routers.obs_kpi._get_cached_kpi", return_value=self._FULL_KPI):
            r = client_no_auth.get("/kpi/cycle-time")
        assert r.status_code == 200
        body = r.json()
        assert "mean_hours" in body
        assert "median_hours" in body

    def test_kpi_invalid_project_returns_400(self, client_no_auth: TestClient):
        """GET /kpi?project=../../bad must return 400."""
        r = client_no_auth.get("/kpi?project=../../bad")
        assert r.status_code == 400


# ===========================================================================
# /deps
# ===========================================================================


class TestDeps:
    def _mock_dg(self) -> MagicMock:
        dg = MagicMock()
        dg.to_json.return_value = {"nodes": [], "edges": []}
        dg.to_dot.return_value = "digraph G {}"
        dg.to_ascii.return_value = "backend.api\n  backend.metrics"
        dg.impact.return_value = {"module": "backend.api", "dependents": []}
        return dg

    def test_deps_json_default(self, client_no_auth: TestClient):
        """GET /deps returns JSON dep graph by default."""
        with patch("backend.routers.obs_deps.get_cached_dep_graph", return_value=self._mock_dg()):
            r = client_no_auth.get("/deps")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")

    def test_deps_dot_format_returns_text(self, client_no_auth: TestClient):
        """GET /deps?format=dot returns text/plain."""
        with patch("backend.routers.obs_deps.get_cached_dep_graph", return_value=self._mock_dg()):
            r = client_no_auth.get("/deps?format=dot")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
        assert "digraph" in r.text

    def test_deps_ascii_format_returns_text(self, client_no_auth: TestClient):
        """GET /deps?format=ascii returns text/plain."""
        with patch("backend.routers.obs_deps.get_cached_dep_graph", return_value=self._mock_dg()):
            r = client_no_auth.get("/deps?format=ascii")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")

    def test_deps_module_param_calls_impact(self, client_no_auth: TestClient):
        """GET /deps?module=foo calls dg.impact('foo')."""
        mock_dg = self._mock_dg()
        with patch("backend.routers.obs_deps.get_cached_dep_graph", return_value=mock_dg):
            r = client_no_auth.get("/deps?module=backend.api")
        assert r.status_code == 200
        mock_dg.impact.assert_called_once_with("backend.api")


# ===========================================================================
# /quality
# ===========================================================================


class TestQuality:
    def test_quality_returns_scores_list(self, client_no_auth: TestClient):
        """GET /quality returns {scores: [...]}."""
        scores = [{"pr": 1, "score": 85.0}, {"pr": 2, "score": 90.0}]
        mock_qs = MagicMock()
        mock_qs.history.return_value = scores
        # QualityScorer is imported inside the handler (deferred) — patch at source
        with patch("backend.quality_scorer.QualityScorer", return_value=mock_qs):
            r = client_no_auth.get("/quality")
        assert r.status_code == 200
        assert_body_eq(r, {"scores": scores})
        mock_qs.history.assert_called_once_with(limit=20)

    def test_quality_content_type_json(self, client_no_auth: TestClient):
        mock_qs = MagicMock()
        mock_qs.history.return_value = []
        with patch("backend.quality_scorer.QualityScorer", return_value=mock_qs):
            r = client_no_auth.get("/quality")
        assert "application/json" in r.headers.get("content-type", "")


# ===========================================================================
# OpenAPI / docs coverage
# ===========================================================================


class TestOpenAPIRoutes:
    """All new P5d routes must appear in /openapi.json."""

    _ALL_ROUTES = [
        "/metrics",
        "/budget/status",
        "/cost",
        "/cost/summary",
        "/registry",
        "/control",
        "/control/gates",
        "/control/audit",
        "/audit",
        "/kpi",
        "/kpi/velocity",
        "/kpi/cycle-time",
        "/deps",
        "/quality",
    ]

    def test_all_routes_in_openapi(self, client_no_auth: TestClient):
        r = client_no_auth.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths", {})
        missing = [route for route in self._ALL_ROUTES if route not in paths]
        assert not missing, f"Routes missing from /openapi.json: {missing}"

    def test_docs_accessible(self, client_no_auth: TestClient):
        r = client_no_auth.get("/docs")
        assert r.status_code == 200
