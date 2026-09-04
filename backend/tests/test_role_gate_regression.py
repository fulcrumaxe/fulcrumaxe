"""backend/tests/test_role_gate_regression.py — Role-gate regression test (D#1364 Spec item #1).

Safety net for the SDK lane role gate. Every dispatch entrypoint must reject EVERY
non-eligible (critical) role unconditionally — including under force modes (SHADOW_MODE=sdk)
and auto-route (SDK_AUTO_ROUTE=1). A future change that accidentally adds a critical role
to SDK_ELIGIBLE_ROLES will fail loudly here before any SDK rollout.

Three entrypoints covered:
  1. is_offload_eligible(role, sdk_eligible=True) — policy function
  2. should_auto_route(role) — auto-route gate (SDK_AUTO_ROUTE=1)
  3. _should_use_sdk(...) and route() — dispatch with SHADOW_MODE=sdk (force mode)

Positive control: the 5 eligible roles pass is_offload_eligible.
Set-membership guard: SDK_ELIGIBLE_ROLES contains EXACTLY the 5 expected roles.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.offload_policy import SDK_ELIGIBLE_ROLES, is_offload_eligible
from backend.orchestrator.auto_route import should_auto_route
from backend.orchestrator.dispatch import _should_use_sdk, route


# ---------------------------------------------------------------------------
# Canonical role sets
# ---------------------------------------------------------------------------

#: The exact 5 roles that are permitted on the SDK lane.
EXPECTED_ELIGIBLE_ROLES: frozenset[str] = frozenset({
    "docs-writer",
    "run-analyst",
    "quality-sweep",
    "feedback-scanner",
    "mission-analyst",
})

#: All roles that must NEVER reach the SDK lane.  Any role that writes code,
#: issues gate labels, or touches the control plane belongs here.
CRITICAL_ROLES: list[str] = [
    "executor",
    "code-reviewer",
    "security-reviewer",
    "acceptance-tester",
    "project-manager",
    "team-lead",
    "technical-architect",
    "product-owner",
    "cost-analyst",
    "performance-expert",
    "security-expert",
    "browser-tester",
    "visual-verifier",
    "incident-commander",
    "release-manager",
    "researcher",
]


# ---------------------------------------------------------------------------
# Guard: SDK_ELIGIBLE_ROLES must contain EXACTLY the 5 expected roles
# ---------------------------------------------------------------------------

class TestSDKEligibleRolesSetMembership:
    """Set-membership guard: adding a critical role to SDK_ELIGIBLE_ROLES fails this test."""

    def test_eligible_set_is_exactly_the_five_expected_roles(self):
        """SDK_ELIGIBLE_ROLES must equal the expected set — no more, no fewer.

        This is the primary regression trip-wire: if anyone adds a role such as
        'executor' or 'code-reviewer' to SDK_ELIGIBLE_ROLES, this assertion fails
        loudly and the PR is blocked.
        """
        assert SDK_ELIGIBLE_ROLES == EXPECTED_ELIGIBLE_ROLES, (
            f"SDK_ELIGIBLE_ROLES has drifted from the approved set.\n"
            f"Expected (sorted): {sorted(EXPECTED_ELIGIBLE_ROLES)}\n"
            f"Actual   (sorted): {sorted(SDK_ELIGIBLE_ROLES)}\n"
            f"Unexpected roles added: {sorted(SDK_ELIGIBLE_ROLES - EXPECTED_ELIGIBLE_ROLES)}\n"
            f"Roles removed:          {sorted(EXPECTED_ELIGIBLE_ROLES - SDK_ELIGIBLE_ROLES)}"
        )

    def test_no_critical_role_is_in_eligible_set(self):
        """Belt-and-suspenders: every critical role is absent from SDK_ELIGIBLE_ROLES."""
        leaked = [r for r in CRITICAL_ROLES if r in SDK_ELIGIBLE_ROLES]
        assert leaked == [], (
            f"Critical role(s) found in SDK_ELIGIBLE_ROLES — this is a security violation: "
            f"{leaked}"
        )


# ---------------------------------------------------------------------------
# Entrypoint 1: is_offload_eligible — policy function
# ---------------------------------------------------------------------------

class TestIsOffloadEligibleBlocksCriticalRoles:
    """is_offload_eligible must return False for every critical role, even with sdk_eligible=True."""

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_critical_role_with_flag_returns_false(self, role: str):
        """sdk_eligible=True cannot override the role gate — only the 5 eligible roles pass."""
        result = is_offload_eligible(role, sdk_eligible=True)
        assert result is False, (
            f"is_offload_eligible({role!r}, sdk_eligible=True) returned True — "
            f"critical role leaked into SDK lane"
        )

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_critical_role_without_flag_returns_false(self, role: str):
        result = is_offload_eligible(role, sdk_eligible=False)
        assert result is False, (
            f"is_offload_eligible({role!r}, sdk_eligible=False) returned True"
        )


class TestIsOffloadEligiblePositiveControl:
    """Positive control: the 5 eligible roles PASS when sdk_eligible=True."""

    @pytest.mark.parametrize("role", sorted(EXPECTED_ELIGIBLE_ROLES))
    def test_eligible_role_with_flag_returns_true(self, role: str):
        assert is_offload_eligible(role, sdk_eligible=True) is True, (
            f"is_offload_eligible({role!r}, sdk_eligible=True) should be True"
        )

    @pytest.mark.parametrize("role", sorted(EXPECTED_ELIGIBLE_ROLES))
    def test_eligible_role_without_flag_returns_false(self, role: str):
        """Eligible roles still need the flag — no implicit routing."""
        assert is_offload_eligible(role, sdk_eligible=False) is False, (
            f"is_offload_eligible({role!r}, sdk_eligible=False) should be False (flag required)"
        )


# ---------------------------------------------------------------------------
# Entrypoint 2: should_auto_route — auto-route gate
# ---------------------------------------------------------------------------

class TestShouldAutoRouteBlocksCriticalRoles:
    """should_auto_route must return False for every critical role, even with SDK_AUTO_ROUTE=1."""

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_critical_role_with_auto_route_env_returns_false(self, role: str, monkeypatch):
        """SDK_AUTO_ROUTE=1 must never auto-route a critical role to the SDK lane."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        result = should_auto_route(role)
        assert result is False, (
            f"should_auto_route({role!r}) returned True with SDK_AUTO_ROUTE=1 — "
            f"critical role would be auto-routed to SDK"
        )

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_critical_role_without_auto_route_env_returns_false(self, role: str, monkeypatch):
        monkeypatch.delenv("SDK_AUTO_ROUTE", raising=False)
        assert should_auto_route(role) is False


