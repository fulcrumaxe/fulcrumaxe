"""Tests for backend/orchestrator/sdk_status.py.

Covers:
- Readiness reflects ROUTE_VIA_DISPATCHER / SHADOW_MODE / SDK_BACKEND env
- backend_would_select matches dispatch._select_sdk_backend logic
- Credit and billing regime are surfaced correctly
- No secret values appear in human-readable or JSON output
- --json produces valid JSON
- Runs cleanly with no SDK data (dispatcher off / empty state)
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.sdk_status import (
    _readiness,
    _backend_would_select,
    _credit_state,
    _routing_counts,
    sdk_status,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_OAUTH = "oauth-token-abc123-secret"
_FAKE_APIKEY = "sk-ant-apikey-secret-xyz"


class TestReadiness:
    """_readiness() reflects env vars correctly."""

    def test_dispatcher_off_by_default(self, monkeypatch):
        monkeypatch.delenv("ROUTE_VIA_DISPATCHER", raising=False)
        monkeypatch.delenv("SHADOW_MODE", raising=False)
        monkeypatch.delenv("SDK_BACKEND", raising=False)

        r = _readiness()

        assert r["dispatcher_live"] is False
        assert r["ROUTE_VIA_DISPATCHER"] == "(not set)"
        assert r["SHADOW_MODE"] == "alternate"
        assert r["SDK_BACKEND"] == "(not set)"

    def test_dispatcher_live_when_env_set_to_1(self, monkeypatch):
        monkeypatch.setenv("ROUTE_VIA_DISPATCHER", "1")

        r = _readiness()

        assert r["dispatcher_live"] is True
        assert r["ROUTE_VIA_DISPATCHER"] == "1"

    def test_dispatcher_not_live_when_env_set_to_0(self, monkeypatch):
        monkeypatch.setenv("ROUTE_VIA_DISPATCHER", "0")

        r = _readiness()

        assert r["dispatcher_live"] is False

    def test_shadow_mode_from_env(self, monkeypatch):
        monkeypatch.setenv("SHADOW_MODE", "sdk")

        r = _readiness()

        assert r["SHADOW_MODE"] == "sdk"

    def test_sdk_backend_override_shown(self, monkeypatch):
        monkeypatch.setenv("SDK_BACKEND", "subscription")

        r = _readiness()

        assert r["SDK_BACKEND"] == "subscription"


class TestBackendWouldSelect:
    """_backend_would_select() mirrors dispatch._select_sdk_backend logic."""

    def test_no_creds_selects_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Pass a non-existent path to isolate from any real machine login file.
        bs = _backend_would_select(credentials_path=str(tmp_path / "no-credentials.json"))

        assert bs["would_select"] == "none"
        assert bs["CLAUDE_CODE_OAUTH_TOKEN"] == "absent"
        assert bs["ANTHROPIC_API_KEY"] == "absent"
        assert bs["claude_login"] == "absent"

    def test_oauth_only_selects_subscription(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_OAUTH)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("SDK_BACKEND", raising=False)

        bs = _backend_would_select()

        assert bs["would_select"] == "subscription"
        assert bs["CLAUDE_CODE_OAUTH_TOKEN"] == "present"
        assert bs["ANTHROPIC_API_KEY"] == "absent"

    def test_apikey_only_selects_apikey(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_APIKEY)
        monkeypatch.delenv("SDK_BACKEND", raising=False)

        bs = _backend_would_select()

        assert bs["would_select"] == "apikey"
        assert bs["ANTHROPIC_API_KEY"] == "present"
        assert bs["CLAUDE_CODE_OAUTH_TOKEN"] == "absent"

    def test_both_creds_prefers_subscription(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_OAUTH)
        monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_APIKEY)
        monkeypatch.delenv("SDK_BACKEND", raising=False)

        bs = _backend_would_select()

        assert bs["would_select"] == "subscription"
        assert bs["CLAUDE_CODE_OAUTH_TOKEN"] == "present"
        assert bs["ANTHROPIC_API_KEY"] == "present"

    def test_sdk_backend_override_subscription(self, monkeypatch):
        monkeypatch.setenv("SDK_BACKEND", "subscription")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        bs = _backend_would_select()

        assert bs["would_select"] == "subscription"
        assert "SDK_BACKEND=subscription" in bs["reason"]

    def test_sdk_backend_override_agent_sdk(self, monkeypatch):
        monkeypatch.setenv("SDK_BACKEND", "agent_sdk")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        bs = _backend_would_select()

        assert bs["would_select"] == "subscription"

    def test_sdk_backend_override_apikey(self, monkeypatch):
        monkeypatch.setenv("SDK_BACKEND", "apikey")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        bs = _backend_would_select()

        assert bs["would_select"] == "apikey"

    def test_no_secret_values_in_output(self, monkeypatch):
        """Credential values must never appear in the output dict."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_OAUTH)
        monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_APIKEY)
        monkeypatch.delenv("SDK_BACKEND", raising=False)

        bs = _backend_would_select()
        serialised = json.dumps(bs)

        assert _FAKE_OAUTH not in serialised
        assert _FAKE_APIKEY not in serialised
        # Presence booleans should be string "present"/"absent"
        assert "present" in serialised


