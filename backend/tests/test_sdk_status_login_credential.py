"""Tests for sdk_status._backend_would_select() login-credential support.

Covers:
- only login file → would_select=subscription
- claude_login field present in output
- no secret values leaked
- state precedence: oauth_token > api_key > login
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.sdk_status import _backend_would_select, _autodetect_backend


_FAKE_OAUTH = "oauth-secret-abc123"
_FAKE_APIKEY = "sk-ant-apikey-secret-xyz"


class TestBackendWouldSelectWithLogin:
    """_backend_would_select() handles the login credential path correctly."""

    def test_only_login_selects_subscription(self, monkeypatch, tmp_path):
        """The core D#1340 case: no env vars, only credentials.json → subscription."""
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds = tmp_path / ".credentials.json"
        creds.write_text('{"claudeAiOauth": {"accessToken": "redacted"}}')

        bs = _backend_would_select(credentials_path=str(creds))

        assert bs["would_select"] == "subscription", (
            f"Expected 'subscription' with login-only, got {bs['would_select']!r}"
        )
        assert "login" in bs["reason"].lower()

    def test_claude_login_field_present_in_output(self, monkeypatch, tmp_path):
        """Output always includes claude_login presence field."""
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # With login present
        creds = tmp_path / ".credentials.json"
        creds.write_text("{}")
        bs_present = _backend_would_select(credentials_path=str(creds))
        assert "claude_login" in bs_present
        assert bs_present["claude_login"] == "present"

        # Without login
        bs_absent = _backend_would_select(credentials_path=str(tmp_path / "no-file.json"))
        assert "claude_login" in bs_absent
        assert bs_absent["claude_login"] == "absent"

    def test_no_secret_values_with_login(self, monkeypatch, tmp_path):
        """Login credential path never leaks token values."""
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds = tmp_path / ".credentials.json"
        creds.write_text('{"claudeAiOauth": {"accessToken": "super-secret-token-value"}}')

        bs = _backend_would_select(credentials_path=str(creds))
        serialised = json.dumps(bs)

        assert "super-secret-token-value" not in serialised
        assert "present" in serialised

    def test_oauth_token_beats_login(self, monkeypatch, tmp_path):
        """CLAUDE_CODE_OAUTH_TOKEN wins over login file."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_OAUTH)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds = tmp_path / ".credentials.json"
        creds.write_text("{}")

        bs = _backend_would_select(credentials_path=str(creds))

        assert bs["would_select"] == "subscription"
        assert bs["CLAUDE_CODE_OAUTH_TOKEN"] == "present"
        # The oauth token value must not appear
        assert _FAKE_OAUTH not in json.dumps(bs)

    def test_api_key_beats_login(self, monkeypatch, tmp_path):
        """ANTHROPIC_API_KEY takes precedence over login when oauth token is absent."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_APIKEY)

        creds = tmp_path / ".credentials.json"
        creds.write_text("{}")

        bs = _backend_would_select(credentials_path=str(creds))

        assert bs["would_select"] == "apikey"
        assert bs["ANTHROPIC_API_KEY"] == "present"

    def test_no_credential_at_all_returns_none(self, monkeypatch, tmp_path):
        """No env vars, no file → would_select='none'."""
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        bs = _backend_would_select(credentials_path=str(tmp_path / "no-file.json"))

        assert bs["would_select"] == "none"
        assert bs["claude_login"] == "absent"

    def test_sdk_backend_override_ignores_login(self, monkeypatch, tmp_path):
        """Explicit SDK_BACKEND override wins regardless of login presence."""
        monkeypatch.setenv("SDK_BACKEND", "apikey")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds = tmp_path / ".credentials.json"
        creds.write_text("{}")

        bs = _backend_would_select(credentials_path=str(creds))

        assert bs["would_select"] == "apikey"
        assert "SDK_BACKEND" in bs["reason"]


class TestAutodetectBackendWithLogin:
    """_autodetect_backend() handles the has_login flag correctly."""

    def test_login_only_returns_subscription(self):
        result = _autodetect_backend(has_oauth=False, has_apikey=False, has_login=True)
        assert result == "subscription"

    def test_oauth_beats_login(self):
        result = _autodetect_backend(has_oauth=True, has_apikey=False, has_login=True)
        assert result == "subscription"  # oauth path

    def test_apikey_beats_login(self):
        result = _autodetect_backend(has_oauth=False, has_apikey=True, has_login=True)
        assert result == "apikey"

    def test_nothing_returns_none(self):
        result = _autodetect_backend(has_oauth=False, has_apikey=False, has_login=False)
        assert result == "none"

    def test_default_has_login_false(self):
        """has_login defaults to False — existing callers without the arg still work."""
        result = _autodetect_backend(has_oauth=False, has_apikey=False)
        assert result == "none"