# ---------------------------------------------------------------------------
# Entrypoint 3a: _should_use_sdk — force-mode (SHADOW_MODE=sdk) must not bypass role gate
# ---------------------------------------------------------------------------

class TestShouldUseSDKBlocksCriticalRolesInForceMode:
    """_should_use_sdk with SHADOW_MODE=sdk must still return 'cc' for every critical role.

    The role gate is documented as UNCONDITIONAL — force modes do NOT override it.
    """

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_force_sdk_mode_returns_cc_for_critical_role(self, role: str):
        """SHADOW_MODE=sdk + sdk_eligible=True + credit available — role gate still wins."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=150.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role=role,
            sdk_eligible=True,
        )
        assert result == "cc", (
            f"_should_use_sdk(role={role!r}, shadow_mode='sdk', sdk_eligible=True) "
            f"returned {result!r} — critical role bypassed the role gate under SHADOW_MODE=sdk"
        )

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_default_mode_returns_cc_for_critical_role_with_flag(self, role: str):
        """In default mode, sdk_eligible=True + critical role → cc."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=150.0,
            shadow_mode="default",
            allow_fallback=False,
            role=role,
            sdk_eligible=True,
        )
        assert result == "cc", (
            f"_should_use_sdk(role={role!r}, shadow_mode='default', sdk_eligible=True) "
            f"returned {result!r}"
        )

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_both_mode_returns_cc_for_critical_role(self, role: str):
        """SHADOW_MODE=both must not run SDK side for critical roles."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=150.0,
            shadow_mode="both",
            allow_fallback=False,
            role=role,
            sdk_eligible=True,
        )
        assert result == "cc", (
            f"_should_use_sdk(role={role!r}, shadow_mode='both') returned {result!r} — "
            f"critical role entered SHADOW_MODE=both SDK side"
        )


# ---------------------------------------------------------------------------
# Entrypoint 3b: route() — full dispatch path, including SDK_AUTO_ROUTE
# ---------------------------------------------------------------------------

def _mock_tracker(remaining: float = 150.0) -> MagicMock:
    t = MagicMock()
    t.remaining_usd.return_value = remaining
    return t


class TestRouteBlocksCriticalRolesEndToEnd:
    """route() must return route='cc' for every critical role, under all force conditions:
    SHADOW_MODE=sdk, sdk_eligible=True, ROUTE_VIA_DISPATCHER=1, credential present.
    """

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_route_returns_cc_for_critical_role_with_force_sdk(self, role: str, monkeypatch):
        """End-to-end: SHADOW_MODE=sdk + sdk_eligible=True + credential → still cc."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "0")  # keep auto-route neutral
        spec = {
            "role": role,
            "task_prompt": "test task",
            "discussion": 1364,
            "sdk_eligible": True,
        }
        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=_mock_tracker()), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "sdk"):
            result = route(spec)

        assert result["route"] == "cc", (
            f"route(role={role!r}, sdk_eligible=True, SHADOW_MODE=sdk) "
            f"returned route={result['route']!r} — critical role reached SDK lane"
        )

    @pytest.mark.parametrize("role", CRITICAL_ROLES)
    def test_route_returns_cc_for_critical_role_with_auto_route(self, role: str, monkeypatch):
        """SDK_AUTO_ROUTE=1 must not auto-route critical roles."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")
        spec = {
            "role": role,
            "task_prompt": "test task",
            "discussion": 1364,
            "sdk_eligible": False,  # auto-route would set this True for eligible roles
        }
        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=_mock_tracker()), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"):
            result = route(spec)

        assert result["route"] == "cc", (
            f"route(role={role!r}, SDK_AUTO_ROUTE=1) returned route={result['route']!r} — "
            f"SDK_AUTO_ROUTE promoted a critical role to the SDK lane"
        )
