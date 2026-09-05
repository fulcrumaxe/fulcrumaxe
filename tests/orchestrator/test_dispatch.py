"""tests/orchestrator/test_dispatch.py — Unit tests for the route decision logic."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from backend.orchestrator.dispatch import (
    _should_use_sdk,
    _emit_credit_warning,
    CreditExhaustedError,
    route,
)


# ---------------------------------------------------------------------------
# Route decision under 4 credit states
# ---------------------------------------------------------------------------

class TestShouldUseSDK:
    """Validate route decisions across all relevant credit / shadow-mode states."""

    def test_alternate_odd_discussion_now_goes_cc(self):
        # D#1322: alternate mode is deprecated; selective opt-in policy applies.
        # An odd discussion number without sdk_eligible=True routes to CC, not SDK.
        assert _should_use_sdk(
            discussion=863,   # odd — under OLD alternate logic this was SDK; now CC
            remaining_usd=100.0,
            shadow_mode="alternate",
            allow_fallback=False,
        ) == "cc"

    def test_alternate_eligible_role_with_flag_goes_sdk(self):
        # Under alternate mode, eligible role + explicit flag still routes to SDK.
        assert _should_use_sdk(
            discussion=863,
            remaining_usd=100.0,
            shadow_mode="alternate",
            allow_fallback=False,
            role="docs-writer",
            sdk_eligible=True,
        ) == "sdk"

    def test_alternate_even_discussion_goes_cc(self):
        assert _should_use_sdk(
            discussion=862,   # even → CC
            remaining_usd=100.0,
            shadow_mode="alternate",
            allow_fallback=False,
        ) == "cc"

    def test_alternate_no_discussion_goes_cc(self):
        assert _should_use_sdk(
            discussion=None,
            remaining_usd=100.0,
            shadow_mode="alternate",
            allow_fallback=False,
        ) == "cc"

    def test_force_sdk_mode_eligible_role(self):
        # SHADOW_MODE=sdk routes eligible roles to SDK even without the sdk_eligible flag.
        # The role gate is unconditional — ineligible roles (no role) stay on CC.
        assert _should_use_sdk(
            discussion=862,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="docs-writer",  # eligible role — force-sdk bypasses the flag
        ) == "sdk"

    def test_force_sdk_mode_ineligible_role_stays_cc(self):
        # SHADOW_MODE=sdk does NOT bypass the role gate.
        # An empty role string is not in SDK_ELIGIBLE_ROLES → cc.
        assert _should_use_sdk(
            discussion=862,
            remaining_usd=100.0,
            shadow_mode="sdk",
            allow_fallback=False,
            role="executor",
        ) == "cc"

    def test_force_cc_mode(self):
        assert _should_use_sdk(
            discussion=863,   # odd, but force cc
            remaining_usd=100.0,
            shadow_mode="cc",
            allow_fallback=False,
        ) == "cc"

    def test_exhausted_with_fallback_goes_cc(self):
        assert _should_use_sdk(
            discussion=863,
            remaining_usd=0.0,
            shadow_mode="alternate",
            allow_fallback=True,
        ) == "cc"

    def test_exhausted_without_fallback_raises(self):
        with pytest.raises(CreditExhaustedError):
            _should_use_sdk(
                discussion=863,
                remaining_usd=0.0,
                shadow_mode="alternate",
                allow_fallback=False,
            )

    def test_negative_balance_raises_without_fallback(self):
        with pytest.raises(CreditExhaustedError):
            _should_use_sdk(
                discussion=863,
                remaining_usd=-5.0,
                shadow_mode="alternate",
                allow_fallback=False,
            )


# ---------------------------------------------------------------------------
# route() function integration tests (mocked SDK)
# ---------------------------------------------------------------------------

class TestRoute:
    """Tests for route() with mocked credit tracker and SDK runner."""

    def _make_spec(self, discussion=863, allow_fallback=False):
        return {
            "role": "code-reviewer",
            "task_prompt": "Review this code",
            "tool_whitelist": ["Read"],
            "env_allowlist": ["PATH", "HOME"],
            "discussion": discussion,
            "allow_subscription_fallback": allow_fallback,
        }

    def test_route_cc_returns_routed_to_cc(self, tmp_path):
        """Even-numbered Discussion should return route='cc'."""
        spec = self._make_spec(discussion=862)

        mock_tracker_instance = MagicMock()
        mock_tracker_instance.remaining_usd.return_value = 100.0
        mock_tracker_instance.soft_cap_breached.return_value = False
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)

        with patch("backend.orchestrator.dispatch.CreditTracker", mock_tracker_cls), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "alternate"):
            result = route(spec)

        assert result["route"] == "cc"
        assert result["verdict"] == "routed_to_cc"
        assert result["error"] is None

    def test_route_exhausted_without_fallback_returns_blocked(self, tmp_path):
        spec = self._make_spec(discussion=863, allow_fallback=False)

        mock_tracker_instance = MagicMock()
        mock_tracker_instance.remaining_usd.return_value = 0.0
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)

        with patch("backend.orchestrator.dispatch.CreditTracker", mock_tracker_cls):
            result = route(spec)

        assert result["route"] == "blocked"
        assert result["verdict"] == "fail"
        assert "exhausted" in result["error"].lower()

    def test_route_exhausted_with_fallback_goes_cc(self):
        spec = self._make_spec(discussion=863, allow_fallback=True)

        mock_tracker_instance = MagicMock()
        mock_tracker_instance.remaining_usd.return_value = 0.0
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)

        with patch("backend.orchestrator.dispatch.CreditTracker", mock_tracker_cls):
            result = route(spec)

        assert result["route"] == "cc"
        assert result["verdict"] == "routed_to_cc"

    def test_route_sdk_calls_runner(self):
        """Eligible role + sdk_eligible=True + available credit → SDK path invokes runner.

        D#1322: replaced old 'odd discussion → SDK' alternate routing with selective opt-in.
        Only eligible roles with sdk_eligible=True route to SDK.
        """
        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "tool_whitelist": ["Read"],
            "env_allowlist": ["PATH", "HOME"],
            "discussion": 863,
            "sdk_eligible": True,
        }

        mock_tracker_instance = MagicMock()
        mock_tracker_instance.remaining_usd.return_value = 100.0
        mock_tracker_instance.soft_cap_breached.return_value = False
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)

        mock_result = MagicMock()
        mock_result.agent_id = "docs-writer-863-999"
        mock_result.verdict = "done"
        mock_result.error = None
        mock_result.input_tokens = 1000
        mock_result.output_tokens = 500

        async def fake_run(s, auto_routed=None):
            return mock_result

        mock_runner_instance = MagicMock()
        mock_runner_instance.run = fake_run
        mock_runner_cls = MagicMock(return_value=mock_runner_instance)

        mock_hook_instance = MagicMock()
        mock_hook_instance.pre_spawn.return_value = True
        mock_hook_cls = MagicMock(return_value=mock_hook_instance)

        with patch("backend.orchestrator.dispatch.CreditTracker", mock_tracker_cls), \
             patch("backend.orchestrator.dispatch.SDKRunner", mock_runner_cls), \
             patch("backend.orchestrator.dispatch.HookRunner", mock_hook_cls), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=mock_runner_instance):
            result = route(spec)

        assert result["route"] == "sdk"
        assert result["verdict"] == "done"


# ---------------------------------------------------------------------------
# _emit_credit_warning — team-log gate tests
# ---------------------------------------------------------------------------

class TestEmitCreditWarningGate:
    """The team-log subprocess must NOT fire in tests or when gate is off."""

    def test_no_team_log_post_when_gate_unset(self, monkeypatch):
        """ROUTE_VIA_DISPATCHER not set → subprocess.run must not be called."""
        monkeypatch.delenv("ROUTE_VIA_DISPATCHER", raising=False)
        # PYTEST_CURRENT_TEST is always set inside pytest, but be explicit
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/orchestrator/test_dispatch.py::fake")

        with patch("backend.orchestrator.dispatch.subprocess.run") as mock_run:
            _emit_credit_warning(100.0)

        mock_run.assert_not_called()

    def test_no_team_log_post_when_pytest_current_test_set(self, monkeypatch):
        """Even with ROUTE_VIA_DISPATCHER=1, do not post when PYTEST_CURRENT_TEST is set."""
        monkeypatch.setenv("ROUTE_VIA_DISPATCHER", "1")
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/orchestrator/test_dispatch.py::fake")

        with patch("backend.orchestrator.dispatch.subprocess.run") as mock_run:
            _emit_credit_warning(100.0)

        mock_run.assert_not_called()

    def test_team_log_post_when_genuinely_live(self, monkeypatch, tmp_path):
        """ROUTE_VIA_DISPATCHER=1 and no PYTEST_CURRENT_TEST → subprocess.run is called."""
        monkeypatch.setenv("ROUTE_VIA_DISPATCHER", "1")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        # Create a fake rotate-team-log.sh so rotate_script.exists() returns True
        fake_rotate = tmp_path / "rotate-team-log.sh"
        fake_rotate.write_text("#!/bin/bash\nexit 0\n")

        # Patch the Path used inside _emit_credit_warning so that the
        # computed rotate_script points to our fake file.
        fake_repo_root = MagicMock()
        fake_repo_root.__truediv__ = MagicMock(return_value=MagicMock())
        fake_rotate_mock = MagicMock()
        fake_rotate_mock.exists.return_value = True
        fake_rotate_mock.__str__ = lambda self: str(fake_rotate)
        fake_repo_root.__truediv__.return_value.__truediv__ = MagicMock(return_value=fake_rotate_mock)

        with patch("backend.orchestrator.dispatch.subprocess.run") as mock_run, \
             patch("backend.orchestrator.dispatch.Path") as mock_path_cls:
            mock_path_instance = MagicMock()
            mock_path_instance.resolve.return_value.parent.parent.parent = fake_repo_root
            mock_path_cls.return_value = mock_path_instance

            _emit_credit_warning(100.0)

        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "rotate-team-log.sh" in str(cmd_args)
        assert "comment" in cmd_args
