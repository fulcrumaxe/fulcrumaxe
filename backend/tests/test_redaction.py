"""Behavioral tests for backend/redaction.py.

Covers:
- Every secret pattern IS redacted (true positives)
- Surrounding non-secret text is preserved (no over-redaction)
- Idempotency (redact(redact(x)) == redact(x))
- Edge cases: empty string, no-secret text, multiple secrets, secret at
  start/end/middle of blob
- scan() returns correct Match metadata
- Regression sentinels — variants that would catch a false-negative if a
  pattern regressed

Run with:  python3 -m pytest backend/tests/test_redaction.py -v
"""

from __future__ import annotations

import sys
import os
import importlib

import pytest

# ---------------------------------------------------------------------------
# Import the module under test.
# Supports both direct-run (python3 backend/tests/test_redaction.py) and
# pytest-from-repo-root invocation.
# ---------------------------------------------------------------------------
try:
    from backend.redaction import redact, scan, Match
except ModuleNotFoundError:
    # Running directly or pytest invoked from a different cwd
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from backend.redaction import redact, scan, Match


# ===========================================================================
# Helpers
# ===========================================================================

def _one_hit(text: str, expected_name: str) -> Match:
    """Assert exactly one scan hit with the expected pattern name, return it."""
    hits = scan(text)
    assert len(hits) == 1, f"Expected 1 hit for pattern '{expected_name}', got {hits}"
    assert hits[0].name == expected_name, (
        f"Expected pattern '{expected_name}', got '{hits[0].name}'"
    )
    return hits[0]


def _no_hit(text: str) -> None:
    """Assert scan returns no hits."""
    hits = scan(text)
    assert hits == [], f"Expected clean scan, got hits: {hits}"


# ===========================================================================
# 1. True-positive: each pattern IS detected and redacted
# ===========================================================================

class TestGitHubPat:
    PAT = "github_pat_" + "A" * 36  # exactly 36 chars suffix → matches

    def test_scan_detects(self):
        _one_hit(self.PAT, "github_pat")

    def test_redact_removes(self):
        assert redact(self.PAT) == "[REDACTED]"

    def test_longer_suffix_still_matches(self):
        long_pat = "github_pat_" + "B" * 80
        assert redact(long_pat) == "[REDACTED]"

    def test_underscore_in_suffix(self):
        # underscores are allowed in the suffix
        pat = "github_pat_" + "Aa1_" * 9  # 36 chars with underscores
        assert redact(pat) == "[REDACTED]"


class TestGhsToken:
    TOKEN = "ghs_" + "a" * 36

    def test_scan_detects(self):
        _one_hit(self.TOKEN, "github_prefixed_token")

    def test_redact_removes(self):
        assert redact(self.TOKEN) == "[REDACTED]"

    def test_boundary_36_chars(self):
        # Exactly 36 alphanumeric chars after prefix → must match
        assert redact("ghs_" + "z" * 36) == "[REDACTED]"

    def test_too_short_not_matched(self):
        # 35 chars — below the {36,} threshold — must NOT be redacted
        candidate = "ghs_" + "a" * 35
        result = redact(candidate)
        # If the pattern under-specifies and catches short tokens, this is a
        # false-positive; if it over-specifies and misses long ones, that is worse.
        # Either way the assertion documents the expected behaviour.
        assert result == candidate, (
            "Short ghs_ token (35 chars) should NOT be redacted — below the 36-char minimum"
        )


class TestGhpToken:
    TOKEN = "ghp_" + "A" * 36

    def test_scan_detects(self):
        _one_hit(self.TOKEN, "github_prefixed_token")

    def test_redact_removes(self):
        assert redact(self.TOKEN) == "[REDACTED]"


