#!/usr/bin/env python3
"""hooks/fleet_register.py — PreToolUse(Agent) fleet.db registration coverage.

D#2314 finding F1: the `Agent()` tool path (the Team Lead's own orchestration
path, per CLAUDE.md's single-spawner invariant) never registers anything in
fleet.db. `scripts/pre-spawn-check.sh` (which does register) only runs for
`scripts/spawn-agent.sh`. Measured: with a real `Agent()`-spawned agent
running, `fleet.db`'s `agents` table had 0 rows. A liveness probe rewritten
to read fleet.db would therefore still read `idle` on a day the team merged
43 PRs — the exact original bug, reproduced in a new mechanism.

This hook closes that gap for the `Agent()` path specifically. It is
**observe-only**: it never blocks a tool call, and every failure mode
(unresolvable project name, unwritable fleet dir, fleet cap already
exceeded) is silently absorbed. Registration correctness is advisory to the
dashboard's liveness display; it must never become a second gate a spawn has
to pass -- see D#2314 S2 below for how that promise is actually kept, not
just asserted.

Correlating this registration with the eventual SubagentStop event
(hooks/fleet_unregister.py) is inherently approximate: Claude Code's
PreToolUse hook fires *before* the spawned agent exists, so there is no
child-process PID or transcript path to key on yet. This hook therefore:

  - generates its own opaque agent_id (``agent-tool-<hex>``, the
    ``AGENT_TOOL_ID_PREFIX`` constant in backend/fleet/concurrency.py) --
    there is nothing from Claude Code to reuse as a stable key across the
    two hooks;
  - registers the PID of *this hook's parent process* (``os.getppid()``) --
    the long-running Team Lead session, not this short-lived hook script
    (which would look dead on the very next poll) and not the not-yet-
    existing child;
  - relies on hooks/fleet_unregister.py's SubagentStop counterpart to
    remove the row on the common path.

D#2314 S1/S2/S4 (security review): registering the session's own long-lived
PID means pid-liveness reaping can never collect a row on its own while the
session is still alive -- but that's fine now, for a reason worth being
explicit about, because an earlier fix here was itself a bug (finding N1).

  1. ``register()`` and ``count_project_capped()`` in
     backend/fleet/concurrency.py exclude ``agent-tool-`` rows from every
     cap-check COUNT(*) -- these rows can never consume a spawn-agent.sh-lane
     concurrency slot, however many pile up. A leaked row is therefore inert,
     not a fleet-cap-exhausting problem.
  2. A missed ``hooks/fleet_unregister.py`` call (crash, hook error, race)
     is bounded by ``reap_stale()``'s existing pid-liveness check, which
     already runs on every ``register()`` call and collects these rows once
     the session pid actually dies -- verified empirically. An earlier
     version of this hook also called a dedicated
     ``sweep_stale_agent_tool_rows()`` that deleted on age alone, with no
     liveness condition. That is wrong on its own terms (it deletes a row
     for an agent that is still running, just an old one -- and CLAUDE.md
     defines persistent agents like project-manager and visual-verifier, so
     "old" is not rare) and wrong in composition: it can delete agent A's
     live row, after which agent B's later ``SubagentStop`` call evicts
     the oldest *remaining* match -- now B's own row -- leaving
     ``active_agents()`` report nothing while B is still running. That is
     D#2314's "idle while working" symptom, rebuilt inside its own fix. It
     was removed rather than given a liveness condition, because with (1)
     in place it wasn't fixing anything a leaked row could still do.

Registers only when this hook's own cwd classifies as "team_lead"
(hooks/sandbox_rules.classify_cwd) — the same tier the single-spawner
invariant already requires for `Agent()` to be allowed at all. A spawn
attempt from any other tier is sandbox.py's problem, not this hook's. Note
this tier check applies ONLY here (the calling context at PreToolUse-time);
hooks/fleet_unregister.py's SubagentStop fires with the *finished subagent's
own* cwd (typically a worktree), not the caller's, so it does not reuse this
same guard — see that file's docstring.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hooks.sandbox_rules import classify_cwd  # noqa: E402


def _register(cwd: str, tool_input: dict) -> None:
    if classify_cwd(cwd) != "team_lead":
        return

    from backend.fleet.project_name import resolve_project_name  # noqa: PLC0415
    project_name = resolve_project_name(_REPO_ROOT)
    if not project_name:
        return

    from backend.fleet.concurrency import AGENT_TOOL_ID_PREFIX, register  # noqa: PLC0415

    role = str(tool_input.get("subagent_type") or "unknown")
    agent_id = f"{AGENT_TOOL_ID_PREFIX}{uuid.uuid4().hex[:16]}"
    # Return value (False on fleet-cap-exceeded) is intentionally ignored —
    # this registration is observe-only and must never influence whether the
    # spawn proceeds (D#2314 Spec item 11). It also can't actually be denied
    # by the real spawn-agent.sh-lane cap: agent-tool- rows are excluded from
    # that count (D#2314 S2, see register()'s own cap-check query).
    register(project_name, agent_id, role, pid=os.getppid())


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    try:
        if payload.get("tool_name", "") != "Agent":
            return
        cwd = payload.get("cwd") or os.environ.get("CLAUDE_HOOK_CWD") or os.getcwd()
        tool_input = payload.get("tool_input") or {}
        _register(cwd, tool_input)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[fleet_register] WARNING: registration skipped (non-fatal): {exc}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    # Always allow — this hook only ever observes, never blocks.
    sys.exit(0)
