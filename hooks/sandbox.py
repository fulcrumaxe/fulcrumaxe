#!/usr/bin/env python3
"""hooks/sandbox.py

PreToolUse hook — sub-agent filesystem and merge-action sandbox.

Reads a JSON tool-call object from stdin:
  {"tool_name": "Bash"|"Edit"|"Write", "tool_input": {...}, "cwd": "..."}

Exits 0 (allow) or 2 (block, Claude Code surfaces stderr as the rejection message).

Telemetry: every decision is appended as a JSON line to
  .autonomous-team/hook-events/blocks-YYYY-MM-DD.jsonl
  Allow decisions are sampled at 10%; blocks are always written.

Install:
  bash scripts/install-sandbox-hook.sh
"""

from __future__ import annotations

import json
import os
import random
import sys
import traceback
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: make hooks/ importable when invoked directly as a script
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hooks.sandbox_rules import (  # noqa: E402
    Decision,
    _GH_API_GRAPHQL_MUTATION,
    _extract_graphql_mutation_names,
    check_claude_spawn,
    classify_agent_spawn,
    classify_bash,
    classify_cwd,
    classify_git_rm,
    classify_path_write,
    is_foreign_self_governed,
    is_real_git_rm_invocation,
    is_worktree,
)
from hooks.background_rules import classify_background  # noqa: E402

# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

_TELEMETRY_DIR = _REPO_ROOT / ".autonomous-team" / "hook-events"
_ALLOW_SAMPLE_RATE = 0.10  # 10% of allow decisions are logged


