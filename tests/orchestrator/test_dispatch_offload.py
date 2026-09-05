"""tests/orchestrator/test_dispatch_offload.py — Orchestrator-level routing tests for D#1322.

Verifies the selective offload routing policy at the spawn-agent.sh / dispatch integration:
  - sdk_eligible+eligible-role → sdk
  - executor/reviewer+flag → cc
  - eligible-role no-flag → cc
  - default spawn unchanged (cc)
  - SHADOW_MODE=cc still forces CC (safe test baseline)
  - sdk_eligible field in SpawnSpec JSON is accepted by dispatch CLI

No real Anthropic API calls.  CreditTracker is mocked to $100.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Helper: invoke dispatch as a subprocess (CLI contract test)
# ---------------------------------------------------------------------------

def _invoke_dispatch(spec_dict: dict, shadow_mode: str = "cc") -> dict:
    """Run dispatch.main() via subprocess with mocked CreditTracker.

    Uses SHADOW_MODE=cc as the safe default so no real SDK calls happen.
    To test selective routing, pass shadow_mode='default'.
    """
    wrapper = (
        "import sys, unittest.mock, os\n"
        "mock_tracker = unittest.mock.MagicMock()\n"
        "mock_tracker.remaining_usd.return_value = 100.0\n"
        "with unittest.mock.patch('backend.orchestrator.dispatch.CreditTracker', return_value=mock_tracker):\n"
        "    import backend.orchestrator.dispatch as d\n"
        "    import json\n"
        "    raw = sys.stdin.read()\n"
        "    result = d.route(json.loads(raw))\n"
        "    print(json.dumps(result))\n"
    )
    env = os.environ.copy()
    env["SHADOW_MODE"] = shadow_mode
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", wrapper],
        input=json.dumps(spec_dict),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert result.returncode == 0, f"dispatch subprocess failed: {result.stderr}"
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# Routing truth table — CLI round-trip
# ---------------------------------------------------------------------------

class TestOffloadRoutingCLI:
    """Routing truth table verified via the CLI entry point."""

    def test_eligible_role_with_flag_routes_sdk(self):
        """docs-writer + sdk_eligible=True → sdk (with mocked runner)."""
        # Use the unit-level test for this — CLI with SDK path needs a real runner mock.
        # Instead verify _should_use_sdk directly to keep this fast and reliable.
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=True,
        )
        assert result == "sdk"

    def test_executor_with_flag_stays_cc(self):
        """executor + sdk_eligible=True → cc (CLI path)."""
        spec = {
            "role": "executor",
            "task_prompt": "implement D#1322",
            "discussion": 1322,
            "sdk_eligible": True,
        }
        result = _invoke_dispatch(spec, shadow_mode="cc")
        assert result["route"] == "cc"
        assert result["verdict"] == "routed_to_cc"

    def test_eligible_role_without_flag_stays_cc(self):
        """docs-writer without sdk_eligible → cc (no auto-spill)."""
        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "discussion": None,
            "sdk_eligible": False,
        }
        result = _invoke_dispatch(spec, shadow_mode="cc")
        assert result["route"] == "cc"

    def test_default_spawn_no_flag_stays_cc(self):
        """A typical spawn with no sdk_eligible key stays on CC — zero behavior change."""
        spec = {
            "role": "quality-sweep",
            "task_prompt": "sweep code quality",
            "discussion": 1322,
            # sdk_eligible absent → defaults to False
        }
        result = _invoke_dispatch(spec, shadow_mode="cc")
        assert result["route"] == "cc"
        assert result["error"] is None

    def test_sdk_eligible_field_accepted_by_dispatch(self):
        """The dispatch CLI accepts sdk_eligible in the spec without error."""
        spec = {
            "role": "run-analyst",
            "task_prompt": "analyse runs",
            "discussion": None,
            "sdk_eligible": True,
        }
        # SHADOW_MODE=cc forces CC even though the spec is eligible
        result = _invoke_dispatch(spec, shadow_mode="cc")
        assert result["route"] == "cc"

    def test_security_reviewer_with_flag_stays_cc_under_default_mode(self):
        """security-reviewer+sdk_eligible=True stays CC under the selective policy."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="security-reviewer",
            sdk_eligible=True,
        )
        assert result == "cc"


# ---------------------------------------------------------------------------
# Force-mode role gate — the key invariant from PR #1323 review
# ---------------------------------------------------------------------------

