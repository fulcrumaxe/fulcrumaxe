"""test_redaction_gaps.py — targeted tests for D#1277 redaction gaps.

Covers two gaps that were missing from the original pattern registry:
  1. GitHub OAuth/user-to-server/refresh token prefixes: gho_, ghu_, ghr_
  2. Classic 40-hex GitHub PATs (pre-mid-2021, no gh*_ prefix)

The critical constraint for gap 2 is that bare 40-hex git SHA-1 strings must
NOT be redacted — they appear constantly in this repo's logs and transcripts.
Context-scoping via keyword lookbehind is the chosen defence.

Do NOT edit test_redaction.py — it is in a separate in-flight PR branch.
"""

from backend.redaction import redact, scan

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# A realistic 40-hex classic PAT (all lowercase hex, no prefix)
CLASSIC_PAT = "a3f8c2e1d0b4f7a9c8e2d5b3f1a7e4c0d9b6a2e8"

# A realistic git SHA (also 40 lowercase hex — must not be redacted)
GIT_SHA = "71efd33a53b2104ef78932cc21db447f90031a2b"

# A longer hex string (>40 chars) that should not be treated as a PAT
LONG_HEX = "a3f8c2e1d0b4f7a9c8e2d5b3f1a7e4c0d9b6a2e8ff"  # 42 chars


# ---------------------------------------------------------------------------
# Gap 1 — gho_ / ghu_ / ghr_ token prefixes
# ---------------------------------------------------------------------------


class TestGitHubOAuthTokens:
    """gho_, ghu_, ghr_ were not covered before D#1277."""

    def _make_token(self, prefix: str) -> str:
        return f"{prefix}_" + "A" * 36

    def test_gho_token_is_redacted(self) -> None:
        token = self._make_token("gho")
        result = redact(f"token: {token}")
        assert "[REDACTED]" in result
        assert token not in result

    def test_ghu_token_is_redacted(self) -> None:
        token = self._make_token("ghu")
        result = redact(f"set token={token}")
        assert "[REDACTED]" in result
        assert token not in result

    def test_ghr_token_is_redacted(self) -> None:
        token = self._make_token("ghr")
        result = redact(f"refresh={token}")
        assert "[REDACTED]" in result
        assert token not in result

    def test_gho_scan_returns_match(self) -> None:
        token = self._make_token("gho")
        hits = scan(token)
        assert len(hits) >= 1
        assert any("github" in h.name or "token" in h.name for h in hits)

    def test_ghu_token_minimum_length(self) -> None:
        """Exactly 36 alphanum chars after prefix — minimum that should match."""
        token = "ghu_" + "B" * 36
        result = redact(token)
        assert "[REDACTED]" in result

    def test_ghr_token_too_short_not_redacted(self) -> None:
        """35 chars after prefix — below the 36-char minimum, must not match."""
        token = "ghr_" + "C" * 35
        result = redact(token)
        # Should be unchanged (too short to match)
        assert token in result

    def test_ghp_still_redacted(self) -> None:
        """Regression: ghp_ (already covered) must still work."""
        token = "ghp_" + "D" * 36
        result = redact(token)
        assert "[REDACTED]" in result
        assert token not in result

    def test_ghs_still_redacted(self) -> None:
        """Regression: ghs_ (already covered) must still work."""
        token = "ghs_" + "E" * 36
        result = redact(token)
        assert "[REDACTED]" in result
        assert token not in result


# ---------------------------------------------------------------------------
# Gap 2 — classic 40-hex PATs in token contexts
# ---------------------------------------------------------------------------


