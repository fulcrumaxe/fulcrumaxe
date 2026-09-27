"""backend/stats/retro_skip.py — countable skip metric for the self-observe retro gate.

Before D#2532, when the self-observe gate (scripts/lib/self-observe-gate.sh,
backend/agent_retros.py) could not write a retro row, the only record of that
was a free-text `skip_reason` field inside the reporting agent's own
AGENT_OUTPUT envelope — visible only to whoever happened to read that one
agent's transcript. A retro corpus that is *selectively* empty (worktree-
isolated executors are the normal case, not the exception) is easy to miss
precisely because it isn't empty; nothing reported how often it was skipped,
or why.

This module makes that skip queryable outside any single agent's envelope,
via the existing stats.duckdb metric store (backend/stats_writer.py) rather
than the append-only audit log — `audit.jsonl` is deliberately off-limits to
worktree sub-agents (see the `_DIAL_PROTECTED_SUFFIXES` hardening in
hooks/sandbox_rules.py; several security-review rounds went into keeping it
Team-Lead-only, and reusing it here for a self-observe metric would quietly
re-open that).

Usage:
    python3 backend/agent_retros.py's own callers use this as a library
    (record_retro_skip), and scripts/lib/self-observe-gate.sh calls it
    directly for the "no transcript" case, where no python3 agent_retros.py
    invocation happens at all:

        python3 backend/stats/retro_skip.py --reason no_transcript --role executor

Query (example — "what fraction of executor runs wrote no retro, and why"
needs a denominator from elsewhere, e.g. agent-feed.jsonl's executor
agent_end count, same as scripts/spawn-hourly-stats.sh's impersonation_rate):

    SELECT json_extract_string(tags, '$.reason') AS reason, COUNT(*)
    FROM metric_event WHERE metric = 'retro_skip' GROUP BY 1;
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a script from the repo root: `python3 backend/stats/retro_skip.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def record_retro_skip(reason: str, role: str | None = None) -> None:
    """Record one "a retro row was not written" event, with its reason.

    Raises if stats_writer.record() itself raises (e.g. duckdb not
    installed) — callers that must not let a metrics call crash their own
    flow (backend/agent_retros.py's _record_skip) catch around this.
    """
    from backend import stats_writer  # noqa: PLC0415

    tags = {"reason": reason}
    if role:
        tags["role"] = role
    stats_writer.record(
        metric="retro_skip",
        value=1.0,
        unit="count",
        tags=tags,
        source="self-observe-gate",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record a self-observe retro-gate skip event"
    )
    parser.add_argument(
        "--reason", required=True, help="Why the retro row was not written"
    )
    parser.add_argument("--role", default=None, help="Agent role, e.g. executor")
    args = parser.parse_args()
    record_retro_skip(args.reason, role=args.role)
    return 0


if __name__ == "__main__":
    sys.exit(main())