class TestSlackToken:
    TOKEN = "xoxb-" + "a1b2c3d4e5"  # 10 chars after prefix → exactly the minimum

    def test_scan_detects(self):
        _one_hit(self.TOKEN, "slack_token")

    def test_redact_removes(self):
        assert redact(self.TOKEN) == "[REDACTED]"

    def test_realistic_slack_token(self):
        tok = "xoxb-12345678901-12345678901-abcdefghijklmnopqrstuvwx"
        assert redact(tok) == "[REDACTED]"

    def test_hyphens_in_body(self):
        # Hyphens are allowed in the body
        tok = "xoxb-abc-def-ghi-jkl"
        assert redact(tok) == "[REDACTED]"


class TestAnthropicKey:
    KEY = "sk-ant-" + "a" * 20  # exactly 20 chars after prefix

    def test_scan_detects(self):
        _one_hit(self.KEY, "sk_ant_key")

    def test_redact_removes(self):
        assert redact(self.KEY) == "[REDACTED]"

    def test_realistic_key(self):
        key = "sk-ant-api03-" + "A" * 40
        assert redact(key) == "[REDACTED]"

    def test_hyphens_and_underscores_allowed(self):
        key = "sk-ant-" + "a-b_c" * 5  # 25 chars with hyphens/underscores
        assert redact(key) == "[REDACTED]"


class TestAwsAccessKey:
    KEY = "AKIA" + "A" * 16  # exactly 16 uppercase alphanum chars

    def test_scan_detects(self):
        _one_hit(self.KEY, "aws_access_key")

    def test_redact_removes(self):
        assert redact(self.KEY) == "[REDACTED]"

    def test_digits_allowed_in_body(self):
        key = "AKIA" + "0123456789ABCDEF"  # 16 chars with digits
        assert redact(key) == "[REDACTED]"

    def test_too_short_not_matched(self):
        # 15 chars after AKIA — one short of the required 16 — must NOT match
        candidate = "AKIA" + "A" * 15
        assert redact(candidate) == candidate

    def test_too_long_not_matched(self):
        # 17 chars — the pattern anchors to exactly 16 so it stops after 16 chars.
        # The first 20 chars of a 21-char string still yields a match.
        # Verify the matched portion is the 20-char prefix only.
        key = "AKIA" + "A" * 16 + "X"
        result = redact(key)
        # The key portion is redacted; trailing X survives
        assert result == "[REDACTED]X"


class TestPostgresUri:
    URI = "postgresql://user:password@localhost/mydb"

    def test_scan_detects(self):
        _one_hit(self.URI, "postgres_uri")

    def test_redact_removes(self):
        assert redact(self.URI) == "[REDACTED]"

    def test_postgres_short_form(self):
        uri = "postgres://admin:secret@db.example.com:5432/prod"
        assert redact(uri) == "[REDACTED]"

    def test_special_chars_in_password(self):
        uri = "postgresql://user:p%40ssw0rd@host/db"
        assert redact(uri) == "[REDACTED]"


class TestBearerToken:
    TOKEN_VALUE = "eyJhbGciOiJIUzI1NiJ9"  # JWT-ish but not necessarily valid JWT
    HEADER = f"Bearer {TOKEN_VALUE}"

    def test_scan_detects(self):
        _one_hit(self.HEADER, "bearer_token")

    def test_redact_preserves_word(self):
        result = redact(self.HEADER)
        assert result == "Bearer [REDACTED]"

    def test_multiple_spaces_still_matches(self):
        # Pattern: Bearer\s+ — one or more whitespace chars
        result = redact("Bearer  mytoken123")
        assert result == "Bearer [REDACTED]"

    def test_realistic_bearer_header(self):
        hdr = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
        result = redact(hdr)
        assert result.startswith("Authorization: Bearer [REDACTED]")
        assert "eyJ" not in result


class TestGhTokenEnv:
    def test_scan_detects(self):
        _one_hit("GH_TOKEN=ghp_abc123456789012345678901234567890", "gh_token_env")

    def test_redact_preserves_key(self):
        result = redact("GH_TOKEN=mysecretvalue")
        assert result == "GH_TOKEN=[REDACTED]"

    def test_stops_at_whitespace(self):
        text = "export GH_TOKEN=abc123 other_stuff"
        result = redact(text)
        assert "abc123" not in result
        assert "other_stuff" in result

    def test_stops_at_newline(self):
        text = "GH_TOKEN=secret\nSOME_OTHER=value"
        result = redact(text)
        assert "secret" not in result
        assert "SOME_OTHER=value" in result