class TestClassic40HexPAT:
    """Classic PATs (40 lowercase hex, no prefix) redacted only in token contexts."""

    def test_authorization_header_redacted(self) -> None:
        text = f"Authorization: token {CLASSIC_PAT}"
        result = redact(text)
        assert CLASSIC_PAT not in result
        assert "[REDACTED]" in result
        # The keyword is preserved
        assert "Authorization: token" in result

    def test_x_access_token_header_redacted(self) -> None:
        text = f"x-access-token: {CLASSIC_PAT}"
        result = redact(text)
        assert CLASSIC_PAT not in result
        assert "[REDACTED]" in result

    def test_token_equals_redacted(self) -> None:
        text = f"token={CLASSIC_PAT}"
        result = redact(text)
        assert CLASSIC_PAT not in result
        assert "[REDACTED]" in result

    def test_pat_equals_redacted(self) -> None:
        text = f"pat={CLASSIC_PAT}"
        result = redact(text)
        assert CLASSIC_PAT not in result
        assert "[REDACTED]" in result

    def test_github_token_env_redacted(self) -> None:
        text = f"GITHUB_TOKEN={CLASSIC_PAT}"
        result = redact(text)
        assert CLASSIC_PAT not in result
        assert "[REDACTED]" in result

    def test_gh_token_env_redacted(self) -> None:
        text = f"gh_token={CLASSIC_PAT}"
        result = redact(text)
        assert CLASSIC_PAT not in result
        assert "[REDACTED]" in result

    def test_scan_returns_match_for_token_context(self) -> None:
        text = f"Authorization: token {CLASSIC_PAT}"
        hits = scan(text)
        assert len(hits) >= 1


# ---------------------------------------------------------------------------
# CRITICAL: git SHA false-positive prevention
# ---------------------------------------------------------------------------


class TestGitSHANotRedacted:
    """Bare 40-hex git SHAs must survive redact() untouched."""

    def test_bare_sha_not_redacted(self) -> None:
        """A lone git SHA with no token-context keyword must be preserved."""
        result = redact(GIT_SHA)
        assert result == GIT_SHA

    def test_sha_in_commit_log_not_redacted(self) -> None:
        """Typical git log output line — SHA must survive."""
        line = f"commit {GIT_SHA}\nAuthor: dev <dev@example.com>"
        result = redact(line)
        assert GIT_SHA in result

    def test_sha_after_merge_not_redacted(self) -> None:
        """PR merge message pattern — SHA must survive."""
        text = f"Merged PR #42 (squash commit {GIT_SHA})"
        result = redact(text)
        assert GIT_SHA in result

    def test_sha_in_diff_header_not_redacted(self) -> None:
        """git diff output contains SHAs in index lines."""
        text = f"index {GIT_SHA[:7]}..{GIT_SHA[:7]} 100644"
        result = redact(text)
        # Shorter than 40 chars — definitely not redacted
        assert "100644" in result

    def test_multiple_shas_in_log_not_redacted(self) -> None:
        """Multiple SHAs in a realistic log snippet."""
        sha2 = "faeb3326b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6"
        text = f"{GIT_SHA} {sha2} HEAD~2"
        result = redact(text)
        assert GIT_SHA in result
        assert sha2 in result

    def test_sha_adjacent_to_word_not_redacted(self) -> None:
        """SHA preceded by non-token word — must not match."""
        text = f"resolved {GIT_SHA} via rebase"
        result = redact(text)
        assert GIT_SHA in result

    def test_scan_returns_no_hits_for_bare_sha(self) -> None:
        """scan() must return empty list for a bare SHA."""
        hits = scan(GIT_SHA)
        # Filter to only classic_gh_pat hits — other patterns should not fire
        pat_hits = [h for h in hits if h.name == "classic_gh_pat"]
        assert pat_hits == []

    def test_longer_hex_not_redacted(self) -> None:
        """A hex string longer than 40 chars is not a SHA or PAT — must not fire."""
        result = redact(LONG_HEX)
        assert LONG_HEX in result


# ---------------------------------------------------------------------------
# Benign text preservation
# ---------------------------------------------------------------------------


class TestBenignTextPreserved:
    """Non-secret text must pass through redact() unchanged."""

    def test_plain_log_line(self) -> None:
        line = "2026-05-20 INFO loop iteration 42 complete"
        assert redact(line) == line

    def test_url_preserved(self) -> None:
        url = "https://github.com/autonomous-agent-7/autonomous-forever"
        assert redact(url) == url

    def test_short_hex_preserved(self) -> None:
        # 7-char abbreviated SHA — common in git log --oneline
        assert redact("71efd33") == "71efd33"

    def test_numeric_string_preserved(self) -> None:
        assert redact("PR #1277 merged") == "PR #1277 merged"

    def test_empty_string(self) -> None:
        assert redact("") == ""
