"""backend/tests/test_offload_policy.py — Unit tests for the SDK offload routing policy.

Verifies the two-condition gate in is_offload_eligible():
  1. sdk_eligible must be True (explicit opt-in)
  2. role must be in SDK_ELIGIBLE_ROLES (low-stakes background roles)

Both conditions must hold. Either condition failing alone routes to CC.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.offload_policy import (
    SDK_ELIGIBLE_ROLES,
    is_offload_eligible,
)


# ---------------------------------------------------------------------------
# Core gate: eligible role + flag
# ---------------------------------------------------------------------------

class TestIsOffloadEligible:
    """Two-condition gate — both must be true for SDK routing."""

    def test_eligible_role_with_flag_returns_true(self):
        """An eligible role with sdk_eligible=True is the only path to the SDK."""
        assert is_offload_eligible("docs-writer", sdk_eligible=True) is True

    def test_eligible_role_without_flag_returns_false(self):
        """No implicit SDK routing — explicit opt-in is required."""
        assert is_offload_eligible("docs-writer", sdk_eligible=False) is False

    def test_ineligible_role_executor_with_flag_returns_false(self):
        """executor must never route to SDK even with the flag set."""
        assert is_offload_eligible("executor", sdk_eligible=True) is False

    def test_ineligible_role_code_reviewer_with_flag_returns_false(self):
        """code-reviewer must never route to SDK — it gates PR merges."""
        assert is_offload_eligible("code-reviewer", sdk_eligible=True) is False

    def test_ineligible_role_security_reviewer_with_flag_returns_false(self):
        """security-reviewer must stay on CC — it issues security-passed labels."""
        assert is_offload_eligible("security-reviewer", sdk_eligible=True) is False

    def test_unknown_role_with_flag_returns_false(self):
        """Unknown roles are not eligible — fail closed."""
        assert is_offload_eligible("unknown-role", sdk_eligible=True) is False

    def test_empty_role_with_flag_returns_false(self):
        """Empty string is not an eligible role."""
        assert is_offload_eligible("", sdk_eligible=True) is False


# ---------------------------------------------------------------------------
# All five eligible roles — each must return True with flag set
# ---------------------------------------------------------------------------

class TestAllEligibleRoles:
    """Each of the five approved background roles must be eligible when flagged."""

    @pytest.mark.parametrize("role", [
        "docs-writer",
        "run-analyst",
        "quality-sweep",
        "feedback-scanner",
        "mission-analyst",
    ])
    def test_each_eligible_role_with_flag_returns_true(self, role):
        assert is_offload_eligible(role, sdk_eligible=True) is True, (
            f"Role '{role}' should be eligible for the SDK offload lane when sdk_eligible=True"
        )

    @pytest.mark.parametrize("role", [
        "docs-writer",
        "run-analyst",
        "quality-sweep",
        "feedback-scanner",
        "mission-analyst",
    ])
    def test_each_eligible_role_without_flag_returns_false(self, role):
        """Eligible roles route to CC without the flag — no auto-spill."""
        assert is_offload_eligible(role, sdk_eligible=False) is False, (
            f"Role '{role}' must NOT route to SDK when sdk_eligible=False"
        )


# ---------------------------------------------------------------------------
# Ineligible roles — must always return False regardless of flag
# ---------------------------------------------------------------------------

class TestIneligibleRoles:
    """High-stakes and control-plane roles must always stay on CC."""

    @pytest.mark.parametrize("role", [
        "executor",
        "code-reviewer",
        "security-reviewer",
        "acceptance-tester",
        "project-manager",
        "team-lead",
        "browser-tester",
        "incident-commander",
        "release-manager",
        "visual-verifier",
    ])
    def test_ineligible_role_returns_false_even_with_flag(self, role):
        assert is_offload_eligible(role, sdk_eligible=True) is False, (
            f"Role '{role}' must never route to SDK regardless of sdk_eligible flag"
        )


# ---------------------------------------------------------------------------
# SDK_ELIGIBLE_ROLES set — structural invariants
# ---------------------------------------------------------------------------

class TestSDKEligibleRolesSet:
    """The frozenset must contain exactly the five approved roles."""

    def test_frozenset_contains_expected_roles(self):
        expected = {
            "docs-writer",
            "run-analyst",
            "quality-sweep",
            "feedback-scanner",
            "mission-analyst",
        }
        assert SDK_ELIGIBLE_ROLES == expected, (
            f"SDK_ELIGIBLE_ROLES mismatch.\n"
            f"Expected: {sorted(expected)}\n"
            f"Got:      {sorted(SDK_ELIGIBLE_ROLES)}"
        )

    def test_executor_not_in_eligible_roles(self):
        assert "executor" not in SDK_ELIGIBLE_ROLES

    def test_code_reviewer_not_in_eligible_roles(self):
        assert "code-reviewer" not in SDK_ELIGIBLE_ROLES

    def test_security_reviewer_not_in_eligible_roles(self):
        assert "security-reviewer" not in SDK_ELIGIBLE_ROLES

    def test_acceptance_tester_not_in_eligible_roles(self):
        assert "acceptance-tester" not in SDK_ELIGIBLE_ROLES