class TestCreditState:
    """_credit_state() surfaces credit and billing regime."""

    def test_credit_state_with_fresh_file(self, tmp_path):
        credit_file = tmp_path / "sdk_credit.json"
        # No file yet — tracker creates a fresh one with $200 initial

        c = _credit_state(credit_file=credit_file)

        assert c["remaining_usd"] == 200.0
        assert c["used_usd"] == 0.0
        assert c["soft_cap_breached"] is False
        assert c["exhausted"] is False
        assert c["billing_regime"] in ("subscription", "credit")
        assert c.get("error") is None

    def test_credit_state_with_partial_usage(self, tmp_path):
        credit_file = tmp_path / "sdk_credit.json"
        import json as _json
        credit_file.write_text(_json.dumps({
            "initial_usd": 200.0,
            "used_usd": 160.0,
            "last_updated": "2026-05-20T00:00:00Z",
            "cache_ts": "2026-05-20T00:00:00Z",
        }))

        c = _credit_state(credit_file=credit_file)

        assert c["remaining_usd"] == pytest.approx(40.0)
        assert c["soft_cap_breached"] is True  # $40 < $50 soft cap

    def test_billing_regime_surfaced(self, tmp_path):
        credit_file = tmp_path / "sdk_credit.json"

        c = _credit_state(credit_file=credit_file)

        assert c["billing_regime"] is not None
        assert c["regime_note"] is not None


class TestRoutingCounts:
    """_routing_counts() reports what's derivable from state."""

    def test_no_db_shows_no_telemetry(self, tmp_path, monkeypatch):
        # Point state dir at an empty tmp dir (no stats.duckdb)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        rc = _routing_counts()

        assert rc["total_runs_all_time"] == 0
        assert rc["total_runs_last_30d"] == 0
        assert "not found" in rc["note"].lower() or rc["db_available"] is False

    def test_sdk_runs_estimate_zero_when_no_credit_consumed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        rc = _routing_counts()

        assert "0 SDK runs" in rc["sdk_runs_estimate"] or "unknown" in rc["sdk_runs_estimate"]

    def test_sdk_runs_estimate_positive_when_credit_consumed(self, tmp_path, monkeypatch):
        import json as _json
        credit_file = tmp_path / "sdk_credit.json"
        credit_file.write_text(_json.dumps({
            "initial_usd": 200.0,
            "used_usd": 0.50,
            "last_updated": "2026-05-20T00:00:00Z",
            "cache_ts": "2026-05-20T00:00:00Z",
        }))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        rc = _routing_counts()

        assert "at least 1" in rc["sdk_runs_estimate"]
        assert "0.5000" in rc["sdk_runs_estimate"]


class TestSdkStatus:
    """Full sdk_status() report structure."""

    def test_report_has_expected_sections(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        report = sdk_status()

        assert "generated_at" in report
        assert "readiness" in report
        assert "backend_selection" in report
        assert "credit" in report
        assert "routing_counts" in report

    def test_dispatcher_off_state(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROUTE_VIA_DISPATCHER", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        # Isolate from real machine login file by using a non-existent path.
        report = sdk_status(credentials_path=str(tmp_path / "no-credentials.json"))

        assert report["readiness"]["dispatcher_live"] is False
        assert report["backend_selection"]["would_select"] == "none"

    def test_no_secret_values_in_full_report(self, tmp_path, monkeypatch):
        """Neither token value may appear anywhere in the report."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_OAUTH)
        monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_APIKEY)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        report = sdk_status()
        serialised = json.dumps(report)

        assert _FAKE_OAUTH not in serialised
        assert _FAKE_APIKEY not in serialised

    def test_json_flag_produces_valid_json(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("ROUTE_VIA_DISPATCHER", raising=False)

        exit_code = main(["--json"])

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)  # raises if invalid JSON
        assert isinstance(parsed, dict)
        assert exit_code == 0

    def test_human_output_no_secret_values(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_OAUTH)
        monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_APIKEY)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        main([])

        captured = capsys.readouterr()
        assert _FAKE_OAUTH not in captured.out
        assert _FAKE_APIKEY not in captured.out
        # Should show presence strings
        assert "present" in captured.out

    def test_runs_with_no_sdk_data(self, tmp_path, monkeypatch, capsys):
        """With dispatcher off and empty state, output is clean and informative."""
        monkeypatch.delenv("ROUTE_VIA_DISPATCHER", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("SDK_BACKEND", raising=False)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        # Patch _CREDENTIALS_FILE so the login-file check doesn't pick up the
        # real machine credentials and falsely report "subscription".
        with patch("backend.orchestrator.sdk_status._CREDENTIALS_FILE",
                   str(tmp_path / "no-credentials.json")):
            exit_code = main([])

        captured = capsys.readouterr()
        output = captured.out
        # Should show dispatcher is off
        assert "OFF" in output
        # Should show backend would be none or cc
        assert "none" in output
        # Should not crash
        assert exit_code == 0

    def test_generated_at_is_iso8601(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        report = sdk_status()

        ts = report["generated_at"]
        # Should parse as ISO 8601
        from datetime import datetime
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
