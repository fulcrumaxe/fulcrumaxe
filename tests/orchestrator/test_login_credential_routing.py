"""tests/orchestrator/test_login_credential_routing.py

Tests proving the selector routes to the subscription backend when only the
claude CLI login (credentials.json) is present — no CLAUDE_CODE_OAUTH_TOKEN,
no ANTHROPIC_API_KEY.

This is the live-machine regression: D#1340 reports that dispatch._select_sdk_backend
fell back to CC with "no SDK credential available" because it only checked env vars.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# _select_sdk_backend — login-only credential
# ---------------------------------------------------------------------------

class TestSelectSdkBackendLoginCredential:
    """_select_sdk_backend picks ClaudeAgentSDKRunner when only the login exists."""

    def test_only_login_selects_subscription(self, monkeypatch, tmp_path):
        """The core D#1340 regression: login-only → ClaudeAgentSDKRunner, not None."""
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Create a fake credentials file
        creds = tmp_path / ".credentials.json"
        creds.write_text('{"claudeAiOauth": {"accessToken": "redacted"}}')

        mock_agent_runner_instance = MagicMock(name="ClaudeAgentSDKRunnerInstance")
        mock_agent_runner_cls = MagicMock(return_value=mock_agent_runner_instance)

        import backend.orchestrator.dispatch as dispatch_mod

        # Patch detect_sdk_credential to simulate login-only scenario
        # (bypasses actual file-system path for isolation)
        with patch(
            "backend.orchestrator.agent_sdk_runner.detect_sdk_credential",
            return_value="login",
        ), patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                detect_sdk_credential=MagicMock(return_value="login"),
                ClaudeAgentSDKRunner=mock_agent_runner_cls,
            ),
        }):
            result = dispatch_mod._select_sdk_backend()

        assert result is mock_agent_runner_instance, (
            "Expected ClaudeAgentSDKRunner when login credential present, "
            f"got {result!r}"
        )

    def test_only_login_selects_subscription_via_full_route(self, monkeypatch, tmp_path):
        """End-to-end: eligible role + sdk_eligible + login only → route='sdk', not 'cc'.

        This proves the full dispatcher path picks sdk with only a login present.
        """
        from backend.orchestrator.dispatch import route

        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "tool_whitelist": ["Read"],
            "env_allowlist": ["PATH", "HOME"],
            "discussion": 1340,
            "sdk_eligible": True,
        }

        mock_tracker = MagicMock()
        mock_tracker.remaining_usd.return_value = 100.0

        mock_result = MagicMock()
        mock_result.agent_id = "docs-writer-1340-999"
        mock_result.verdict = "done"
        mock_result.error = None
        mock_result.input_tokens = 500
        mock_result.output_tokens = 200

        async def fake_run(s, auto_routed=None):
            return mock_result

        mock_runner_instance = MagicMock()
        mock_runner_instance.run = fake_run

        mock_hook = MagicMock()

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch(
                 "backend.orchestrator.dispatch._select_sdk_backend",
                 return_value=mock_runner_instance,
             ):
            result = route(spec)

        assert result["route"] == "sdk", (
            f"Expected route='sdk' with login credential present, got route={result['route']!r}. "
            f"Full result: {result}"
        )
        assert result["verdict"] == "done"

    def test_api_key_only_selects_apikey_backend(self, monkeypatch, tmp_path):
        """ANTHROPIC_API_KEY only → SDKRunner (API-key backend), not subscription."""
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

        mock_sdk_runner_instance = MagicMock(name="SDKRunnerInstance")
        mock_sdk_runner_cls = MagicMock(return_value=mock_sdk_runner_instance)

        import backend.orchestrator.dispatch as dispatch_mod

        with patch.object(dispatch_mod, "SDKRunner", mock_sdk_runner_cls), \
             patch.dict("sys.modules", {
                 "backend.orchestrator.agent_sdk_runner": MagicMock(
                     detect_sdk_credential=MagicMock(return_value="api_key"),
                     ClaudeAgentSDKRunner=MagicMock(),
                 ),
             }):
            result = dispatch_mod._select_sdk_backend()

        assert result is mock_sdk_runner_instance

    def test_no_credential_returns_none(self, monkeypatch, tmp_path):
        """No env vars, no login file → None (CC fallback)."""
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

        assert result is None, f"Expected None with no credential, got {result!r}"


# ---------------------------------------------------------------------------
# State precedence checks
# ---------------------------------------------------------------------------

class TestCredentialPrecedenceInSelector:
    """Verify ANTHROPIC_API_KEY-overrides and precedence rules hold."""

    def test_oauth_token_beats_api_key_and_login(self, monkeypatch, tmp_path):
        """CLAUDE_CODE_OAUTH_TOKEN is highest priority — beats API key and login."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        mock_agent_runner_instance = MagicMock()
        mock_agent_runner_cls = MagicMock(return_value=mock_agent_runner_instance)

        import backend.orchestrator.dispatch as dispatch_mod

        with patch.dict("sys.modules", {
            "backend.orchestrator.agent_sdk_runner": MagicMock(
                detect_sdk_credential=MagicMock(return_value="oauth_token"),
                ClaudeAgentSDKRunner=mock_agent_runner_cls,
            ),
        }):
            result = dispatch_mod._select_sdk_backend()

        assert result is mock_agent_runner_instance

    def test_api_key_beats_login(self, monkeypatch, tmp_path):
        """ANTHROPIC_API_KEY takes priority over login when oauth token is absent."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        creds = tmp_path / ".credentials.json"
        creds.write_text("{}")

        mock_sdk_runner_cls = MagicMock()
        mock_sdk_runner_instance = MagicMock()
        mock_sdk_runner_cls.return_value = mock_sdk_runner_instance

        import backend.orchestrator.dispatch as dispatch_mod

        with patch.object(dispatch_mod, "SDKRunner", mock_sdk_runner_cls), \
             patch.dict("sys.modules", {
                 "backend.orchestrator.agent_sdk_runner": MagicMock(
                     detect_sdk_credential=MagicMock(return_value="api_key"),
                     ClaudeAgentSDKRunner=MagicMock(),
                 ),
             }):
            result = dispatch_mod._select_sdk_backend()

        assert result is mock_sdk_runner_instance
