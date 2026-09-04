"""backend/classifiers/gate_check_skipped.py

Phase A.8 run-analyst classifier: detect merge / label-apply actions in
agent transcripts where the security-review-passed label was absent.

Rationale: PRs #645, #650, #653 were merged after security-trigger keywords
appeared in the diff but without a security-review-passed gate check.  This
classifier makes that gap measurable.

Detection (transcript-level):
  - Scan Bash tool calls for merge triggers: `gh pr merge`, `gh api.*merge`.
  - A security gate is present if ANY of these appeared before the merge:
      * security-review-passed label check (`gh pr view.*label` or
        `gh api.*labels` containing security-review-passed)
      * security-trigger keyword in a label-apply command
      * ANY `--json labels` fetch (the team-lead auto-merge gate fetches labels
        before every merge to check code-review-passed / security state)
  - Flag if merge is reached with no preceding security gate check.

Severity: high (matches D#655 spec).
Auto-bug threshold: ≥1 in 24h (direct response to the 3 real cases today).

Usage (registered in backend/run_analyst.py _PHASE_A8_CLASSIFIERS):
    from backend.classifiers.gate_check_skipped import classify_gate_check_skipped
"""

from __future__ import annotations

import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_MERGE_PAT = re.compile(
    r"\bgh\s+pr\s+merge\b|\bgh\s+api\b.*\bmerge\b",
    re.IGNORECASE,
)

_SECURITY_GATE_PAT = re.compile(
    r"security.review.passed"
    r"|security.trigger"
    r"|security.review.triggered"
    r"|apply_label.*security"
    r"|gh\s+pr\s+view.*labels.*security"
    r"|gh\s+api.*labels.*security"
    # Widen: any --json labels fetch is the label-gate check the team-lead
    # auto-merge loop runs before every merge (checks code-review-passed and
    # security state).  Without this, every legitimate non-security auto-merge
    # fires a false positive.
    r"|--json\s+labels",
    re.IGNORECASE,
)

# Agent roles that legitimately auto-merge without security gates.
_SKIP_ROLE_TOKENS = frozenset({"reaper", "hook", "snapshot", "watchdog"})


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_gate_check_skipped(turns: "Iterable") -> list:
    """Return a Finding when gh pr merge runs without a prior security gate check.

    Args:
        turns: Iterable of TranscriptTurn (duck-type with .role, .tool_calls, .turn_idx).

    Returns:
        list[Finding] — one entry per merge-without-gate occurrence.
    """
    try:
        from backend.run_analyst import Finding  # type: ignore[import]
    except ImportError:
        from run_analyst import Finding  # type: ignore[import]

    turns_list = list(turns)
    if not turns_list:
        return []

    # Check agent_id on first user turn for mechanical role skip.
    first_user = next((t for t in turns_list if getattr(t, "role", "") == "user"), None)
    if first_user is not None:
        agent_id: str = getattr(first_user, "agent_id", "") or ""
        if any(tok in agent_id for tok in _SKIP_ROLE_TOKENS):
            return []

    findings = []
    saw_security_gate = False

    for t in turns_list:
        for tc in t.tool_calls:
            if tc.get("name") != "Bash":
                continue
            cmd = tc.get("input", {}).get("command", "")
            if _SECURITY_GATE_PAT.search(cmd):
                saw_security_gate = True
            elif _MERGE_PAT.search(cmd) and not saw_security_gate:
                findings.append(Finding(
                    classifier="gate_check_skipped",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=(
                        f"gh pr merge reached without security-review-passed gate check: "
                        f"{cmd[:120]}"
                    ),
                ))
                # Flag once per transcript — one finding is enough.
                return findings

    return findings
