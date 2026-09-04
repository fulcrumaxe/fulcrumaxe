"""backend/tests/test_dispatch_offload.py — Routing tests for the selective offload policy.

Verifies that dispatch.route() and _should_use_sdk() correctly implement D#1322:
  - sdk_eligible+eligible-role → sdk (when credit available)
  - executor/reviewer (even with flag) → cc
  - eligible role WITHOUT the flag → cc
  - default spawn (no sdk_eligible field) → cc
  - SHADOW_MODE force modes still work for operator overrides

No real Anthropic API calls.  CreditTracker and SDK runners are mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.dispatch import _should_use_sdk, CreditExhaustedError, route


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_result(verdict: str = "done", agent_id: str = "bg-agent-1") -> MagicMock:
    r = MagicMock()
    r.verdict = verdict
    r.agent_id = agent_id
    r.error = None
    r.input_tokens = 100
    r.output_tokens = 50
    return r


def _make_tracker(remaining: float = 100.0) -> MagicMock:
    t = MagicMock()
    t.remaining_usd.return_value = remaining
    return t


def _route_with(spec_dict: dict, shadow_mode: str = "cc") -> dict:
    """Call route() with CreditTracker mocked to $100 and SHADOW_MODE patched."""
    mock_tracker = _make_tracker(100.0)
    with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
         patch("backend.orchestrator.dispatch._SHADOW_MODE", shadow_mode):
        return route(spec_dict)


# ---------------------------------------------------------------------------
# _should_use_sdk — selective routing truth table
# ---------------------------------------------------------------------------

class TestShouldUseSDKSelectivePolicy:
    """The D#1322 routing truth table for _should_use_sdk."""

    def test_eligible_role_with_flag_and_credit_returns_sdk(self):
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=True,
        )
        assert result == "sdk"

    def test_eligible_role_without_flag_returns_cc(self):
        """No auto-spill — the flag is required."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=False,
        )
        assert result == "cc"

    def test_executor_with_flag_returns_cc(self):
        """executor stays on CC even if the caller mistakenly sets sdk_eligible=True."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="executor",
            sdk_eligible=True,
        )
        assert result == "cc"

    def test_code_reviewer_with_flag_returns_cc(self):
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="code-reviewer",
            sdk_eligible=True,
        )
        assert result == "cc"

    def test_security_reviewer_with_flag_returns_cc(self):
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="security-reviewer",
            sdk_eligible=True,
        )
        assert result == "cc"

    def test_acceptance_tester_with_flag_returns_cc(self):
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="acceptance-tester",
            sdk_eligible=True,
        )
        assert result == "cc"

    @pytest.mark.parametrize("role", [
        "docs-writer",
        "run-analyst",
        "quality-sweep",
        "feedback-scanner",
        "mission-analyst",
    ])
    def test_all_five_eligible_roles_with_flag_return_sdk(self, role):
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role=role,
            sdk_eligible=True,
        )
        assert result == "sdk", f"Expected sdk for eligible role '{role}' with flag, got {result!r}"

    def test_unknown_role_with_flag_returns_cc(self):
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="mystery-role",
            sdk_eligible=True,
        )
        assert result == "cc"

    def test_no_flag_no_role_returns_cc(self):
        """A bare default spawn with no flags routes to CC."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="",
            sdk_eligible=False,
        )
        assert result == "cc"

    def test_alternate_shadow_mode_also_uses_selective_policy(self):
        """SHADOW_MODE=alternate is deprecated — selective policy still applies."""
        result = _should_use_sdk(
            discussion=863,    # odd (would have gone to sdk in old alternate logic)
            remaining_usd=100.0,
            shadow_mode="alternate",
            allow_fallback=False,
            role="executor",   # not eligible
            sdk_eligible=False,
        )
        assert result == "cc", (
            "SHADOW_MODE=alternate must not bring back auto-alternation for ineligible roles"
        )

    def test_alternate_with_eligible_role_and_flag_routes_sdk(self):
        """Under alternate mode, eligible role + flag still routes to SDK."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="alternate",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=True,
        )
        assert result == "sdk"

    def test_credit_exhausted_returns_cc_with_fallback(self):
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=0.0,
            shadow_mode="default",
            allow_fallback=True,
            role="docs-writer",
            sdk_eligible=True,
        )
        assert result == "cc"

    def test_credit_exhausted_raises_without_fallback(self):
        with pytest.raises(CreditExhaustedError):
            _should_use_sdk(
                discussion=None,
                remaining_usd=0.0,
                shadow_mode="default",
                allow_fallback=False,
                role="docs-writer",
                sdk_eligible=True,
            )

    def test_force_sdk_mode_ineligible_role_stays_cc(self):
        """SHADOW_MODE=sdk does NOT bypass the role gate — executor stays on cc."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="executor",
            sdk_eligible=False,
        )
        assert result == "cc", (
            "SHADOW_MODE=sdk must not override the role gate — executor is not eligible"
        )

    def test_force_sdk_mode_eligible_role_bypasses_flag(self):
        """SHADOW_MODE=sdk routes eligible-role spawns to SDK even without the flag."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=False,  # flag is off — force-sdk bypasses this for eligible roles
        )
        assert result == "sdk", (
            "SHADOW_MODE=sdk should bypass the sdk_eligible flag for an eligible role"
        )

    @pytest.mark.parametrize("role", [
        "executor",
        "code-reviewer",
        "security-reviewer",
        "acceptance-tester",
        "team-lead",
    ])
    def test_force_sdk_mode_never_routes_ineligible_roles(self, role):
        """SHADOW_MODE=sdk never overrides the role gate for ineligible roles."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role=role,
            sdk_eligible=True,  # even with the flag set, ineligible roles stay on CC
        )
        assert result == "cc", (
            f"SHADOW_MODE=sdk must not route ineligible role '{role}' to SDK"
        )

    def test_force_both_mode_ineligible_role_stays_cc(self):
        """SHADOW_MODE=both does not run the SDK side for ineligible roles."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="both",
            allow_fallback=False,
            role="executor",
            sdk_eligible=False,
        )
        assert result == "cc", (
            "SHADOW_MODE=both must not run SDK side for ineligible role 'executor'"
        )

    def test_force_both_mode_eligible_role_returns_both(self):
        """SHADOW_MODE=both returns 'both' for eligible roles."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="both",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=True,
        )
        assert result == "both", (
            "SHADOW_MODE=both should return 'both' for an eligible role"
        )

    def test_force_cc_mode_overrides_eligible_role(self):
        """SHADOW_MODE=cc forces CC regardless of role or flag."""
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="cc",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=True,
        )
        assert result == "cc"


# ---------------------------------------------------------------------------
# route() — integration with mocked SDK runner
# ---------------------------------------------------------------------------

class TestRouteSelectivePolicy:
    """route() routes to SDK only for eligible+flagged specs; everything else → cc."""

    def test_eligible_role_with_flag_routes_to_sdk(self):
        """docs-writer + sdk_eligible=True → SDK path invokes the runner."""
        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "discussion": None,
            "sdk_eligible": True,
        }

        mock_result = _make_run_result(verdict="done", agent_id="docs-writer-nod-1")

        async def fake_run(s, auto_routed=None):
            return mock_result

        mock_runner_instance = MagicMock()
        mock_runner_instance.run = fake_run

        mock_hook = MagicMock()
        mock_hook.pre_spawn.return_value = True

        mock_tracker = _make_tracker(100.0)

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=mock_runner_instance):
            result = route(spec)

        assert result["route"] == "sdk", f"Expected sdk, got {result['route']!r}"
        assert result["verdict"] == "done"

    def test_executor_with_flag_stays_on_cc(self):
        """executor + sdk_eligible=True still routes to CC (role gate holds)."""
        spec = {
            "role": "executor",
            "task_prompt": "implement something",
            "discussion": 1322,
            "sdk_eligible": True,
        }
        result = _route_with(spec, shadow_mode="default")
        assert result["route"] == "cc", f"executor must stay on CC, got {result['route']!r}"
        assert result["verdict"] == "routed_to_cc"

    def test_code_reviewer_with_flag_stays_on_cc(self):
        spec = {
            "role": "code-reviewer",
            "task_prompt": "review PR #55",
            "discussion": 1322,
            "sdk_eligible": True,
        }
        result = _route_with(spec, shadow_mode="default")
        assert result["route"] == "cc"

    def test_eligible_role_without_flag_stays_on_cc(self):
        """docs-writer without sdk_eligible stays on CC — no auto-spill."""
        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "sdk_eligible": False,
        }
        result = _route_with(spec, shadow_mode="default")
        assert result["route"] == "cc", (
            "eligible role without sdk_eligible flag must NOT route to SDK"
        )

    def test_default_spawn_no_sdk_eligible_field_routes_cc(self):
        """A typical spawn spec with no sdk_eligible key defaults to CC."""
        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "discussion": 1322,
            # sdk_eligible absent → defaults to False in route()
        }
        result = _route_with(spec, shadow_mode="default")
        assert result["route"] == "cc", (
            "Absent sdk_eligible must default to False → CC (no behavior change)"
        )


# ---------------------------------------------------------------------------
# SpawnSpec default — sdk_eligible defaults False
# ---------------------------------------------------------------------------

class TestSpawnSpecDefault:
    """SpawnSpec.sdk_eligible must default to False."""

    def test_spawn_spec_sdk_eligible_defaults_false(self):
        from backend.orchestrator.sdk_runner import SpawnSpec
        spec = SpawnSpec(
            role="executor",
            task_prompt="do work",
            tool_whitelist=["Read", "Bash"],
        )
        assert spec.sdk_eligible is False, (
            f"SpawnSpec.sdk_eligible must default to False, got {spec.sdk_eligible!r}"
        )

    def test_dict_to_spec_parses_sdk_eligible_true(self):
        """_dict_to_spec() correctly parses sdk_eligible=True from JSON dict."""
        import backend.orchestrator.dispatch as dispatch_mod

        spec_dict = {
            "role": "docs-writer",
            "task_prompt": "docs",
            "sdk_eligible": True,
        }
        spec = dispatch_mod._dict_to_spec(spec_dict)
        assert spec.sdk_eligible is True

    def test_dict_to_spec_defaults_sdk_eligible_false_when_absent(self):
        """_dict_to_spec() defaults sdk_eligible to False when key is absent."""
        import backend.orchestrator.dispatch as dispatch_mod

        spec_dict = {
            "role": "executor",
            "task_prompt": "implement",
        }
        spec = dispatch_mod._dict_to_spec(spec_dict)
        assert spec.sdk_eligible is False
