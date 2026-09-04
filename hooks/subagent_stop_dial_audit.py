#!/usr/bin/env python3
"""hooks/subagent_stop_dial_audit.py

SubagentStop post-hook: defense-in-depth audit for Agent() calls that
slipped past the PreToolUse sandbox.

When a subagent finishes, Claude Code invokes this hook with a JSON payload
on stdin.  We scan the just-finished transcript for `Agent` tool_use entries
and emit `sandbox_block_agent_spawn` audit rows for any that occurred while
the parent agent was running from a worktree CWD.

Why this exists
---------------
The PreToolUse hook fires *before* a tool call is executed and can block it.
This hook runs *after* the subagent stops and cannot block anything — it is
purely an audit/detection layer.  If an Agent() call appears here that we
expected to block, it means the PreToolUse matcher silently failed to fire
(e.g. the harness was updated and the hook format changed).  The audit row
gives operators visibility so they can investigate and fix the gap.

Environment variables (set by Claude Code on hook invocation)
-------------------------------------------------------------
CLAUDE_SUBAGENT_TRANSCRIPT_PATH   — path to the completed subagent's JSONL
                                    transcript file (may be absent on older
                                    harness versions; we degrade gracefully).
CLAUDE_HOOK_CWD                   — CWD the subagent was launched from.

Input (stdin)
-------------
JSON object with at minimum:
  { "cwd": "<str>", "transcript_path": "<str>" }

Both `CLAUDE_HOOK_CWD` and the stdin `cwd` field are checked; whichever is
present is used.

Output
------
Appends one JSON line per suspect Agent() call to the blocks-YYYY-MM-DD.jsonl
audit file (same format as the PreToolUse hook blocks).

Exit code is always 0 — this hook never blocks execution.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks.repo_root import resolve_main_repo_root  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Resolved, not written down. This was a literal absolute path until D#1997,
# and on any machine where that path did not exist the hook wrote its audit
# rows into a directory nobody was reading and matched worktree prefixes that
# could never match — silently, because the hook is documented to always exit
# 0 and so had no way to complain. hooks/repo_root.py is the hooks-side
# resolver (deliberately separate from backend/repo_root.py, which imports
# subprocess; sandbox_rules.py must not) and honours SANDBOX_MAIN_REPO_ROOT so
# the shell tests can pin it to a synthetic root.
_REPO_ROOT = resolve_main_repo_root()
_HOOK_EVENTS_DIR = _REPO_ROOT / ".autonomous-team" / "hook-events"

# Worktree prefix patterns — must stay in sync with sandbox_rules.py.
_WORKTREE_PREFIXES = [
    str(_REPO_ROOT / ".claude" / "worktrees") + "/",
    "/tmp/wt-",
]


def _is_worktree_path(path: str) -> bool:
    """Return True if *path* looks like a sub-agent worktree CWD."""
    try:
        resolved = str(Path(path).resolve())
    except Exception:
        resolved = path
    return any(resolved.startswith(prefix) for prefix in _WORKTREE_PREFIXES)


def _blocks_file() -> Path:
    today = datetime.date.today().isoformat()
    return _HOOK_EVENTS_DIR / f"blocks-{today}.jsonl"


def _append_audit_row(row: dict) -> None:
    """Append a single JSON audit row to today's blocks file."""
    _HOOK_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    blocks_path = _blocks_file()
    with open(blocks_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _scan_transcript(transcript_path: str) -> list[dict]:
    """Return a list of Agent tool_use entries found in *transcript_path*.

    Each returned dict has keys: `tool_use_id`, `input` (the tool input dict).
    Returns [] if the file is absent, unreadable, or contains no Agent entries.
    """
    try:
        p = Path(transcript_path)
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    agent_entries: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # JSONL transcripts may contain message objects with a `content` array.
        # Each content item with `type == "tool_use"` and `name == "Agent"` is a spawn attempt.
        content = event.get("content") if isinstance(event, dict) else None
        if isinstance(content, list):
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "tool_use"
                    and item.get("name") == "Agent"
                ):
                    agent_entries.append(
                        {
                            "tool_use_id": item.get("id", "unknown"),
                            "input": item.get("input", {}),
                        }
                    )

    return agent_entries


def main() -> None:
    # 1. Parse stdin payload
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    # 2. Determine the subagent's CWD
    cwd = (
        os.environ.get("CLAUDE_HOOK_CWD")
        or payload.get("cwd")
        or ""
    )

    # 3. Determine transcript path
    transcript_path = (
        os.environ.get("CLAUDE_SUBAGENT_TRANSCRIPT_PATH")
        or payload.get("transcript_path")
        or ""
    )

    # 4. Only audit if the subagent ran from a worktree
    if not cwd or not _is_worktree_path(cwd):
        sys.exit(0)

    # 5. Scan transcript for Agent tool_use entries
    if not transcript_path:
        sys.exit(0)

    agent_entries = _scan_transcript(transcript_path)
    if not agent_entries:
        sys.exit(0)

    # 6. Emit audit rows for each Agent call found
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for entry in agent_entries:
        row = {
            "ts": ts,
            "kind": "sandbox_block_agent_spawn",
            "source": "subagent_stop_dial_audit",
            "cwd": cwd,
            "tool_use_id": entry["tool_use_id"],
            "attempted_target": entry["input"].get("prompt", "")[:200],
            "transcript_path": transcript_path,
            "note": (
                "Agent() tool_use detected in post-stop transcript scan — "
                "PreToolUse hook may have silently failed to fire"
            ),
        }
        _append_audit_row(row)
        # Also emit to stderr so operators see it in logs
        print(
            f"[subagent_stop_dial_audit] WARN: Agent() call from worktree cwd={cwd!r} "
            f"found in transcript — audit row written",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