class TestForceModeRoleGate:
    """SHADOW_MODE=sdk/both must not bypass the role gate.

    These tests prove the hard invariant: executors, reviewers, and control-plane
    roles NEVER route to SDK in ANY mode, including force modes.
    """

    def test_force_sdk_executor_routes_cc(self):
        """SHADOW_MODE=sdk + role=executor → cc (role gate is unconditional)."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="executor",
            sdk_eligible=False,
        )
        assert result == "cc", "SHADOW_MODE=sdk must not send executor to SDK"

    def test_force_sdk_code_reviewer_routes_cc(self):
        """SHADOW_MODE=sdk + role=code-reviewer → cc."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="code-reviewer",
            sdk_eligible=True,
        )
        assert result == "cc", "SHADOW_MODE=sdk must not send code-reviewer to SDK"

    def test_force_sdk_security_reviewer_routes_cc(self):
        """SHADOW_MODE=sdk + role=security-reviewer → cc."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="security-reviewer",
            sdk_eligible=True,
        )
        assert result == "cc", "SHADOW_MODE=sdk must not send security-reviewer to SDK"

    def test_force_sdk_acceptance_tester_routes_cc(self):
        """SHADOW_MODE=sdk + role=acceptance-tester → cc."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="acceptance-tester",
            sdk_eligible=True,
        )
        assert result == "cc", "SHADOW_MODE=sdk must not send acceptance-tester to SDK"

    def test_force_sdk_unknown_role_routes_cc(self):
        """SHADOW_MODE=sdk + unknown/control role → cc."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="unknown-control-role",
            sdk_eligible=True,
        )
        assert result == "cc", "SHADOW_MODE=sdk must not send unknown roles to SDK"

    def test_force_sdk_eligible_role_bypasses_flag(self):
        """SHADOW_MODE=sdk + eligible role → sdk even without sdk_eligible flag."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=False,  # flag off — force-sdk bypasses for eligible roles
        )
        assert result == "sdk", (
            "SHADOW_MODE=sdk should bypass the sdk_eligible flag for eligible role docs-writer"
        )

    def test_force_both_executor_routes_cc(self):
        """SHADOW_MODE=both + role=executor → cc (SDK side not run for ineligible roles)."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="both",
            allow_fallback=False,
            role="executor",
            sdk_eligible=False,
        )
        assert result == "cc", "SHADOW_MODE=both must not run SDK side for executor"

    def test_default_mode_unchanged_eligible_with_flag_routes_sdk(self):
        """Default mode: eligible+flag → sdk (regression guard)."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=True,
        )
        assert result == "sdk"

    def test_default_mode_eligible_no_flag_routes_cc(self):
        """Default mode: eligible role without flag → cc (no auto-spill; regression guard)."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=False,
        )
        assert result == "cc"

    def test_default_mode_ineligible_routes_cc(self):
        """Default mode: ineligible role → cc regardless of flag (regression guard)."""
        from backend.orchestrator.dispatch import _should_use_sdk
        result = _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="default",
            allow_fallback=False,
            role="executor",
            sdk_eligible=True,
        )
        assert result == "cc"


# ---------------------------------------------------------------------------
# Spawn-agent.sh integration — sdk_eligible field in generated spec
# ---------------------------------------------------------------------------

class TestSpawnAgentSDKLaneFlag:
    """Verify spawn-agent.sh includes sdk_eligible in the SpawnSpec JSON.

    These tests read the script source to verify the contract, rather than
    executing the full shell pipeline (which requires the full runtime env).
    """

    def test_sdk_lane_flag_exists_in_wrapper(self):
        """spawn-agent.sh has --sdk-lane argument."""
        wrapper = (REPO_ROOT / "scripts" / "spawn-agent.sh").read_text()
        assert "--sdk-lane" in wrapper, (
            "--sdk-lane flag is missing from spawn-agent.sh"
        )

    def test_sdk_eligible_key_in_dispatch_spec(self):
        """spawn-agent.sh sets sdk_eligible in the SpawnSpec JSON it sends to dispatch."""
        wrapper = (REPO_ROOT / "scripts" / "spawn-agent.sh").read_text()
        assert "'sdk_eligible'" in wrapper, (
            "sdk_eligible key not found in spawn-agent.sh SpawnSpec JSON builder"
        )

    def test_sdk_lane_env_var_honoured(self):
        """SDK_LANE env var is used as the initial value for SDK_LANE in the script."""
        wrapper = (REPO_ROOT / "scripts" / "spawn-agent.sh").read_text()
        assert 'SDK_LANE="${SDK_LANE:-0}"' in wrapper, (
            "SDK_LANE env fallback not found — SDK_LANE=1 env must work as an alternative"
        )

    def test_default_sdk_eligible_is_false(self):
        """The default value of SDK_LANE is 0 (off), meaning sdk_eligible=False by default."""
        wrapper = (REPO_ROOT / "scripts" / "spawn-agent.sh").read_text()
        # Default must be 0 so existing spawn calls are byte-identical
        assert 'SDK_LANE="${SDK_LANE:-0}"' in wrapper or "SDK_LANE=0" in wrapper, (
            "SDK_LANE must default to 0 (off) to preserve byte-identical behavior"
        )


# ---------------------------------------------------------------------------
# Existing dispatch tests still pass (regression guard)
# ---------------------------------------------------------------------------

class TestExistingRoutingUnchanged:
    """Regression guard: existing SHADOW_MODE=cc and blocked-credit paths still work."""

    def test_shadow_mode_cc_always_routes_cc(self):
        spec = {
            "role": "executor",
            "task_prompt": "test",
            "discussion": 1302,
        }
        result = _invoke_dispatch(spec, shadow_mode="cc")
        assert result["route"] == "cc"

    def test_credit_blocked_returns_blocked(self):
        """Credit-exhausted path still returns route==blocked under new policy."""
        import backend.orchestrator.dispatch as dispatch_mod

        mock_tracker = MagicMock()
        mock_tracker.remaining_usd.return_value = 0.0

        spec = {
            "role": "executor",
            "task_prompt": "implement",
            "discussion": 1322,
        }

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker):
            result = dispatch_mod.route(spec)

        assert result["route"] == "blocked"
        assert result["verdict"] == "fail"
