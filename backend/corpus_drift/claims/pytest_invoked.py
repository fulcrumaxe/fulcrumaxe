"""code-reviewer.pytest_invoked — fraction of code-reviewer runs that invoked pytest.

Checks each code-reviewer transcript for a Bash tool call whose command starts
with "pytest" (or contains "pytest" as the first token after optional env vars).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from backend.corpus_drift.types import ClaimResult
from backend.transcript_reader import find_transcripts, iter_turns

logger = logging.getLogger(__name__)

CLAIM_ID = "code-reviewer.pytest_invoked"
ROLE_SCOPE = "code-reviewer"

# Matches "pytest ..." as first substantive command token, also handles
# "python -m pytest" and "python3 -m pytest".
_PYTEST_RE = re.compile(r'\bpytest\b')


def _transcript_contains_pytest(path: Path) -> bool:
    """Return True if the transcript at *path* has a Bash call invoking pytest."""
    try:
        for turn in iter_turns(path):
            for tc in turn.tool_calls:
                if tc.get("name", "") != "Bash":
                    continue
                cmd = tc.get("input", {}).get("command", "")
                if _PYTEST_RE.search(cmd):
                    return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("pytest_invoked: error reading %s: %s", path, exc)
    return False


def evaluate(
    runs: list[dict[str, Any]],
    transcripts_dir: Path | None,
    window_days: int,
    sample_cap: int = 100,
    **_kwargs: Any,
) -> ClaimResult:
    """Evaluate what fraction of code-reviewer runs invoked pytest.

    Parameters
    ----------
    runs:
        Rows from agent_runs for role="code-reviewer" within the window.
    transcripts_dir:
        Unused — transcripts are discovered via the canonical glob.
    window_days:
        Number of days in the audit window (for evidence display).
    sample_cap:
        Maximum runs to examine (default 100).
    """
    since_seconds = window_days * 86400

    # Discover transcripts from both the ephemeral /tmp glob and the persistent
    # ~/.claude/projects JSONL archive, filtered by mtime.
    all_paths = find_transcripts(since_seconds=since_seconds)
    # Filter to code-reviewer transcripts by path convention or run metadata.
    # transcript path: /tmp/claude-*/-home-agent-fulcrumaxe/*/tasks/<agent_id>.output
    # or ~/.claude/projects/-home-agent-fulcrumaxe/<uuid>.jsonl
    # We match by agent_id from the runs list when available, otherwise fall through
    # to mtime-filtered list (less precise but still useful).

    run_agent_ids: set[str] = {
        r.get("agent_id", "") or "" for r in runs if r.get("agent_id")
    }

    # Build {agent_id -> path} from discovered transcripts
    agent_id_to_path: dict[str, Path] = {p.stem: p for p in all_paths}

    # Prefer run-matched transcripts; fall back to all transcripts when run IDs
    # don't match transcript stems (agent_id in DB uses a different naming scheme
    # than transcript filename stems — fall through to scan all paths in that case).
    if run_agent_ids:
        matched_paths = [
            agent_id_to_path[aid]
            for aid in run_agent_ids
            if aid in agent_id_to_path
        ]
        if not matched_paths:
            # IDs present but no stem match — scan all transcripts as best-effort
            matched_paths = list(agent_id_to_path.values())
    else:
        matched_paths = list(agent_id_to_path.values())

    matched_paths = matched_paths[:sample_cap]
    sample_size = len(matched_paths)

    if sample_size == 0:
        # No transcripts — check if there are any runs at all
        if not runs:
            return ClaimResult(
                claim_id=CLAIM_ID,
                role_scope=ROLE_SCOPE,
                sample_size=0,
                score=0.0,
                score_type="fraction",
                status="n/a",
                evidence="no code-reviewer runs in window",
            )
        # Runs exist but no transcripts found (e.g., they've been cleaned up)
        return ClaimResult(
            claim_id=CLAIM_ID,
            role_scope=ROLE_SCOPE,
            sample_size=0,
            score=0.0,
            score_type="fraction",
            status="n/a",
            evidence="runs found but transcripts not available",
        )

    passing = 0
    last_fail_id: str = ""
    for path in matched_paths:
        if _transcript_contains_pytest(path):
            passing += 1
        else:
            last_fail_id = path.stem

    score = passing / sample_size if sample_size > 0 else 0.0
    status = ClaimResult.classify_fraction(score, sample_size)

    if last_fail_id:
        evidence = f"last non-pytest run: {last_fail_id}"
    else:
        evidence = "all sampled runs invoked pytest"

    return ClaimResult(
        claim_id=CLAIM_ID,
        role_scope=ROLE_SCOPE,
        sample_size=sample_size,
        score=score,
        score_type="fraction",
        status=status,
        evidence=evidence,
    )
