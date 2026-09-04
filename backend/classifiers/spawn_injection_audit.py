"""backend/classifiers/spawn_injection_audit.py

Phase A.3 run-analyst classifier that audits each agent transcript's first
user-prompt turn for expected spawn-template injection markers.

D#544 added scripts/spawn-agent.sh as canonical spawn wrapper injecting:
  ## Voice, ## Working Principles, YOUR WORKTREE:, ## Self-Observe Gate

If a future code path bypasses spawn-agent.sh the injection silently drops.
This classifier makes that drop measurable by emitting a Finding per missing
marker, which the existing analyst_bug_filer pipeline (D#609) converts into a
[Bug] Discussion.

Usage (registered in backend/run_analyst.py _PHASE_A3_CLASSIFIERS):
    from backend.classifiers.spawn_injection_audit import classify_spawn_injection
"""

from __future__ import annotations

from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Import Finding from run_analyst at call-time to avoid circular imports.
# Classifiers are imported by run_analyst, so we resolve Finding lazily.

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPECTED_MARKERS: list[str] = [
    "## Voice",
    "## Working Principles",
    "YOUR WORKTREE:",
    "## Self-Observe Gate",
]

# Agent-id substrings that identify mechanical/infra roles — no spawn-template
# injection expected, so skip entirely.
MECHANICAL_ROLES: set[str] = {
    "reaper",
    "post-agent-hook",
    "post-merge-hook",
    "snapshot",
    "hook",
    "interactive-metrics",
    "watchdog",
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_spawn_injection(turns: "Iterable") -> list:
    """Return one Finding per missing injection marker in the first user turn.

    Args:
        turns: Iterable of TranscriptTurn (or compatible duck-type) from
               transcript_reader.iter_transcripts.

    Returns:
        list[Finding] — empty when all markers present, role is mechanical,
        or no user turn exists.
    """
    # Import Finding lazily to avoid circular import with run_analyst.
    try:
        from backend.run_analyst import Finding  # type: ignore[import]
    except ImportError:
        from run_analyst import Finding  # type: ignore[import]

    turns_list = list(turns)
    if not turns_list:
        return []

    # Identify the first user-role turn (carries the spawn prompt).
    first_user = next((t for t in turns_list if getattr(t, "role", "") == "user"), None)
    if first_user is None:
        return []

    # Derive agent_id: prefer an explicit attribute, fall back to empty string.
    agent_id: str = getattr(first_user, "agent_id", "") or ""

    # Skip mechanical/infra roles — they receive no spawn-template injection.
    if any(token in agent_id for token in MECHANICAL_ROLES):
        return []

    prompt: str = getattr(first_user, "text", "") or ""

    # Skip file-read directives at turn 0 (the new spawn style).
    # The current spawn-agent.sh sends "Read /tmp/<briefing>.txt" at turn 0;
    # injection markers arrive via the file content, not the inline user message.
    # Checking these always fires because the markers are never in the directive.
    if getattr(first_user, "turn_idx", -1) == 0 and prompt.startswith("Read "):
        return []

    findings = []
    for marker in EXPECTED_MARKERS:
        if marker not in prompt:
            findings.append(Finding(
                classifier="spawn_injection_audit",
                severity="medium",
                turn_index=first_user.turn_idx,
                detail=(
                    f"spawn prompt missing injection marker '{marker}'"
                    f" (agent_id={agent_id!r})"
                ),
            ))
    return findings
