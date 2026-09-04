"""backend/classifiers/stale_rebase_warning.py

Phase A.8 run-analyst classifier: detect executor transcripts where git push
is reached without a preceding rebase against origin/main.

Rationale: executor agents in worktrees that skip `git rebase origin/main`
before pushing create stale-base branches.  When those branches are merged the
PR race causes repeated "needs rebase" cycles.

Detection (transcript-level proxy):
  - Scan Bash tool calls for git push (git push / git push -u origin HEAD).
  - A rebase is present if git rebase origin/<main|master|HEAD> OR
    git pull --rebase appeared before the push.
  - Flag if push is reached with no preceding rebase.

Excluded from detection:
  - Branch deletion pushes: `git push origin :old-branch`
  - Tag pushes: `git push origin v1.2` (version tags)

Severity: high (matches D#655 spec).
Auto-bug threshold: ≥3 in 24h (handled by analyst_bug_filer pipeline).

Usage (registered in backend/run_analyst.py _PHASE_A8_CLASSIFIERS):
    from backend.classifiers.stale_rebase_warning import classify_stale_rebase_warning
"""

from __future__ import annotations

import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_GIT_PUSH_PAT = re.compile(
    r"\bgit\s+push\b",
    re.IGNORECASE,
)

# Exclude branch deletion form: `git push origin :old-branch` (space + colon)
# and tag pushes: `git push origin v1.2` or `git push --tags`.
_GIT_PUSH_EXCLUDE_PAT = re.compile(
    r"\bgit\s+push\b.*\s:[^\s]"        # delete form: origin :ref
    r"|\bgit\s+push\b.*\sv\d"          # tag push: origin v1.2
    r"|\bgit\s+push\b.*--tags\b",      # explicit --tags flag
    re.IGNORECASE,
)

_REBASE_PAT = re.compile(
    r"\bgit\s+rebase\b|\bgit\s+pull\s+.*--rebase\b",
    re.IGNORECASE,
)

# Skip these roles — they never push feature branches.
_SKIP_ROLE_TOKENS = frozenset({"reaper", "hook", "snapshot", "watchdog"})


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_stale_rebase_warning(turns: "Iterable") -> list:
    """Return a Finding when git push is reached without prior git rebase.

    Args:
        turns: Iterable of TranscriptTurn (duck-type with .role, .tool_calls, .turn_idx).

    Returns:
        list[Finding] — one entry per push-without-rebase occurrence.
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
    saw_rebase = False

    for t in turns_list:
        for tc in t.tool_calls:
            if tc.get("name") != "Bash":
                continue
            cmd = tc.get("input", {}).get("command", "")
            if _REBASE_PAT.search(cmd):
                saw_rebase = True
            elif (
                _GIT_PUSH_PAT.search(cmd)
                and not _GIT_PUSH_EXCLUDE_PAT.search(cmd)
                and not saw_rebase
            ):
                findings.append(Finding(
                    classifier="stale_rebase_warning",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=(
                        f"git push reached without prior git rebase origin/main: "
                        f"{cmd[:120]}"
                    ),
                ))
                # Flag once per transcript — one finding is enough.
                return findings

    return findings
