#!/usr/bin/env python3
"""hooks/repo_scope_warn.py

PreToolUse hook — warns (does NOT block) when a gh CLI call is missing
'--repo <this project's configured repo>' (see _TARGET_REPO below).

Hard blocking would be too noisy (many read-only gh calls are fine without
--repo when the local git remote is set correctly). Warning is enough to
surface the anti-pattern in context.

Pattern matched:
  - 'gh api ...' without '--repo <_TARGET_REPO>'
  - 'gh pr ...' or 'gh issue ...' without '--repo <_TARGET_REPO>'

Exit codes:
  0 — allow (always; warnings go to stderr)

Telemetry: logs warn events to
  .autonomous-team/hook-events/repo-scope-warns-YYYY-MM-DD.jsonl

Timing: 10ms hard timeout — fail-open on timeout or any IO error.
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

_TARGET_REPO = "fulcrumaxe/fulcrumaxe"

# Matches gh subcommands that mutate or query scoped resources
_GH_SCOPED_RE = re.compile(
    r"\bgh\s+(api|pr|issue|release|run)\b", re.IGNORECASE
)

# Already has --repo flag
_HAS_REPO_RE = re.compile(r"--repo\s+\S+", re.IGNORECASE)


def _log_warn(command: str) -> None:
    try:
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"repo-scope-warns-{date.today().isoformat()}.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "command_excerpt": command[:300],
        }
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


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

    if _GH_SCOPED_RE.search(command) and not _HAS_REPO_RE.search(command):
        _log_warn(command)
        sys.stderr.write(
            f"WARN: gh command missing --repo {_TARGET_REPO}.\n"
            f"Add: --repo {_TARGET_REPO} to scope this call correctly.\n"
        )

    # Always exit 0 — this hook warns only, never blocks
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
