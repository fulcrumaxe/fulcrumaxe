"""Tests for detect_sdk_credential() in agent_sdk_runner.py.

Covers:
- oauth_token env var set → "oauth_token"
- api_key env var set → "api_key"
- only login file present (no env vars) → "login"
- nothing → None
- precedence: oauth_token > api_key > login
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# We import only detect_sdk_credential — not the full module — to avoid
# triggering the claude_agent_sdk ImportError guard in agent_sdk_runner.py.
# We patch the module into sys.modules with just the function we need.


def _get_detector(monkeypatch, tmp_path):
    """Return detect_sdk_credential with _CREDENTIALS_FILE pointed at tmp_path."""
    # Patch away the heavy imports so the module loads in the test environment.
    fake_sdk_module = type(sys)("claude_agent_sdk")
    fake_sdk_module.query = None
    fake_types = type(sys)("claude_agent_sdk.types")
    fake_types.AssistantMessage = object
    fake_types.ClaudeAgentOptions = object
    fake_types.ResultMessage = object
    fake_types.TextBlock = object
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk_module)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types)

    # Also stub out the heavy internal deps that agent_sdk_runner imports
    for mod_name in [
        "backend.orchestrator.sdk_runner",
        "backend.orchestrator.tool_proxy",
        "backend.orchestrator.mcp_tools",
        "backend.orchestrator.redact",
    ]:
        if mod_name not in sys.modules:
            stub = type(sys)("stub")
            # Provide minimal attributes that agent_sdk_runner imports
            stub.RunResult = object
            stub.SpawnSpec = object
            stub._SYSTEM_PROMPT_TEMPLATE = ""
            stub._extract_verdict = lambda t: "done"
            stub._load_role_card = lambda p: ""
            stub._now_iso = lambda: ""
            stub._prompt_sha256 = lambda s: ""
            stub._write_agent_run = lambda r: None
            stub._write_audit = lambda d: None
            stub.build_user_message = lambda s: ""
            stub.build_env = lambda a: {}
            stub.build_mcp_server = lambda **kw: None
            stub.redact = lambda s: s
            monkeypatch.setitem(sys.modules, mod_name, stub)

    # Remove any cached version so we get a fresh import with our patches
    for key in list(sys.modules.keys()):
        if "agent_sdk_runner" in key:
            monkeypatch.delitem(sys.modules, key, raising=False)

    from backend.orchestrator.agent_sdk_runner import detect_sdk_credential
    return detect_sdk_credential


class TestDetectSdkCredential:
    """Unit tests for the shared credential-kind detector."""

    def test_oauth_token_env_returns_oauth_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc123")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        detect = _get_detector(monkeypatch, tmp_path)
        # Pass a non-existent file so login branch is not triggered
        result = detect(credentials_path=str(tmp_path / "no-file.json"))
        assert result == "oauth_token"

    def test_api_key_env_returns_api_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        detect = _get_detector(monkeypatch, tmp_path)
        result = detect(credentials_path=str(tmp_path / "no-file.json"))
        assert result == "api_key"

    def test_only_login_file_returns_login(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        creds = tmp_path / ".credentials.json"
        creds.write_text('{"claudeAiOauth": {"accessToken": "redacted"}}')
        detect = _get_detector(monkeypatch, tmp_path)
        result = detect(credentials_path=str(creds))
        assert result == "login"

    def test_nothing_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        detect = _get_detector(monkeypatch, tmp_path)
        result = detect(credentials_path=str(tmp_path / "no-file.json"))
        assert result is None

    def test_oauth_token_beats_api_key(self, monkeypatch, tmp_path):
        """CLAUDE_CODE_OAUTH_TOKEN takes priority over ANTHROPIC_API_KEY."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        detect = _get_detector(monkeypatch, tmp_path)
        result = detect(credentials_path=str(tmp_path / "no-file.json"))
        assert result == "oauth_token"

    def test_oauth_token_beats_login(self, monkeypatch, tmp_path):
        """CLAUDE_CODE_OAUTH_TOKEN takes priority over login file."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        creds = tmp_path / ".credentials.json"
        creds.write_text("{}")
        detect = _get_detector(monkeypatch, tmp_path)
        result = detect(credentials_path=str(creds))
        assert result == "oauth_token"

    def test_api_key_beats_login(self, monkeypatch, tmp_path):
        """ANTHROPIC_API_KEY takes priority over login file."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        creds = tmp_path / ".credentials.json"
        creds.write_text("{}")
        detect = _get_detector(monkeypatch, tmp_path)
        result = detect(credentials_path=str(creds))
        assert result == "api_key"
