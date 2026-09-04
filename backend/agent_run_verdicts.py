"""
backend/agent_run_verdicts.py — verdict values that are NOT agent-reported.

`agent_run.verdict` holds two different kinds of value in the same column:
real outcomes an agent reported itself (`pass`, `needs-fix`, `done`, ...),
and placeholders the reconciler/sweeper writes in after the agent never
reported a verdict at all (a stale run, a superseded duplicate, a swept
test fixture). Both look like plain strings in that column, so a reader
that doesn't know the difference presents a placeholder as if it were a
real outcome.

This module is the single source of truth for which values are placeholders.
"""

from __future__ import annotations

NON_AGENT_VERDICTS: frozenset[str] = frozenset({
    "reconciled-stale",
    "superseded",
    "swept-test-fixture",
})


def is_agent_reported(verdict: str | None) -> bool:
    """True if *verdict* is a real agent-reported outcome.

    False for a missing verdict and for any value in NON_AGENT_VERDICTS —
    those are written by housekeeping code, not by the agent whose run the
    row describes.
    """
    if not verdict:
        return False
    return verdict not in NON_AGENT_VERDICTS