def _write_telemetry(
    tool: str,
    decision: Decision,
    cwd: str,
    command_or_path: str,
    worktree_id: str | None,
) -> None:
    """Append one JSON line to the daily telemetry file.

    Never raises — telemetry failure must not affect the hook decision.
    """
    try:
        if decision.allow and random.random() > _ALLOW_SAMPLE_RATE:
            return  # Skip most allow decisions to limit volume

        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"blocks-{date.today().isoformat()}.jsonl"

        entry = {
            "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool": tool,
            "decision": "allow" if decision.allow else "block",
            "reason": decision.reason,
            "cwd": cwd,
            "command_or_path": command_or_path[:500],  # truncate long commands
            "worktree_id": worktree_id,
        }
        with open(log_file, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Swallow all telemetry errors


# ---------------------------------------------------------------------------
# Main hook logic
# ---------------------------------------------------------------------------


def _allow(tool: str, cwd: str, command_or_path: str, worktree_id: str | None) -> None:
    """Exit 0 — tool call is allowed."""
    _write_telemetry(tool, Decision(allow=True, reason=""), cwd, command_or_path, worktree_id)
    sys.exit(0)


def _block(tool: str, cwd: str, command_or_path: str, worktree_id: str | None, reason: str) -> None:
    """Exit 2 — tool call is blocked.  Claude Code surfaces stderr as the rejection message.

    D#2246: the old message ended with "Do not retry this operation" and
    nothing else — sound advice against respelling the same call, but no
    help for an agent that hit this after its cwd had drifted outside the
    worktree, since every absolute path it might use to get back is itself
    a path token. `Recovery:` below names the two things that actually work.
    """
    _write_telemetry(tool, Decision(allow=False, reason=reason), cwd, command_or_path, worktree_id)
    full_msg = (
        f"blocked by sandbox: {reason}\n"
        f"tool={tool} cwd={cwd}\n"
        "Recovery: this worktree is writable; the parent checkout is not. Put scratch files "
        "under /tmp (T=$(mktemp -d)), and use a relative `cd ../../..` to reach the main "
        "checkout. Re-running the same operation with different quoting or a different tool "
        "will not change the answer — if you believe this is a false positive, stop and "
        "report it in your AGENT_OUTPUT envelope with block_reason.\n"
    )
    sys.stderr.write(full_msg)
    sys.exit(2)


def _write_claude_spawn_block_event(
    command: str,
    reason: str,
    cwd: str,
    worktree_id: str | None,
) -> None:
    """Write a structured block event for a claude_spawn_forbidden denial.

    Fields: block_reason, command, matched_pattern, cwd, worktree_id, timestamp.
    Written to the same daily blocks-*.jsonl file as regular telemetry so the
    dashboard can surface it within one loop iteration (D#439 AC3).

    Never raises — event write failure must not affect the hook decision.
    """
    try:
        import datetime

        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"blocks-{date.today().isoformat()}.jsonl"

        # Extract matched_pattern from reason string "claude_spawn_forbidden: matched pattern 'X'"
        matched_pattern = ""
        if "matched pattern" in reason:
            try:
                matched_pattern = reason.split("matched pattern")[-1].strip().strip("'\"")
            except Exception:
                matched_pattern = reason

        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool": "Bash",
            "decision": "block",
            "block_reason": "claude_spawn_forbidden",
            "reason": reason,
            "command": command[:500],
            "matched_pattern": matched_pattern,
            "cwd": cwd,
            "worktree_id": worktree_id,
        }
        with open(log_file, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Swallow all telemetry errors


def _write_agent_spawn_block_event(
    cwd: str,
    worktree_id: str | None,
    reason: str,
    args: dict,
) -> None:
    """Write a structured audit row for a blocked Agent() spawn attempt.

    kind: "sandbox_block_agent_spawn" — cwd + attempted target task/prompt.
    Never raises.
    """
    try:
        import datetime

        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"blocks-{date.today().isoformat()}.jsonl"

        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool": "Agent",
            "kind": "sandbox_block_agent_spawn",
            "decision": "block",
            "reason": reason,
            "cwd": cwd,
            "worktree_id": worktree_id,
            "attempted_target": str(args.get("prompt", args.get("task_prompt", "")))[:300],
        }
        with open(log_file, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _write_gh_api_mutation_block_event(
    cwd: str,
    worktree_id: str | None,
    command: str,
) -> None:
    """Write a structured audit row for a blocked gh api mutation attempt.

    kind: "sandbox_block_gh_api_mutation" — cwd + attempted call (truncated).
    Never raises.
    """
    try:
        import datetime

        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"blocks-{date.today().isoformat()}.jsonl"

        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool": "Bash",
            "kind": "sandbox_block_gh_api_mutation",
            "decision": "block",
            "cwd": cwd,
            "worktree_id": worktree_id,
            "attempted_call": command[:500],
        }
        with open(log_file, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _write_gh_api_mutation_allow_event(
    cwd: str,
    worktree_id: str | None,
    mutation_names: list[str],
) -> None:
    """Write a structured audit row when an allowlisted GraphQL mutation is permitted.

    kind: "sandbox_allow_graphql_mutation" — cwd + mutation_names + role.
    Written to both the daily hook-events file and <state_dir>/audit.jsonl.
    Never raises.
    """
    try:
        import datetime

        # Derive role from WORKTREE_ID env var (e.g. "executor-1148-p0-xyz")
        role = os.environ.get("WORKTREE_ID", worktree_id or "unknown")

        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool": "Bash",
            "kind": "sandbox_allow_graphql_mutation",
            "decision": "allow",
            "cwd": cwd,
            "mutation_names": mutation_names,
            "role": role,
        }
        line = json.dumps(entry) + "\n"

        # Write to daily hook-events file (consistent with other sandbox events)
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"blocks-{date.today().isoformat()}.jsonl"
        with open(log_file, "a") as fh:
            fh.write(line)

        # Also write to state-dir audit.jsonl for cross-subsystem observability
        state_dir = Path(
            os.environ.get(
                "AUTONOMOUS_TEAM_STATE_DIR",
                str(Path.home() / ".fulcrumaxe-state"),
            )
        )
        audit_log = state_dir / "audit.jsonl"
        if state_dir.exists():
            with open(audit_log, "a") as fh:
                fh.write(line)
    except Exception:
        pass


