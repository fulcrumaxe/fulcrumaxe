"""
redaction.py — shared secret-scrubbing module.

Primary control: redact at the data source (stats writers, RPC handlers) so
that the TUI, web dashboard, logs, and screenshots all inherit scrubbed values.

The tui-tester uses scan() as a pre-upload gate; any artifact that hits a
pattern is refused for upload, quarantined, and replaced with [REDACTED].

Usage::

    from backend.redaction import redact, scan

    clean = redact(raw_text)          # replace secrets in-place
    hits  = scan(raw_text)            # list Match objects; empty = clean
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


# ---------------------------------------------------------------------------
# Pattern registry — extend here, never inline elsewhere
# ---------------------------------------------------------------------------

# Each tuple: (name, compiled_regex, replacement)
# Ordered longest/most-specific first to avoid partial matches.
#
# For patterns that must preserve a context prefix (e.g. the keyword before a
# 40-hex classic PAT), the replacement string may contain a back-reference
# to group 1 (\g<ctx>) via a named group named "ctx".  The redact() function
# handles this transparently — callers see no difference.
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # GitHub PATs (fine-grained)
    (
        "github_pat",
        re.compile(r"github_pat_[A-Za-z0-9_]{36,}"),
        "[REDACTED]",
    ),
    # GitHub prefixed tokens — ghp_ (classic PAT), ghs_ (Actions/installation),
    # gho_ (OAuth app token), ghu_ (user-to-server), ghr_ (refresh token).
    # All share the format: 3-char prefix + underscore + 36+ alphanumeric chars.
    # Previously only ghp_ and ghs_ were covered; gho_/ghu_/ghr_ are now added.
    (
        "github_prefixed_token",
        re.compile(r"gh[pshour]_[A-Za-z0-9]{36,}"),
        "[REDACTED]",
    ),
    # Classic 40-hex GitHub PATs (pre-mid-2021, no gh*_ prefix).
    #
    # Design tension: git SHA-1 hashes are also 40 lowercase hex characters.
    # A bare \b[0-9a-f]{40}\b would redact every SHA in logs and transcripts —
    # an unacceptable false-positive rate.
    #
    # Approach: require a token-context keyword immediately before the hex string.
    # Covered contexts:
    #   • Authorization: token <hex>       (HTTP header, standard GitHub auth)
    #   • x-access-token: <hex>            (GitHub Apps, Actions)
    #   • token=<hex>                      (query-string / env assignments)
    #   • pat=<hex>                        (explicit PAT assignments)
    #   • github_token=<hex>               (CI env vars, case-insensitive)
    #   • gh_token=<hex>                   (short env var alias)
    #   • x_auth_token=<hex>               (generic token env pattern)
    #
    # git SHAs appear as bare hex after spaces, in commit log output, diff
    # headers, etc. — none of those contexts match the anchors above, so they
    # are left untouched.
    #
    # The named group "ctx" captures the keyword + delimiter so the replacement
    # can preserve it:  \g<ctx>[REDACTED]
    (
        "classic_gh_pat",
        re.compile(
            r"(?P<ctx>"
            r"(?:Authorization\s*:\s*token\s+)"       # HTTP header
            r"|(?:x-access-token\s*:\s*)"              # X-Access-Token header
            r"|(?:(?:token|pat|github[_-]token|gh[_-]token|x[_-]auth[_-]token)\s*[=:]\s*)"
            r")"
            r"([0-9a-f]{40})"                          # 40-hex secret
            r"(?![0-9a-f])",                           # not part of a longer run
            re.IGNORECASE,
        ),
        r"\g<ctx>[REDACTED]",
    ),
    # Slack bot tokens
    (
        "slack_token",
        re.compile(r"xoxb-[A-Za-z0-9\-]{10,}"),
        "[REDACTED]",
    ),
    # Anthropic API keys
    (
        "sk_ant_key",
        re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
        "[REDACTED]",
    ),
    # AWS access key IDs (AKIA + 16 uppercase alphanum chars)
    (
        "aws_access_key",
        re.compile(r"AKIA[0-9A-Z]{16}"),
        "[REDACTED]",
    ),
    # Postgres / PostgreSQL connection URIs (user:pass@host)
    (
        "postgres_uri",
        re.compile(r"postgres(?:ql)?://[^@\s]+@[^\s\"']+"),
        "[REDACTED]",
    ),
    # Bearer tokens in HTTP headers
    (
        "bearer_token",
        re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
        "Bearer [REDACTED]",
    ),
    # GH_TOKEN= env-var assignments
    (
        "gh_token_env",
        re.compile(r"GH_TOKEN=[^\s\"'\n]+"),
        "GH_TOKEN=[REDACTED]",
    ),
    # JWT tokens — eyJ header (header.payload, optionally .signature)
    (
        "jwt_token",
        re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?"),
        "[REDACTED]",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class Match:
    """A single secret match found by scan()."""

    name: str          # pattern name (e.g. "ghp_token")
    value: str         # the matched substring
    start: int         # start offset in input text
    end: int           # end offset in input text


def scan(text: str) -> List[Match]:
    """Return all secret matches found in *text*.

    Returns an empty list when *text* is clean.  Callers should treat a
    non-empty return as a hard-stop for any upload operation.
    """
    results: list[Match] = []
    for name, pattern, _ in _PATTERNS:
        for m in pattern.finditer(text):
            results.append(Match(name=name, value=m.group(), start=m.start(), end=m.end()))
    # Sort by position for deterministic output
    results.sort(key=lambda m: m.start)
    return results


def redact(text: str) -> str:
    """Replace all secret patterns in *text* with their redaction strings.

    Applies all patterns from left to right; overlapping matches use the
    first-matched pattern (longest-match bias from ordering above).
    """
    for _name, pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
