#!/usr/bin/env python3
"""hooks/fleet_unregister.py — SubagentStop counterpart to hooks/fleet_register.py.

D#2314 F1. Removes the fleet.db row a matching hooks/fleet_register.py
PreToolUse call created, so a finished `Agent()`-tool spawn stops reading
'active' on the dashboard.

Fires on every SubagentStop (matcher ""), same as scripts/subagent-stop-hook.sh
and hooks/subagent_stop_dial_audit.py. Purely observational: never raises,
never affects anything else this event does, always exits 0.

D#2314 S1 (security review — this was a real bug, now fixed): an earlier
version of this hook skipped unregistering unless
``hooks.sandbox_rules.classify_cwd(cwd) == "team_lead"``. That check is
correct for ``hooks/fleet_register.py`` (a ``PreToolUse`` hook, which runs in
the *caller's* context — the Team Lead's own top-level session, always
team_lead tier for a legitimate spawn). It is wrong here: ``SubagentStop``'s
``cwd`` is the *just-finished subagent's own* cwd
(``scripts/subagent-stop-hook.sh``'s ``CLAUDE_HOOK_CWD``,
``hooks/subagent_stop_dial_audit.py``'s docstring — both built on that same
contract), which for a worktree-isolated agent is a worktree path, not
team_lead. Gating on that tier meant unregister silently never fired for
any worktree-isolated spawn — the common case — leaking one immortal fleet
row (pid = the long-lived session PID, which never dies) per spawn until
the fleet-wide cap was exhausted and every future spawn was denied.

This hook does not need a cwd check at all: specificity comes from matching
on the resolved project name plus the parent session's own PID, not from
where the finished subagent happened to run.

No stable key survives from PreToolUse to SubagentStop (see
hooks/fleet_register.py's docstring for why), so this matches by what *is*
available at both ends: the resolved project name and the parent process's
own PID (`os.getppid()` — the same value fleet_register.py registered
under, since both hooks fire as children of the same long-running Team Lead
session, regardless of what cwd either payload reports). Among rows
matching those two things, the oldest (`agent-tool-*` prefix, earliest
`started_at`) is removed. Under concurrent `Agent()` spawns from the same
session this can remove the wrong one of several in-flight rows rather than
the one that actually just finished — a known, accepted imprecision, not a
correctness requirement this hook claims to meet. A wrongly-left-behind row
here is inert rather than harmful (D#2314 S2: `agent-tool-` rows never
consume a fleet-cap slot) and is eventually collected by `reap_stale()`'s
existing pid-liveness check once the session itself ends — see
hooks/fleet_register.py's docstring for why a separate age-based sweep was
tried and removed (finding N1) rather than kept as a tighter backstop here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _unregister_one() -> None:
    from backend.fleet.project_name import resolve_project_name  # noqa: PLC0415
    project_name = resolve_project_name(_REPO_ROOT)
    if not project_name:
        return

    from backend.fleet.concurrency import (  # noqa: PLC0415
        AGENT_TOOL_ID_PREFIX,
        list_agents,
        unregister,
    )
    pid = os.getppid()
    candidates = [
        row for row in list_agents()
        if row.get("project_name") == project_name
        and str(row.get("agent_id", "")).startswith(AGENT_TOOL_ID_PREFIX)
        and row.get("pid") == pid
    ]
    if not candidates:
        return
    oldest = min(candidates, key=lambda r: r.get("started_at", ""))
    unregister(project_name, oldest["agent_id"])


def main() -> None:
    try:
        raw = sys.stdin.read()
        json.loads(raw) if raw.strip() else {}  # validate only; payload unused
    except Exception:
        pass

    try:
        _unregister_one()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[fleet_unregister] WARNING: unregister skipped (non-fatal): {exc}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
