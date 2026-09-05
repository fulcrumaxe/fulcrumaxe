"""tests/orchestrator/test_dispatch_auto_route.py — Integration tests for SDK_AUTO_ROUTE gate (D#1346).

Verifies that dispatch.route() applies should_auto_route() correctly:
  - gate OFF (default): no behavior change — eligible roles without the explicit flag stay on CC
  - gate ON + eligible role + no explicit flag → auto-routed to SDK
  - gate ON + ineligible role → still CC (role gate holds)
  - gate ON + already sdk_eligible=True → still SDK (no double-flip)
  - audit log line is emitted on auto-route
  - ROUTE_VIA_DISPATCHER off path still unaffected

No real Anthropic API calls. CreditTracker and SDK runners are mocked.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.dispatch import route, _should_use_sdk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tracker(remaining: float = 100.0) -> MagicMock:
    t = MagicMock()
    t.remaining_usd.return_value = remaining
    t.soft_cap_breached.return_value = False
    return t


def _make_run_result(verdict: str = "done", agent_id: str = "auto-test-1") -> MagicMock:
    r = MagicMock()
    r.verdict = verdict
    r.agent_id = agent_id
    r.error = None
    r.input_tokens = 100
    r.output_tokens = 50
    return r


def _make_runner(result: MagicMock) -> MagicMock:
    """Return a mock SDK runner whose run() coroutine returns result."""
    async def _run(spec, auto_routed=None):
        return result

    runner = MagicMock()
    runner.run = _run
    return runner


# ---------------------------------------------------------------------------
# Gate OFF — zero behavior change
# ---------------------------------------------------------------------------

class TestAutoRouteGateOff:
    """When SDK_AUTO_ROUTE is unset, dispatch behaves exactly as before D#1346."""

    def test_eligible_role_no_flag_stays_cc_gate_off(self, monkeypatch):
        """docs-writer + no explicit flag + gate OFF → cc (no auto-spill)."""
        monkeypatch.delenv("SDK_AUTO_ROUTE", raising=False)
        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "discussion": 1346,
            # sdk_eligible absent → defaults False
        }
        mock_tracker = _make_tracker()
        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"):
            result = route(spec)
        assert result["route"] == "cc"
        assert result["verdict"] == "routed_to_cc"

    def test_all_eligible_roles_no_flag_stay_cc_gate_off(self, monkeypatch):
        """All 5 eligible roles without flag stay on CC when gate is off."""
        monkeypatch.delenv("SDK_AUTO_ROUTE", raising=False)
        from backend.orchestrator.offload_policy import SDK_ELIGIBLE_ROLES
        mock_tracker = _make_tracker()
        for role in SDK_ELIGIBLE_ROLES:
            spec = {"role": role, "task_prompt": "work", "discussion": 1346}
            with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
                 patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"):
                result = route(spec)
            assert result["route"] == "cc", (
                f"role={role!r}: expected cc with gate off, got {result['route']!r}"
            )

    def test_ineligible_role_stays_cc_gate_off(self, monkeypatch):
        monkeypatch.delenv("SDK_AUTO_ROUTE", raising=False)
        spec = {"role": "executor", "task_prompt": "implement", "discussion": 1346}
        mock_tracker = _make_tracker()
        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"):
            result = route(spec)
        assert result["route"] == "cc"


# ---------------------------------------------------------------------------
# Gate ON + eligible role → SDK
# ---------------------------------------------------------------------------

