"""Tests for the backend selector in backend/orchestrator/dispatch.py.

Covers _select_sdk_backend() directly and _run_sdk() fallback behaviour.
All tests mock both runner classes — no real SDK calls, no real credentials.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_result(verdict: str = "done", agent_id: str = "agent-1"):
    """Return a minimal RunResult-like object for mocking."""
    r = MagicMock()
    r.verdict = verdict
    r.agent_id = agent_id
    r.error = None
    r.input_tokens = 100
    r.output_tokens = 50
    return r


# ---------------------------------------------------------------------------
# _select_sdk_backend — explicit SDK_BACKEND override
# ---------------------------------------------------------------------------

class TestSelectSdkBackendExplicitOverride:
    """SDK_BACKEND env var takes highest priority."""

    def test_subscription_override_returns_agent_sdk_runner(self, monkeypatch):
        monkeypatch.setenv("SDK_BACKEND", "subscription")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_agent_runner = MagicMock(name="ClaudeAgentSDKRunner")
        mock_agent_runner_instance = MagicMock()
        mock_agent_runner.return_value = mock_agent_runner_instance

        with patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                ClaudeAgentSDKRunner=mock_agent_runner
            ),
        }):
            import backend.orchestrator.dispatch as dispatch_mod

            result = dispatch_mod._select_sdk_backend()
            mock_agent_runner.assert_called_once()
            assert result is mock_agent_runner_instance

    def test_agent_sdk_alias_returns_agent_sdk_runner(self, monkeypatch):
        monkeypatch.setenv("SDK_BACKEND", "agent_sdk")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_agent_runner = MagicMock(name="ClaudeAgentSDKRunner")

        with patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                ClaudeAgentSDKRunner=mock_agent_runner
            ),
        }):
            import backend.orchestrator.dispatch as dispatch_mod

            dispatch_mod._select_sdk_backend()
            mock_agent_runner.assert_called_once()

    def test_apikey_override_returns_sdk_runner(self, monkeypatch):
        monkeypatch.setenv("SDK_BACKEND", "apikey")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_sdk_runner = MagicMock(name="SDKRunner")
        mock_sdk_runner_instance = MagicMock()
        mock_sdk_runner.return_value = mock_sdk_runner_instance

        import backend.orchestrator.dispatch as dispatch_mod

        with patch.object(dispatch_mod, "SDKRunner", mock_sdk_runner):
            result = dispatch_mod._select_sdk_backend()
            mock_sdk_runner.assert_called_once()
            assert result is mock_sdk_runner_instance

    def test_anthropic_alias_returns_sdk_runner(self, monkeypatch):
        monkeypatch.setenv("SDK_BACKEND", "anthropic")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_sdk_runner = MagicMock(name="SDKRunner")

        import backend.orchestrator.dispatch as dispatch_mod

        with patch.object(dispatch_mod, "SDKRunner", mock_sdk_runner):
            dispatch_mod._select_sdk_backend()
            mock_sdk_runner.assert_called_once()


# ---------------------------------------------------------------------------
# _select_sdk_backend — auto-detect from credentials
# ---------------------------------------------------------------------------

class TestSelectSdkBackendAutoDetect:
    """Auto-detect picks the right runner from the credential environment."""

    def test_only_oauth_token_selects_subscription(self, monkeypatch):
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_agent_runner = MagicMock(name="ClaudeAgentSDKRunner")

        with patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                detect_sdk_credential=MagicMock(return_value="oauth_token"),
                ClaudeAgentSDKRunner=mock_agent_runner,
            ),
        }):
            import backend.orchestrator.dispatch as dispatch_mod

            dispatch_mod._select_sdk_backend()
            mock_agent_runner.assert_called_once()

    def test_only_api_key_selects_sdk_runner(self, monkeypatch):
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        mock_sdk_runner = MagicMock(name="SDKRunner")

        import backend.orchestrator.dispatch as dispatch_mod

        with patch.object(dispatch_mod, "SDKRunner", mock_sdk_runner):
            dispatch_mod._select_sdk_backend()
            mock_sdk_runner.assert_called_once()

    def test_both_creds_prefers_subscription(self, monkeypatch):
        """When both tokens are set, subscription wins (documented preference)."""
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        mock_agent_runner = MagicMock(name="ClaudeAgentSDKRunner")

        with patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                detect_sdk_credential=MagicMock(return_value="oauth_token"),
                ClaudeAgentSDKRunner=mock_agent_runner,
            ),
        }):
            import backend.orchestrator.dispatch as dispatch_mod

            dispatch_mod._select_sdk_backend()
            mock_agent_runner.assert_called_once()

    def test_neither_credential_returns_none(self, monkeypatch):
        """No env credentials and no login file → returns None (triggers CC fallback)."""
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        import backend.orchestrator.dispatch as dispatch_mod

        with patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                detect_sdk_credential=MagicMock(return_value=None),
                ClaudeAgentSDKRunner=MagicMock(),
            ),
        }):
            result = dispatch_mod._select_sdk_backend()

        assert result is None


# ---------------------------------------------------------------------------
# _run_sdk — no-credential CC fallback
# ---------------------------------------------------------------------------

class TestRunSdkNoCreditFallback:
    """When _select_sdk_backend returns None, _run_sdk falls back to CC."""

    def test_no_credential_falls_back_to_cc_no_crash(self, monkeypatch):
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        import backend.orchestrator.dispatch as dispatch_mod

        mock_tracker = MagicMock()
        mock_hook = MagicMock()
        mock_hook.pre_spawn = MagicMock()

        with patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=None):

            spec_dict = {"role": "executor", "discussion": 1308}
            result = dispatch_mod._run_sdk(spec_dict, mock_tracker)

        assert result["route"] == "cc"
        assert result["verdict"] == "routed_to_cc"
        assert result["error"] is not None  # describes why
        assert result["run_id"] is not None


# ---------------------------------------------------------------------------
# Telemetry marker — selected-backend log line
# ---------------------------------------------------------------------------

class TestBackendSelectorTelemetry:
    """The selector emits a logger.info line indicating which backend was chosen."""

    def test_subscription_override_emits_telemetry(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("SDK_BACKEND", "subscription")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_agent_runner = MagicMock(name="ClaudeAgentSDKRunner")

        with patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                ClaudeAgentSDKRunner=mock_agent_runner
            ),
        }), caplog.at_level(logging.INFO, logger="backend.orchestrator.dispatch"):
            import backend.orchestrator.dispatch as dispatch_mod

            dispatch_mod._select_sdk_backend()

        assert any("[backend-selector]" in r.message for r in caplog.records), \
            f"Expected [backend-selector] marker in logs. Got: {[r.message for r in caplog.records]}"

    def test_apikey_override_emits_telemetry(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("SDK_BACKEND", "apikey")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_sdk_runner = MagicMock(name="SDKRunner")

        import backend.orchestrator.dispatch as dispatch_mod

        with patch.object(dispatch_mod, "SDKRunner", mock_sdk_runner), \
             caplog.at_level(logging.INFO, logger="backend.orchestrator.dispatch"):
            dispatch_mod._select_sdk_backend()

        assert any("[backend-selector]" in r.message for r in caplog.records)

    def test_auto_detect_oauth_emits_telemetry(self, monkeypatch, caplog):
        import logging
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-xyz")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_agent_runner = MagicMock(name="ClaudeAgentSDKRunner")

        with patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                ClaudeAgentSDKRunner=mock_agent_runner
            ),
        }), caplog.at_level(logging.INFO, logger="backend.orchestrator.dispatch"):
            import backend.orchestrator.dispatch as dispatch_mod

            dispatch_mod._select_sdk_backend()

        assert any("[backend-selector]" in r.message for r in caplog.records)

    def test_no_credential_emits_telemetry(self, monkeypatch, caplog):
        import logging
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with caplog.at_level(logging.INFO, logger="backend.orchestrator.dispatch"):
            import backend.orchestrator.dispatch as dispatch_mod

            dispatch_mod._select_sdk_backend()

        assert any("[backend-selector]" in r.message for r in caplog.records)
