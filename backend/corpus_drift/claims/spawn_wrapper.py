"""team-lead.spawn_wrapper_used — fraction of Agent() calls preceded by spawn-agent.sh.

For each Team Lead transcript, looks for Agent tool calls.  For each Agent call,
checks whether a Bash tool call containing "spawn-agent" or "spawn-agent.sh"
appeared within the N preceding tool-use turns in the same transcript.

Window: 5 tool-use turns before each Agent() call.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from backend.corpus_drift.types import ClaimResult
from backend.transcript_reader import find_transcripts, iter_turns

logger = logging.getLogger(__name__)

CLAIM_ID = "team-lead.spawn_wrapper_used"
ROLE_SCOPE = "team-lead"

# Matches the spawn-agent.sh wrapper call in a Bash command
_SPAWN_WRAPPER_RE = re.compile(r'\bspawn-agent\b')

# How many tool-use turns to look back for the wrapper call
_LOOKBACK_TURNS = 5


def _count_agent_calls_with_wrapper(path: Path) -> tuple[int, int, str]:
    """Return (calls_with_wrapper, total_agent_calls, last_uncovered_id).

    Scans the transcript for Agent() tool calls and checks if a spawn-agent.sh
    Bash call appeared within _LOOKBACK_TURNS turns before each one.
    """
    # Collect all tool calls in order: (turn_idx, tool_name, command_or_empty)
    all_tool_calls: list[tuple[int, str, str]] = []
    try:
        for turn in iter_turns(path):
            for tc in turn.tool_calls:
                name = tc.get("name", "")
                cmd = ""
                if name == "Bash":
                    cmd = tc.get("input", {}).get("command", "")
                all_tool_calls.append((turn.turn_idx, name, cmd))
    except Exception as exc:  # noqa: BLE001
        logger.debug("spawn_wrapper: error reading %s: %s", path, exc)
        return 0, 0, ""

    total_agent_calls = 0
    calls_with_wrapper = 0
    last_uncovered: str = ""

    for i, (idx, name, cmd) in enumerate(all_tool_calls):
        if name != "Agent":
            continue
        total_agent_calls += 1

        # Look back up to _LOOKBACK_TURNS tool calls for a spawn-agent.sh Bash call
        start = max(0, i - _LOOKBACK_TURNS)
        window = all_tool_calls[start:i]
        has_wrapper = any(
            tc_name == "Bash" and _SPAWN_WRAPPER_RE.search(tc_cmd)
            for _, tc_name, tc_cmd in window
        )
        if has_wrapper:
            calls_with_wrapper += 1
        else:
            last_uncovered = path.stem

    return calls_with_wrapper, total_agent_calls, last_uncovered


def evaluate(
    runs: list[dict[str, Any]],
    transcripts_dir: Path | None,
    window_days: int,
    sample_cap: int = 100,
    **_kwargs: Any,
) -> ClaimResult:
    """Evaluate fraction of Agent() calls preceded by spawn-agent.sh in Team Lead transcripts.

    Parameters
    ----------
    runs:
        Agent run rows for role="team-lead" in the window.
    transcripts_dir:
        Unused — canonical glob used.
    window_days:
        Audit window in days.
    sample_cap:
        Max transcripts to scan.
    """
    since_seconds = window_days * 86400

    # Discover transcripts from both the ephemeral /tmp glob and the persistent
    # ~/.claude/projects JSONL archive, filtered by mtime.
    all_paths = find_transcripts(since_seconds=since_seconds)

    # Prefer transcripts matched by run agent_ids
    run_agent_ids: set[str] = {
        r.get("agent_id", "") or "" for r in runs if r.get("agent_id")
    }
    agent_id_to_path: dict[str, Path] = {p.stem: p for p in all_paths}

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
        return ClaimResult(
            claim_id=CLAIM_ID,
            role_scope=ROLE_SCOPE,
            sample_size=0,
            score=0.0,
            score_type="fraction",
            status="n/a",
            evidence="no team-lead transcripts found in window",
        )

    total_agent_calls = 0
    total_with_wrapper = 0
    last_uncovered_id: str = ""

    for path in matched_paths:
        with_wrapper, total, last_uncov = _count_agent_calls_with_wrapper(path)
        total_agent_calls += total
        total_with_wrapper += with_wrapper
        if last_uncov:
            last_uncovered_id = last_uncov

    if total_agent_calls == 0:
        return ClaimResult(
            claim_id=CLAIM_ID,
            role_scope=ROLE_SCOPE,
            sample_size=sample_size,
            score=0.0,
            score_type="fraction",
            status="n/a",
            evidence="no Agent() calls found in sampled team-lead transcripts",
        )

    score = total_with_wrapper / total_agent_calls
    # Use agent call count as the effective sample for status classification
    status = ClaimResult.classify_fraction(score, total_agent_calls)

    if last_uncovered_id:
        evidence = f"{total_with_wrapper}/{total_agent_calls} Agent() calls had wrapper; last uncovered: {last_uncovered_id}"
    else:
        evidence = f"all {total_agent_calls} Agent() calls preceded by spawn-agent.sh"

    return ClaimResult(
        claim_id=CLAIM_ID,
        role_scope=ROLE_SCOPE,
        sample_size=total_agent_calls,
        score=score,
        score_type="fraction",
        status=status,
        evidence=evidence,
        notes=f"scanned {sample_size} team-lead transcripts",
    )
