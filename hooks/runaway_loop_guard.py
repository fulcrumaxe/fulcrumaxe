#!/usr/bin/env python3
"""hooks/runaway_loop_guard.py

PreToolUse hook — blocks 'until <x>; do sleep <N>; done' patterns in Bash
invocations.

These sleep-poll loops are a hard anti-pattern documented in feedback_no_runaway_loops:
they hold agent slots, burn budget with zero progress, and can spin forever if
the condition is never true.

Pattern matched (case-insensitive):
  until\\s+\\w+;\\s*do\\s+sleep\\s+\\d

Exits 2 (block) when the pattern is found.
Exits 0 (allow) otherwise.

Timing: 10ms hard timeout — fail-open on timeout or any IO error.

Telemetry: logs block events to
  .autonomous-team/hook-events/runaway-loop-blocks-YYYY-MM-DD.jsonl
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TELEMETRY_DIR = _REPO_ROOT / ".autonomous-team" / "hook-events"
_TIMEOUT_S = 0.010  # 10ms

# Matches: until <word>; do sleep <digits>
# Also catches: until cmd; do sleep 5; done (the done is not required to match)
_RUNAWAY_RE = re.compile(r"until\s+\w[\w\s()${}\"'-]*;\s*do\s+sleep\s+\d", re.IGNORECASE)


def _log_block(command: str) -> None:
    try:
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"runaway-loop-blocks-{date.today().isoformat()}.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "command_excerpt": command[:300],
        }
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # never wedge on telemetry failure


def main() -> None:
    start = time.monotonic()

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") != "Bash":
        sys.exit(0)

    command: str = payload.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    if time.monotonic() - start > _TIMEOUT_S:
        sys.exit(0)

    if _RUNAWAY_RE.search(command):
        _log_block(command)
        sys.stderr.write(
            "BLOCKED: 'until ... ; do sleep N; done' detected.\n"
            "Sleep-poll loops hold agent slots and burn budget for zero progress.\n"
            "Use Monitor with an until-loop in the shell or emit verdict:fail and stop.\n"
            "Do NOT retry with cosmetic variations of this command.\n"
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
