"""backend/tests/test_auto_route.py — Unit tests for should_auto_route() (D#1346).

Tests the pure gate function in isolation. No dispatch logic involved here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.auto_route import should_auto_route
from backend.orchestrator.offload_policy import SDK_ELIGIBLE_ROLES


# ---------------------------------------------------------------------------
# Gate OFF (default) — zero effect regardless of role
# ---------------------------------------------------------------------------

class TestAutoRouteGateOff:
    """When SDK_AUTO_ROUTE is absent or not '1', should_auto_route always returns False."""

    def test_gate_unset_eligible_role_false(self, monkeypatch):
        monkeypatch.delenv("SDK_AUTO_ROUTE", raising=False)
        for role in SDK_ELIGIBLE_ROLES:
            assert should_auto_route(role) is False, (
                f"role={role!r}: should return False when SDK_AUTO_ROUTE is unset"
            )

    def test_gate_zero_string_eligible_role_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "0")
        for role in SDK_ELIGIBLE_ROLES:
            assert should_auto_route(role) is False, (
                f"role={role!r}: should return False when SDK_AUTO_ROUTE=0"
            )

    def test_gate_empty_string_eligible_role_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "")
        assert should_auto_route("docs-writer") is False

    def test_gate_false_string_eligible_role_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "false")
        assert should_auto_route("run-analyst") is False

    def test_gate_off_ineligible_role_false(self, monkeypatch):
        monkeypatch.delenv("SDK_AUTO_ROUTE", raising=False)
        for role in ["executor", "code-reviewer", "security-reviewer", "acceptance-tester",
                     "project-manager", "team-lead"]:
            assert should_auto_route(role) is False


# ---------------------------------------------------------------------------
# Gate ON — only eligible roles return True; ineligible always False
# ---------------------------------------------------------------------------

class TestAutoRouteGateOn:
    """When SDK_AUTO_ROUTE=1, eligible roles return True; ineligible return False."""

    def test_gate_on_all_eligible_roles_return_true(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        for role in SDK_ELIGIBLE_ROLES:
            assert should_auto_route(role) is True, (
                f"role={role!r}: should return True when SDK_AUTO_ROUTE=1 and role is eligible"
            )

    def test_gate_on_docs_writer_true(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("docs-writer") is True

    def test_gate_on_run_analyst_true(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("run-analyst") is True

    def test_gate_on_quality_sweep_true(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("quality-sweep") is True

    def test_gate_on_feedback_scanner_true(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("feedback-scanner") is True

    def test_gate_on_mission_analyst_true(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("mission-analyst") is True

    # --- Ineligible roles NEVER auto-route, even with gate on ---

    def test_gate_on_executor_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("executor") is False

    def test_gate_on_code_reviewer_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("code-reviewer") is False

    def test_gate_on_security_reviewer_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("security-reviewer") is False

    def test_gate_on_acceptance_tester_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("acceptance-tester") is False

    def test_gate_on_project_manager_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("project-manager") is False

    def test_gate_on_team_lead_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("team-lead") is False

    def test_gate_on_unknown_role_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("unknown-role") is False

    def test_gate_on_empty_role_false(self, monkeypatch):
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        assert should_auto_route("") is False
