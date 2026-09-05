"""
test_redaction.py — unit tests for backend/redaction.py.

Every supported secret pattern type is exercised with at least one
positive fixture (must redact) and the scan() return value is verified.
"""

import pytest

from backend.redaction import redact, scan, Match


# ---------------------------------------------------------------------------
# Fixtures — one per secret type
# ---------------------------------------------------------------------------

SECRETS = [
    # (pattern_name, raw_text, expected_redacted_substring)
    ("ghp_token",    "GH_TOKEN=ghp_abc123XYZdef456ghi789jkl012mno345pqr678",  "[REDACTED]"),
    ("ghs_token",    "token: ghs_abc123XYZdef456ghi789jkl012mno345pqr678",     "[REDACTED]"),
    ("github_pat",   "pat: github_pat_11ABCDE_abc123def456ghi789jkl012mno345pqr", "[REDACTED]"),
    ("sk_ant_key",   "Authorization: sk-ant-api03-abc123def456ghi789jkl-012",  "[REDACTED]"),
    ("aws_access_key","export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",          "[REDACTED]"),
    ("postgres_uri", "DSN=postgres://user:s3cret@db.internal/mydb",             "[REDACTED]"),
    ("bearer_token", "Authorization: Bearer eyJfoo.eyJbar.baz",                "Bearer [REDACTED]"),
    ("gh_token_env", "GH_TOKEN=ghp_will_be_caught_by_gh_token_env_first",      "GH_TOKEN=[REDACTED]"),
    ("jwt_token",    "token=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.sig",    "[REDACTED]"),
    ("slack_token",  "SLACK_BOT_TOKEN=xoxb-12345678-abc-def",                  "[REDACTED]"),
]


@pytest.mark.parametrize("name,raw,expected_in_result", SECRETS)
def test_redact_removes_secret(name, raw, expected_in_result):
    result = redact(raw)
    # The expected placeholder must appear in the result
    assert expected_in_result in result, (
        f"Pattern '{name}': redact() output missing '{expected_in_result}'. Got: {result!r}"
    )


@pytest.mark.parametrize("name,raw,_", SECRETS)
def test_scan_finds_secret(name, raw, _):
    hits = scan(raw)
    # At least one match should be found
    assert hits, f"Pattern '{name}': scan() found no matches in: {raw!r}"


def test_scan_returns_match_objects():
    raw = "AKIA1234567890ABCDEF"
    hits = scan(raw)
    assert len(hits) >= 1
    m = hits[0]
    assert isinstance(m, Match)
    assert m.name == "aws_access_key"
    assert m.value == "AKIA1234567890ABCDEF"
    assert m.start == 0
    assert m.end == 20


def test_scan_clean_text_returns_empty():
    assert scan("Hello world, nothing secret here.") == []


def test_redact_clean_text_unchanged():
    clean = "SELECT * FROM table WHERE id = 1;"
    assert redact(clean) == clean


def test_redact_multiple_patterns_in_one_string():
    raw = (
        "GH_TOKEN=ghp_abc123XYZdef456ghi789jkl012mno345pqr678 "
        "and AKIAIOSFODNN7EXAMPLE in the same line"
    )
    result = redact(raw)
    assert "ghp_" not in result
    assert "AKIA" not in result
    assert "[REDACTED]" in result


def test_redact_postgres_uri_various_forms():
    uris = [
        "postgres://alice:password123@pg.example.com/mydb",
        "postgresql://bob:secret@localhost:5432/testdb",
    ]
    for uri in uris:
        result = redact(uri)
        assert "@" not in result or "[REDACTED]" in result, (
            f"Postgres URI not fully redacted: {result!r}"
        )


def test_scan_sorted_by_position():
    raw = "AKIAIOSFODNN7EXAMPLE ... ghp_abc123XYZdef456ghi789jkl012mno345pqr678"
    hits = scan(raw)
    positions = [h.start for h in hits]
    assert positions == sorted(positions)
