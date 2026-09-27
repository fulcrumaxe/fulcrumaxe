"""backend/spawn_registry.py — one append-only row per spawn (D#2615 ENG-0 C2).

Why this exists: the Discussion outbox (backend/discussion_outbox.py) needs to
know which role and Discussion a *stopping* spawn belongs to, without trusting
anything the agent itself wrote (its final message, its AGENT_OUTPUT envelope,
a file in its worktree — see that module's docstring for the full threat
model). The one thing available on both ends is a join key the *calling*
process writes before the agent ever starts: `scripts/spawn-agent.sh` already
generates a per-spawn `EVENT_ID` (`<role>-<discussion-or-nod>-<unix-ts>`)
before it assembles the prompt, and that same id is embedded as the literal
`hook_event_id=<id>` trailer line of the prompt Team Lead passes to `Agent()`
— see `backend/prompt_builder.py`. `scripts/spawn-agent.sh` records that
mapping here, in the same step that already writes `agent_run_tracker`'s
`start` row, so the identity is on record before the spawn can do anything at
all.

No PostToolUse recorder is needed to close the loop: the hook recovers the
same event id from the subagent's own transcript's first message (which is
the assembled prompt Team Lead passed to `Agent()`, and therefore something
the calling process wrote, not the agent) rather than from anything the agent
produced afterward. See `backend.discussion_outbox._extract_join_key`.

Storage: a single append-only JSONL file under STATE_DIR, resolved fresh on
every access via `backend.state_paths` (never bind `state_paths.STATE_DIR` to
a module-level name — see that module's own docstring for why). Append-only
and unindexed on purpose: a spawn registry entry is read at most once (by the
one SubagentStop event for that spawn) and this file is not a database this
project wants to maintain. A duplicate event-id (a bug, a retried write)
naturally surfaces as more than one matching row — `find_by_event_id` returns
every match rather than silently picking one, and the caller in
`discussion_outbox.py` treats more than one match as `ambiguous_registry`
(fail closed) rather than a coin flip.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend import state_paths

_REGISTRY_FILENAME = "spawn-registry.jsonl"


def _registry_path() -> Path:
    return Path(state_paths.STATE_DIR) / _REGISTRY_FILENAME


def record_spawn(
    event_id: str,
    role: str,
    discussion: Optional[int],
    created_at: str,
) -> None:
    """Append one registry row.

    Called by `scripts/spawn-agent.sh` at (or before) spawn time — i.e.
    before the Team Lead's `Agent()` call that actually starts the agent, so
    the row exists no matter how quickly the new spawn stops. Raises on a
    genuine I/O failure (a full disk, an unwritable STATE_DIR) rather than
    swallowing it: the shell caller already wraps this call non-fatally
    (`|| true`), and tests that call this directly want a real failure to
    surface, not a silently-empty registry.
    """
    state_paths.ensure_state_dir()
    row = {
        "event_id": event_id,
        "role": role,
        "discussion": discussion,
        "created_at": created_at,
    }
    with open(_registry_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def find_by_event_id(event_id: str) -> list[dict]:
    """Return every registry row whose `event_id` matches, in file order.

    Empty list when the registry file doesn't exist, is unreadable, or has
    no match — never raises. The caller decides what "no match" and "more
    than one match" each mean (see `discussion_outbox._resolve_identity`);
    this function just reports what's on disk.
    """
    path = _registry_path()
    if not path.exists():
        return []
    matches: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("event_id") == event_id:
                    matches.append(row)
    except OSError:
        return []
    return matches


def _cli(argv: list[str]) -> int:
    """`python3 -m backend.spawn_registry record <event_id> <role> [discussion]`

    Wraps `record_spawn` with the exception-to-stderr-WARN handling that used
    to be inlined as a `python3 -c "..."` block in `scripts/spawn-agent.sh`
    (D#2615 fix round 1: that inline block alone put the shell diff over the
    Spec's line cap for that file). Always returns 0 — a registry-write
    failure must never fail the spawn that's recording it; the WARN on
    stderr is the signal (see CHEAP HARDENING: it must not be redirected to
    /dev/null by the caller).
    """
    if len(argv) < 3 or argv[0] != "record":
        print("usage: python3 -m backend.spawn_registry record <event_id> <role> [discussion]", file=sys.stderr)
        return 0
    _, event_id, role, *rest = argv
    discussion = int(rest[0]) if rest and rest[0] else None
    try:
        record_spawn(event_id, role, discussion, datetime.now(timezone.utc).isoformat())
    except Exception as e:
        print(f"[spawn-agent] WARN: spawn registry record failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
