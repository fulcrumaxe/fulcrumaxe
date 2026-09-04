#!/usr/bin/env python3
"""hooks/bash_retry_warn.py

PreToolUse hook — warn agents when they are about to run a cosmetic
variant of a Bash command that recently exited non-zero.

Reads a JSON tool-call object from stdin:
  {"tool_name": "Bash", "tool_input": {"command": "..."}, "cwd": "..."}

Exits 0 always (warn-only, never blocks).

Warning is printed to stderr so Claude Code surfaces it as a tool note.

Performance target: <100ms — reads only the tail of the transcript file.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

# How many recent failed Bash calls to inspect.
_LOOKBACK = 3

# Maximum bytes to read from the end of the transcript file.
_READ_TAIL_BYTES = 200_000

sys.path.insert(0, str(_REPO_ROOT))

from hooks._retry_common import normalize as _normalize, parse_bash_history as _parse_bash_history_common  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_id_from_cwd(cwd: str) -> "str | None":
    """Extract the agent/worktree ID from a worktree CWD."""
    worktree_prefixes = [
        str(_REPO_ROOT / ".claude" / "worktrees") + "/",
        "/tmp/wt-",
    ]
    try:
        resolved = str(Path(cwd).resolve())
    except Exception:
        resolved = cwd

    for prefix in worktree_prefixes:
        if resolved.startswith(prefix):
            rest = resolved[len(prefix):]
            return rest.split("/")[0] or None
    return None


def _find_transcript(agent_id: str) -> "str | None":
    """Return the path to the agent transcript file, or None."""
    patterns = [
        f"/tmp/claude-*/-home-agent-fulcrumaxe/*/tasks/{agent_id}.output",
        f"/tmp/claude-*/**/{agent_id}.output",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return max(matches, key=os.path.getmtime)
    return None


def _tail_bytes(path: str, nbytes: int) -> str:
    """Read the last *nbytes* of *path* as UTF-8."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - nbytes)
            fh.seek(start)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_bash_history(transcript_tail: str) -> "list[tuple[str, bool]]":
    """Parse transcript tail; return list of (command, failed) tuples.

    Delegates to hooks._retry_common.parse_bash_history.
    """
    return _parse_bash_history_common(transcript_tail)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)

    tool_name: str = payload.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    tool_input: dict = payload.get("tool_input", {})
    new_command: str = tool_input.get("command", "")
    cwd: str = payload.get("cwd", os.getcwd())

    if not new_command:
        sys.exit(0)

    agent_id = _agent_id_from_cwd(cwd)
    if not agent_id:
        sys.exit(0)

    transcript_path = _find_transcript(agent_id)
    if not transcript_path:
        sys.exit(0)

    tail = _tail_bytes(transcript_path, _READ_TAIL_BYTES)
    history = _parse_bash_history(tail)

    recent_failures = [(cmd, f) for cmd, f in history if f][-_LOOKBACK:]
    if not recent_failures:
        sys.exit(0)

    new_norm = _normalize(new_command)
    for failed_cmd, _ in recent_failures:
        if new_norm == _normalize(failed_cmd):
            msg = (
                "[bash-retry-guard] WARNING: this command is a cosmetic variant "
                "of a recent non-zero-exit call.\n"
                f"Last failure: {failed_cmd!r}\n"
                f"This call:     {new_command!r}\n"
                "Per CLAUDE.md Bash discipline: STOP, read stderr from the failure, "
                "identify root cause before retrying.\n"
                "Cosmetic retries are a flagged failure mode.\n"
            )
            sys.stderr.write(msg)
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