def _write_foreign_defer_event(
    cwd: str,
    tool: str,
    worktree_id: str | None,
) -> None:
    """Audit row when the af sandbox DEFERS to a foreign self-governed team.

    kind: "sandbox_foreign_self_governed_defer" — the af hook fires inside a
    sibling team's session (e.g. lafk-demo) and allows the call so that team's
    own session can spawn/mutate. Written to the daily hook-events file only
    (a foreign cwd has no af state-dir relevance). Never raises.
    """
    try:
        import datetime

        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool": tool,
            "kind": "sandbox_foreign_self_governed_defer",
            "decision": "allow",
            "cwd": cwd,
            "worktree_id": worktree_id,
        }
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"blocks-{date.today().isoformat()}.jsonl"
        with open(log_file, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _write_archive_protocol_warning_event(
    cwd: str,
    command: str,
) -> None:
    """Write a structured warning audit row when the Team Lead issues a git rm.

    kind: "archive_protocol_warning" — records the violation in the daily
    hook-events file and in <state_dir>/audit.jsonl so the drift scanner and
    dashboard can surface it.  The command is still ALLOWED (exit 0); this
    write is purely for observability / discipline enforcement.

    Fields: ts, tool, kind, decision, cwd, tier, command (truncated to 500).
    Never raises — telemetry failure must not block the tool call.
    """
    try:
        import datetime

        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool": "Bash",
            "kind": "archive_protocol_warning",
            "decision": "warn",
            "tier": "team_lead",
            "cwd": cwd,
            "command": command[:500],
        }
        line = json.dumps(entry) + "\n"

        # Write to daily hook-events file
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"blocks-{date.today().isoformat()}.jsonl"
        with open(log_file, "a") as fh:
            fh.write(line)

        # Also write to state-dir audit.jsonl for cross-subsystem observability
        state_dir = Path(
            os.environ.get(
                "AUTONOMOUS_TEAM_STATE_DIR",
                str(Path.home() / ".fulcrumaxe-state"),
            )
        )
        audit_log = state_dir / "audit.jsonl"
        if state_dir.exists():
            with open(audit_log, "a") as fh:
                fh.write(line)
    except Exception:
        pass  # Swallow all telemetry errors