class TestAutoRouteGateOn:
    """When SDK_AUTO_ROUTE=1, eligible roles auto-route to SDK without the explicit flag."""

    def test_eligible_role_no_flag_auto_routes_sdk(self, monkeypatch):
        """docs-writer + no explicit flag + gate ON → sdk."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "discussion": 1346,
            # sdk_eligible absent — should be auto-set by the gate
        }
        mock_tracker = _make_tracker()
        mock_result = _make_run_result()
        mock_runner = _make_runner(mock_result)
        mock_hook = MagicMock()
        mock_hook.pre_spawn.return_value = True

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=mock_runner), \
             patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook):
            result = route(spec)

        assert result["route"] == "sdk", (
            f"Expected sdk with gate ON + eligible role, got {result['route']!r}"
        )
        assert result["verdict"] == "done"

    def test_gate_on_eligible_explicit_false_auto_routes_sdk(self, monkeypatch):
        """sdk_eligible=False explicitly + gate ON + eligible role → still auto-routes to sdk."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        spec = {
            "role": "run-analyst",
            "task_prompt": "analyse runs",
            "discussion": 1346,
            "sdk_eligible": False,  # explicit False, but gate overrides
        }
        mock_tracker = _make_tracker()
        mock_result = _make_run_result()
        mock_runner = _make_runner(mock_result)
        mock_hook = MagicMock()
        mock_hook.pre_spawn.return_value = True

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=mock_runner), \
             patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook):
            result = route(spec)

        assert result["route"] == "sdk"

    def test_gate_on_already_sdk_eligible_still_routes_sdk(self, monkeypatch):
        """gate ON + sdk_eligible=True (explicit) → sdk (no regression from pre-gate behavior)."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        spec = {
            "role": "quality-sweep",
            "task_prompt": "sweep",
            "discussion": 1346,
            "sdk_eligible": True,  # explicit flag — gate is additive, not conflicting
        }
        mock_tracker = _make_tracker()
        mock_result = _make_run_result()
        mock_runner = _make_runner(mock_result)
        mock_hook = MagicMock()
        mock_hook.pre_spawn.return_value = True

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=mock_runner), \
             patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook):
            result = route(spec)

        assert result["route"] == "sdk"

    def test_gate_on_audit_log_emitted(self, monkeypatch, caplog):
        """When auto-routing fires, an INFO line is logged with the role."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        spec = {
            "role": "feedback-scanner",
            "task_prompt": "scan feedback",
            "discussion": 1346,
        }
        mock_tracker = _make_tracker()
        mock_result = _make_run_result()
        mock_runner = _make_runner(mock_result)
        mock_hook = MagicMock()
        mock_hook.pre_spawn.return_value = True

        with caplog.at_level(logging.INFO, logger="backend.orchestrator.dispatch"), \
             patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=mock_runner), \
             patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook):
            route(spec)

        auto_route_logs = [r for r in caplog.records if "SDK_AUTO_ROUTE" in r.message]
        assert auto_route_logs, (
            "Expected at least one INFO log line mentioning SDK_AUTO_ROUTE when auto-routing fires"
        )
        assert "feedback-scanner" in auto_route_logs[0].message


# ---------------------------------------------------------------------------
# Gate ON + ineligible role → CC (role gate holds)
# ---------------------------------------------------------------------------

class TestAutoRouteIneligibleRoleGateOn:
    """Ineligible roles NEVER auto-route, even with SDK_AUTO_ROUTE=1."""

    @pytest.mark.parametrize("role", [
        "executor", "code-reviewer", "security-reviewer",
        "acceptance-tester", "project-manager", "team-lead",
    ])
    def test_ineligible_role_stays_cc_gate_on(self, monkeypatch, role):
        """All ineligible roles stay on CC when SDK_AUTO_ROUTE=1."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        spec = {
            "role": role,
            "task_prompt": "test",
            "discussion": 1346,
            # No sdk_eligible flag — gate should NOT auto-set it for ineligible roles
        }
        mock_tracker = _make_tracker()
        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"):
            result = route(spec)

        assert result["route"] == "cc", (
            f"role={role!r}: ineligible role must stay on CC even with SDK_AUTO_ROUTE=1, "
            f"got {result['route']!r}"
        )

    def test_ineligible_role_explicit_flag_still_cc_gate_on(self, monkeypatch):
        """executor + sdk_eligible=True + SDK_AUTO_ROUTE=1 → cc (role gate unconditional)."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        spec = {
            "role": "executor",
            "task_prompt": "implement",
            "discussion": 1346,
            "sdk_eligible": True,  # explicit True — still blocked by role gate
        }
        mock_tracker = _make_tracker()
        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"):
            result = route(spec)

        assert result["route"] == "cc"