class TestJwtToken:
    # Minimal valid-looking JWT structure: header.payload (no sig) and header.payload.sig
    JWT_TWO_PART = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0"
    JWT_THREE_PART = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"

    def test_scan_detects_two_part(self):
        _one_hit(self.JWT_TWO_PART, "jwt_token")

    def test_scan_detects_three_part(self):
        _one_hit(self.JWT_THREE_PART, "jwt_token")

    def test_redact_two_part(self):
        assert redact(self.JWT_TWO_PART) == "[REDACTED]"

    def test_redact_three_part(self):
        assert redact(self.JWT_THREE_PART) == "[REDACTED]"

    def test_realistic_jwt_in_curl(self):
        cmd = "curl -H 'Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.sigpart' https://api.example.com"
        result = redact(cmd)
        assert "eyJ" not in result


# ===========================================================================
# 2. Non-secret text is preserved (no over-redaction)
# ===========================================================================

class TestNoOverRedaction:
    def test_plain_text_unchanged(self):
        text = "This is a normal sentence with no secrets."
        assert redact(text) == text

    def test_similar_looking_but_short_prefix(self):
        # github_pat_ with only 35 chars suffix — should NOT be redacted
        text = "github_pat_" + "A" * 35
        assert redact(text) == text

    def test_bearer_without_token(self):
        # "Bearer" by itself — no token value follows; should not be redacted
        # (Pattern requires \s+ after Bearer)
        # The regex Bearer\s+[A-Za-z0-9\-._~+/]+=* requires at least one
        # alphanumeric char. If "Bearer" is followed by punctuation only, no match.
        text = "Bearer"
        assert redact(text) == text

    def test_aws_key_lowercase_not_matched(self):
        # AKIA pattern requires uppercase — lowercase should NOT match
        text = "akia" + "a" * 16
        assert redact(text) == text

    def test_akia_insufficient_body(self):
        # AKIA + only 15 chars — should not match
        text = "AKIA123456789012345"  # AKIA + 15 chars
        assert len(text) == 4 + 15
        assert redact(text) == text

    def test_postgres_uri_no_password(self):
        # URI without user:pass@ format — no @ means no match
        text = "postgres://localhost/mydb"
        assert redact(text) == text

    def test_slack_token_too_short(self):
        # xoxb- with only 9 chars — below minimum 10
        text = "xoxb-abcde1234"  # 9 chars after prefix
        assert len(text.split("-", 1)[1]) == 9
        assert redact(text) == text

    def test_jwt_no_dot(self):
        # Starts with eyJ but has no dot — not a JWT
        text = "eyJhelloworld"
        assert redact(text) == text

    def test_regular_url_unchanged(self):
        url = "https://github.com/user/repo/issues/42"
        assert redact(url) == url

    def test_log_line_unchanged(self):
        line = "2026-05-20 12:00:00 INFO loop iteration 42 started"
        assert redact(line) == line


# ===========================================================================
# 3. Idempotency
# ===========================================================================

class TestIdempotency:
    def test_redact_already_redacted_token(self):
        # [REDACTED] should not itself be re-redacted
        assert redact("[REDACTED]") == "[REDACTED]"

    def test_bearer_redacted_is_idempotent(self):
        text = "Bearer [REDACTED]"
        # Already-redacted bearer — applying redact again must be a no-op
        assert redact(text) == text

    def test_gh_token_env_redacted_is_idempotent(self):
        text = "GH_TOKEN=[REDACTED]"
        assert redact(text) == text

    def test_double_redact_github_pat(self):
        pat = "github_pat_" + "X" * 36
        once = redact(pat)
        twice = redact(once)
        assert once == twice

    def test_double_redact_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        once = redact(jwt)
        twice = redact(once)
        assert once == twice