def main() -> None:
    # 1. Parse stdin
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as exc:
        # If we can't parse the input, allow (fail open) but log the parse error.
        sys.stderr.write(f"[sandbox] WARNING: could not parse hook input: {exc}\n")
        sys.exit(0)

    tool_name: str = payload.get("tool_name", "")
    tool_input: dict = payload.get("tool_input", {})
    cwd: str = payload.get("cwd", os.getcwd())

    # 2. Determine context tier from CWD
    cwd_tier = classify_cwd(cwd)
    worktree_id = is_worktree(cwd)

    # 2b. Foreign self-governed team defer. This hook is registered globally
    #     (~/.claude) so it also fires inside sibling autonomous-team sessions
    #     (e.g. learningappforkids at <home>/lafk-demo). Those teams govern
    #     themselves; without this defer their cwd classifies as "untrusted" and
    #     every spawn/mutation in their OWN session is blocked. Allow + audit so
    #     the sibling team's session can operate. Worktrees and our own repo are
    #     excluded inside is_foreign_self_governed, so this cannot relax af's own
    #     sub-agent sandbox. See feedback_global_sandbox_multi_team_defer.
    if is_foreign_self_governed(cwd):
        _write_foreign_defer_event(cwd, tool_name, worktree_id)
        _allow(tool_name, cwd, str(tool_input), worktree_id)
        return

    # 3. Team Lead exemption: if tier is "team_lead", allow everything — but first
    #    check for `git rm` Bash calls and emit an archive_protocol_warning audit row
    #    + stderr warning so violations are observable even though they are not blocked.
    #    The Team Lead needs full git access (merges, branch ops, etc.), so we NEVER
    #    hard-block this tier — warn+audit is the enforcement mechanism here.
    if cwd_tier == "team_lead":
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            if is_real_git_rm_invocation(command, exempt_cached=True):
                _write_archive_protocol_warning_event(cwd=cwd, command=command)
                sys.stderr.write(
                    "[sandbox] WARNING: git rm in main repo violates archive protocol"
                    " — use git mv to archive/<name>-<date>/ instead. "
                    "See CLAUDE.md Archive Protocol.\n"
                )
        _allow(tool_name, cwd, str(tool_input), worktree_id)
        return  # unreachable after sys.exit, but keeps type-checker happy

    # 3b. Agent() tool — block from any non-team-lead context.
    if tool_name == "Agent":
        agent_decision = classify_agent_spawn(cwd, tool_input)
        if not agent_decision.allow:
            _write_agent_spawn_block_event(
                cwd=cwd,
                worktree_id=worktree_id,
                reason=agent_decision.reason,
                args=tool_input,
            )
            _block(tool_name, cwd, str(tool_input)[:300], worktree_id, agent_decision.reason)
        else:
            _allow(tool_name, cwd, str(tool_input), worktree_id)
        return

    # 4. For non-team-lead tiers (worktree or untrusted), route by tool type.
    if tool_name == "Bash":
        command = tool_input.get("command", "")

        bg_decision = classify_background(tool_input)  # D#2070
        if not bg_decision.allow:
            _block(tool_name, cwd, command, worktree_id, bg_decision.reason)

        # 4a. claude-spawn deny-list check (D#439) — runs before classify_bash.
        spawn_decision = check_claude_spawn([], command)
        if not spawn_decision.allow:
            _write_claude_spawn_block_event(
                command=command,
                reason=spawn_decision.reason,
                cwd=cwd,
                worktree_id=worktree_id,
            )
            _block(tool_name, cwd, command, worktree_id, spawn_decision.reason)
            return  # unreachable

        decision = classify_bash(command, cwd)
        if not decision.allow:
            # Emit dedicated audit row for gh api mutation blocks.
            if "sandbox_block_gh_api_mutation" in decision.reason:
                _write_gh_api_mutation_block_event(cwd=cwd, worktree_id=worktree_id, command=command)
            _block(tool_name, cwd, command, worktree_id, decision.reason)
        else:
            # If this is an allowlisted GraphQL mutation, emit an audit allow row.
            if _GH_API_GRAPHQL_MUTATION.search(command):
                names = _extract_graphql_mutation_names(command)
                if names:
                    _write_gh_api_mutation_allow_event(
                        cwd=cwd, worktree_id=worktree_id, mutation_names=names
                    )
            _allow(tool_name, cwd, command, worktree_id)

    elif tool_name in ("Edit", "Write"):
        # For untrusted CWD, block all Edit/Write (classify_path_write only checks
        # worktree-relative paths; untrusted paths have no worktree root to compare).
        if cwd_tier == "untrusted":
            _block(tool_name, cwd, tool_input.get("file_path", ""), worktree_id,
                   "untrusted cwd: all mutations are blocked outside repo and worktree paths")
            return
        file_path = tool_input.get("file_path", "")
        decision = classify_path_write(file_path, cwd)
        if not decision.allow:
            _block(tool_name, cwd, file_path, worktree_id, decision.reason)
        else:
            _allow(tool_name, cwd, file_path, worktree_id)

    else:
        # Unknown tool — allow (hook is registered only for Bash/Edit/Write/Agent)
        _allow(tool_name, cwd, str(tool_input), worktree_id)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Unexpected error — fail open, log to stderr
        sys.stderr.write(f"[sandbox] INTERNAL ERROR:\n{traceback.format_exc()}\n")
        sys.exit(0)
