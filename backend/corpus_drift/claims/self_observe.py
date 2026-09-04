"""global.self_observe_present — fraction of AGENT_OUTPUT envelopes with self_observed field.

Scans transcripts for the AGENT_OUTPUT JSON envelope emitted in the final
assistant message.  Checks whether the envelope contains a "self_observed" key
(any value counts — shadow-mode baseline, not a pass/fail gate per Spec).

Source: Transcript AGENT_OUTPUT envelopes — extracted from assistant message text.

Note: agent_retros.jsonl rows are NOT used here.  Those rows record per-classifier
self-corrections (git_rm_usage, etc.) and the writer never sets a "self_observed"
field on them.  The canonical signal is the AGENT_OUTPUT envelope emitted by the
executor / code-reviewer templates, which includes "self_observed": true when the
self-observe gate was invoked during the run.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from backend.corpus_drift.types import ClaimResult
from backend.transcript_reader import TRANSCRIPT_GLOB, iter_turns

logger = logging.getLogger(__name__)

CLAIM_ID = "global.self_observe_present"
ROLE_SCOPE = "executor,code-reviewer"

# Matches the JSON block between <!-- AGENT_OUTPUT --> markers
_ENVELOPE_RE = re.compile(
    r'<!--\s*AGENT_OUTPUT\s*-->\s*```json\s*(\{.*?\})\s*```\s*<!--\s*/AGENT_OUTPUT\s*-->',
    re.DOTALL,
)


def _extract_envelope(text: str) -> dict | None:
    """Extract and parse the AGENT_OUTPUT JSON envelope from a message text."""
    m = _ENVELOPE_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _transcript_envelope_self_observed(path: Path) -> tuple[bool, bool]:
    """Return (has_envelope, has_self_observed) for the last AGENT_OUTPUT in transcript."""
    has_envelope = False
    has_self_observed = False
    try:
        for turn in iter_turns(path):
            if turn.role != "assistant":
                continue
            envelope = _extract_envelope(turn.text)
            if envelope is not None:
                has_envelope = True
                if "self_observed" in envelope:
                    has_self_observed = True
    except Exception as exc:  # noqa: BLE001
        logger.debug("self_observe: error reading %s: %s", path, exc)
    return has_envelope, has_self_observed


def evaluate(
    runs: list[dict[str, Any]],
    transcripts_dir: Path | None,
    window_days: int,
    sample_cap: int = 100,
    retros_path: Path | None = None,
    **_kwargs: Any,
) -> ClaimResult:
    """Evaluate what fraction of AGENT_OUTPUT envelopes include self_observed.

    Scans transcript files for AGENT_OUTPUT envelopes and checks whether each
    envelope contains a "self_observed" key.  The retros_path parameter is
    accepted for backward compatibility but is intentionally ignored — retro
    rows never carry a self_observed field (the writer doesn't set it), so
    counting them produced a spurious 0/N score.  The canonical source of
    truth is the envelope the executor / code-reviewer templates emit.

    Parameters
    ----------
    runs:
        Agent run rows (unused — transcripts are discovered via glob).
    transcripts_dir:
        Unused — canonical glob used for transcripts.
    window_days:
        Audit window in days.
    sample_cap:
        Max transcripts to scan.
    retros_path:
        Ignored.  Kept for API compatibility.
    """
    # Scan transcript envelopes — this is the only reliable source
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
            notes="shadow-mode baseline; not a pass/fail gate",
        )

    envelopes_found = 0
    self_observed_found = 0
    last_missing_id: str = ""

    for p in all_paths:
        has_env, has_so = _transcript_envelope_self_observed(Path(p))
        if has_env:
            envelopes_found += 1
            if has_so:
                self_observed_found += 1
            else:
                last_missing_id = Path(p).stem

    if envelopes_found == 0:
        return ClaimResult(
            claim_id=CLAIM_ID,
            role_scope=ROLE_SCOPE,
            sample_size=sample_size,
            score=0.0,
            score_type="fraction",
            status="n/a",
            evidence="no AGENT_OUTPUT envelopes found in transcripts",
            notes="shadow-mode baseline; not a pass/fail gate",
        )

    score = self_observed_found / envelopes_found
    status = ClaimResult.classify_fraction(score, envelopes_found)

    if last_missing_id:
        evidence = f"{self_observed_found}/{envelopes_found} envelopes have self_observed; last without: {last_missing_id}"
    else:
        evidence = f"all {envelopes_found} envelopes have self_observed field"

    return ClaimResult(
        claim_id=CLAIM_ID,
        role_scope=ROLE_SCOPE,
        sample_size=envelopes_found,
        score=score,
        score_type="fraction",
        status=status,
        evidence=evidence,
        notes="shadow-mode baseline; not a pass/fail gate",
    )
