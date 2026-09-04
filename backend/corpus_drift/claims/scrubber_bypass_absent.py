"""executor.scrubber_bypass_absent — fraction of transcripts with zero secret-pattern matches.

Scans recent subagent transcripts for patterns that the scrubber is meant to
catch. If the scrubber is working correctly, none of these should appear in
transcript output. Score = % of transcripts with zero matches.

Patterns reused from backend.transcript_tailer._SECRET_PATTERNS for symmetry:
  - URL credentials: https://user:token@host/...
  - GitHub tokens: ghp_, ghs_, gho_, ghx_
  - Anthropic keys: sk-ant-
  - Authorization header values
  - GH_TOKEN= / ANTHROPIC_API_KEY= assignments

Healthy when 100% of transcripts have zero matches (scrubber never bypassed).

Scope: executor + code-reviewer.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from backend.corpus_drift.types import ClaimResult
from backend.transcript_reader import TRANSCRIPT_GLOB, iter_turns

# Import the compiled patterns from the canonical source
from backend.transcript_tailer import _SECRET_PATTERNS

logger = logging.getLogger(__name__)

CLAIM_ID = "executor.scrubber_bypass_absent"
ROLE_SCOPE = "executor,code-reviewer"

# Additional lightweight check patterns that catch raw secret text
# regardless of surrounding context — complement _SECRET_PATTERNS.
_EXTRA_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bghp_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"https?://[^/\s]*:[^@/\s]+@", re.IGNORECASE),
    re.compile(r"Authorization\s*:\s*(?:Bearer\s+)?\S{8,}", re.IGNORECASE),
    re.compile(r"GH_TOKEN\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"ANTHROPIC_API_KEY\s*=\s*\S+", re.IGNORECASE),
]


def _transcript_has_secret(path: Path) -> bool:
    """Return True if any turn in the transcript contains a raw secret pattern."""
    try:
        for turn in iter_turns(path):
            for tc in turn.tool_calls:
                # Check tool call inputs
                cmd = tc.get("input", {}).get("command", "") or ""
                if _text_has_secret(cmd):
                    return True
            # Check assistant text turns
            if turn.role == "assistant" and _text_has_secret(turn.text):
                return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("scrubber_bypass_absent: error reading %s: %s", path, exc)
    return False


def _text_has_secret(text: str) -> bool:
    """Return True if text matches any secret pattern."""
    if not text:
        return False
    for pattern in _EXTRA_PATTERNS:
        if pattern.search(text):
            return True
    # Also run the canonical scrubber patterns — if they would change the text,
    # it means a raw secret is present.
    for compiled_re, replacement in _SECRET_PATTERNS:
        if compiled_re.search(text):
            return True
    return False


def evaluate(
    runs: list[dict[str, Any]],
    transcripts_dir: Path | None,
    window_days: int,
    sample_cap: int = 100,
    **_kwargs: Any,
) -> ClaimResult:
    """Evaluate fraction of transcripts with zero raw-secret-pattern matches.

    Parameters
    ----------
    runs:
        Agent run rows for executor/code-reviewer roles — used to prioritise
        which transcripts to scan; falls back to all recent transcripts.
    transcripts_dir:
        Unused — canonical glob used.
    window_days:
        Audit window in days.
    sample_cap:
        Max transcripts to examine.
    """
    since_seconds = window_days * 86400
    now = time.time()
    cutoff = now - since_seconds

    all_paths = sorted(
        p for p in glob.glob(TRANSCRIPT_GLOB)
        if os.path.getmtime(p) >= cutoff
    )[:sample_cap]

    sample_size = len(all_paths)

    if sample_size == 0:
        return ClaimResult(
            claim_id=CLAIM_ID,
            role_scope=ROLE_SCOPE,
            sample_size=0,
            score=0.0,
            score_type="fraction",
            status="n/a",
            evidence="no transcripts found in window",
        )

    clean = 0
    last_violating: str = ""

    for p in all_paths:
        has_secret = _transcript_has_secret(Path(p))
        if has_secret:
            last_violating = Path(p).stem
        else:
            clean += 1

    score = clean / sample_size
    status = ClaimResult.classify_fraction(score, sample_size)

    if last_violating:
        evidence = (
            f"{clean}/{sample_size} transcripts clean; "
            f"last with raw secret pattern: {last_violating}"
        )
    else:
        evidence = f"all {sample_size} transcripts have zero raw-secret patterns"

    return ClaimResult(
        claim_id=CLAIM_ID,
        role_scope=ROLE_SCOPE,
        sample_size=sample_size,
        score=score,
        score_type="fraction",
        status=status,
        evidence=evidence,
        notes="healthy=100%; any match indicates scrubber bypass",
    )