# ===========================================================================
# 4. Edge cases: empty, no secrets, multiple secrets, position variants
# ===========================================================================

class TestEdgeCases:
    def test_empty_string(self):
        assert redact("") == ""
        assert scan("") == []

    def test_whitespace_only(self):
        assert redact("   \n\t  ") == "   \n\t  "
        assert scan("   \n\t  ") == []

    def test_secret_at_start(self):
        pat = "github_pat_" + "A" * 36
        text = pat + " followed by normal text"
        result = redact(text)
        assert result.startswith("[REDACTED]")
        assert "followed by normal text" in result

    def test_secret_at_end(self):
        pat = "github_pat_" + "B" * 36
        text = "Some prefix text then " + pat
        result = redact(text)
        assert result.endswith("[REDACTED]")
        assert "Some prefix text then" in result

    def test_secret_in_middle(self):
        pat = "ghp_" + "C" * 36
        text = "Before " + pat + " after"
        result = redact(text)
        assert "Before" in result
        assert "after" in result
        assert "[REDACTED]" in result
        assert "ghp_" not in result

    def test_multiple_different_secrets(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.sig"
        aws = "AKIA" + "A" * 16
        text = f"JWT={jwt} AWS={aws}"
        result = redact(text)
        assert "eyJ" not in result
        assert "AKIA" not in result
        assert "JWT=" in result
        assert "AWS=" in result

    def test_two_github_pats_in_blob(self):
        pat1 = "github_pat_" + "A" * 36
        pat2 = "github_pat_" + "B" * 36
        text = f"first={pat1} second={pat2}"
        result = redact(text)
        assert result == "first=[REDACTED] second=[REDACTED]"

    def test_secret_embedded_in_json(self):
        key = "sk-ant-" + "x" * 30
        payload = f'{{"api_key": "{key}", "model": "claude-3"}}'
        result = redact(payload)
        assert "[REDACTED]" in result
        assert key not in result
        assert '"model": "claude-3"' in result

    def test_secret_in_multiline_blob(self):
        token = "xoxb-" + "a1" * 10
        text = f"line 1\nSLACK_TOKEN={token}\nline 3"
        result = redact(text)
        assert "line 1" in result
        assert "line 3" in result
        assert token not in result

    def test_scan_returns_position_metadata(self):
        pat = "github_pat_" + "A" * 36
        text = "prefix " + pat + " suffix"
        hits = scan(text)
        assert len(hits) == 1
        h = hits[0]
        assert h.start == len("prefix ")
        assert h.end == len("prefix ") + len(pat)
        assert h.value == pat
        assert h.name == "github_pat"

    def test_scan_sorted_by_position(self):
        # jwt appears first (offset 0), then ghp token
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.sig"
        ghp = "ghp_" + "D" * 36
        text = jwt + "---" + ghp
        hits = scan(text)
        # jwt should come before ghp in position ordering
        jwt_hits = [h for h in hits if h.name == "jwt_token"]
        ghp_hits = [h for h in hits if h.name == "github_prefixed_token"]
        assert jwt_hits and ghp_hits
        assert jwt_hits[0].start < ghp_hits[0].start


# ===========================================================================
# 5. Regression sentinels — catch false negatives from pattern regression
# ===========================================================================

class TestRegressionSentinels:
    """
    These tests would FAIL if a pattern regressed (e.g. min-length check
    removed, prefix typo, character class narrowed).
    """

    def test_github_pat_minimum_length_regression(self):
        # If the {36,} minimum were changed to {40,}, this would pass redaction.
        pat = "github_pat_" + "A" * 36  # exactly at minimum
        assert "[REDACTED]" in redact(pat), (
            "REGRESSION: github_pat pattern no longer matches 36-char suffixes"
        )

    def test_ghs_token_minimum_length_regression(self):
        tok = "ghs_" + "b" * 36
        assert "[REDACTED]" in redact(tok), (
            "REGRESSION: ghs_token pattern no longer matches 36-char values"
        )

    def test_ghp_token_minimum_length_regression(self):
        tok = "ghp_" + "c" * 36
        assert "[REDACTED]" in redact(tok), (
            "REGRESSION: ghp_token pattern no longer matches 36-char values"
        )

    def test_anthropic_key_minimum_length_regression(self):
        key = "sk-ant-" + "d" * 20
        assert "[REDACTED]" in redact(key), (
            "REGRESSION: sk_ant_key pattern no longer matches 20-char suffixes"
        )

    def test_aws_key_full_16_char_body(self):
        key = "AKIA" + "ABCDEF1234567890"  # 16 uppercase alphanum
        assert "[REDACTED]" in redact(key), (
            "REGRESSION: aws_access_key pattern no longer matches 16-char body"
        )

    def test_bearer_in_log_line(self):
        line = "DEBUG request_headers={'Authorization': 'Bearer tok-abc123XYZ'}"
        result = redact(line)
        assert "tok-abc123XYZ" not in result, (
            "REGRESSION: bearer_token pattern missed token in log line"
        )

    def test_jwt_no_false_escape(self):
        # A realistic compact JWT must not slip through
        jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJodHRwczovL2FjY291bnRzLmV4YW1wbGUuY29tIn0.abc"
        result = redact(jwt)
        assert "eyJ" not in result, (
            "REGRESSION: jwt_token pattern no longer catches realistic compact JWTs"
        )

    def test_postgres_uri_with_special_host(self):
        uri = "postgresql://dbuser:correcthorsebatterystaple@db-prod.us-east-1.rds.amazonaws.com/mydb"
        result = redact(uri)
        assert "correcthorsebatterystaple" not in result, (
            "REGRESSION: postgres_uri pattern missed real RDS connection string"
        )

    def test_slack_token_with_realistic_value(self):
        tok = "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx"
        result = redact(tok)
        assert tok not in result, (
            "REGRESSION: slack_token pattern missed realistic xoxb- bot token"
        )

    def test_gh_token_env_in_shell_script(self):
        script = "export GH_TOKEN=ghp_realtoken123456789012345678901234"
        result = redact(script)
        assert "ghp_realtoken" not in result, (
            "REGRESSION: gh_token_env pattern missed GH_TOKEN= env var"
        )


# ===========================================================================
# 6. scan() API contract
# ===========================================================================

class TestScanApi:
    def test_returns_list(self):
        assert isinstance(scan("clean text"), list)

    def test_empty_on_clean_input(self):
        assert scan("nothing secret here") == []

    def test_match_has_correct_fields(self):
        tok = "ghp_" + "E" * 36
        hits = scan(tok)
        assert len(hits) == 1
        m = hits[0]
        assert hasattr(m, "name")
        assert hasattr(m, "value")
        assert hasattr(m, "start")
        assert hasattr(m, "end")

    def test_match_value_equals_original_substring(self):
        tok = "ghs_" + "F" * 36
        text = "before_" + tok + "_after"
        hits = scan(text)
        assert len(hits) == 1
        assert hits[0].value == tok

    def test_match_coordinates_consistent(self):
        tok = "sk-ant-" + "G" * 25
        text = "key=" + tok
        hits = scan(text)
        assert len(hits) == 1
        h = hits[0]
        assert text[h.start:h.end] == h.value

    def test_multiple_hits_sorted(self):
        aws = "AKIA" + "H" * 16
        ghp = "ghp_" + "I" * 36
        text = aws + "---" + ghp
        hits = scan(text)
        names = [h.name for h in hits]
        starts = [h.start for h in hits]
        # Hits must be in ascending position order
        assert starts == sorted(starts), (
            f"scan() returned hits out of position order: {starts}"
        )
        assert "aws_access_key" in names
        assert "github_prefixed_token" in names
