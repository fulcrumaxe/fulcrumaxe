"""backend/orchestrator/redact.py — Redaction filter for orchestrator writes.

Thin specialization of backend.redaction with the exact patterns and
replacement strings required by the orchestrator spec (S7).

Applied at write time to:
  - audit.jsonl entries
  - team-log issue comments
  - agent_runs row writes (prompt content is NOT stored; only SHA-256 hash)

Usage::

    from backend.orchestrator.redact import redact, OrchestratorMatch, scan

    clean = redact(raw_text)          # replace secrets with typed labels
    hits  = scan(raw_text)            # list OrchestratorMatch objects
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


# ---------------------------------------------------------------------------
# Pattern registry (S7 — exact spec patterns + replacements)
# ---------------------------------------------------------------------------

@dataclass
class OrchestratorMatch:
    """A single secret match found by scan()."""
    name: str       # pattern name (e.g. "anthropic_key")
    value: str      # the matched substring
    start: int      # start offset in input text
    end: int        # end offset in input text


# Each tuple: (name, compiled_regex, replacement)
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # Anthropic API keys — must come before generic JWT pattern
    (
        "anthropic_key",
        re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
        "[REDACTED:anthropic]",
    ),
    # Classic GitHub PATs (ghp_)
    (
        "github_pat",
        re.compile(r"ghp_[A-Za-z0-9]+"),
        "[REDACTED:github-pat]",
    ),
    # GitHub server / Actions tokens (ghs_)
    (
        "github_server",
        re.compile(r"ghs_[A-Za-z0-9]+"),
        "[REDACTED:github-server]",
    ),
    # JWT-shaped tokens (header.payload.signature or header.payload)
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        "[REDACTED:jwt]",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan(text: str) -> List[OrchestratorMatch]:
    """Return all secret matches found in *text*.

    Returns an empty list when *text* is clean.  Callers should treat a
    non-empty return as a gate failure for any audit write that embeds raw
    prompt content.
    """
    results: list[OrchestratorMatch] = []
    for name, pattern, _ in _PATTERNS:
        for m in pattern.finditer(text):
            results.append(
                OrchestratorMatch(name=name, value=m.group(), start=m.start(), end=m.end())
            )
    results.sort(key=lambda m: m.start)
    return results


def redact(text: str) -> str:
    """Replace all secret patterns in *text* with their typed redaction labels.

    Applies patterns in declaration order (most-specific first).
    Idempotent: already-redacted text is returned unchanged.
    """
    for _name, pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
