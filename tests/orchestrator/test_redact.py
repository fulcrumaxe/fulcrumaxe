"""tests/orchestrator/test_redact.py — Unit tests for orchestrator redaction (S7)."""

import pytest
from backend.orchestrator.redact import redact, scan, OrchestratorMatch


class TestRedactPatterns:
    """Verify all four S7-required secret patterns are redacted correctly."""

    def test_anthropic_key_redacted(self):
        raw = "using key sk-ant-api03-ABCDEF1234567890abcdefghij-xyzXYZ_abcdefghijklmnop"
        result = redact(raw)
        assert "[REDACTED:anthropic]" in result
        assert "sk-ant-" not in result

    def test_github_pat_redacted(self):
        raw = "token ghp_ABCDEF1234567890abcdefghijklmnop"
        result = redact(raw)
        assert "[REDACTED:github-pat]" in result
        assert "ghp_" not in result

    def test_github_server_token_redacted(self):
        raw = "Authorization: Bearer ghs_ABCDEF1234567890abcdefghijklmnop"
        result = redact(raw)
        assert "[REDACTED:github-server]" in result
        assert "ghs_" not in result

    def test_jwt_token_redacted(self):
        raw = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzIn0.abc123XYZ-abc"
        result = redact(raw)
        assert "[REDACTED:jwt]" in result
        assert "eyJhbGciOi" not in result

    def test_clean_text_unchanged(self):
        raw = "This is a normal log message with no secrets."
        assert redact(raw) == raw

    def test_idempotent(self):
        """Redacting already-redacted text should not double-redact."""
        raw = "key=sk-ant-api03-ABCDEF1234567890abcdefghijklmnopqrstuvwxyz"
        once = redact(raw)
        twice = redact(once)
        assert once == twice

    def test_multiple_patterns_in_one_string(self):
        raw = "key=sk-ant-api03-ABCDEF1234567890abc token=ghp_ABCDEF1234567890abcdefghijklmnop"
        result = redact(raw)
        assert "[REDACTED:anthropic]" in result
        assert "[REDACTED:github-pat]" in result
        assert "sk-ant-" not in result
        assert "ghp_" not in result


class TestScanFunction:
    """Verify scan() returns OrchestratorMatch objects."""

    def test_scan_finds_anthropic_key(self):
        raw = "sk-ant-api03-ABCDEF1234567890abcdefghijklmnopqrstuvwxyz config"
        matches = scan(raw)
        assert any(m.name == "anthropic_key" for m in matches)

    def test_scan_returns_empty_for_clean_text(self):
        assert scan("no secrets here") == []

    def test_scan_returns_sorted_by_position(self):
        raw = "token=ghp_ABCDEF1234567890abcdefghijklmnop key=sk-ant-api03-ABCDEF1234567890abc"
        matches = scan(raw)
        assert len(matches) >= 2
        positions = [m.start for m in matches]
        assert positions == sorted(positions)

    def test_scan_match_fields(self):
        raw = "ghp_ABCDEF1234567890abcdefghijklmnop"
        matches = scan(raw)
        assert len(matches) == 1
        m = matches[0]
        assert m.name == "github_pat"
        assert m.start == 0
        assert m.end == len(raw)
        assert m.value == raw
