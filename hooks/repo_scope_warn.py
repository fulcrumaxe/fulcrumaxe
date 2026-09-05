#!/usr/bin/env python3
"""hooks/repo_scope_warn.py

PreToolUse hook — warns (does NOT block) when a gh CLI call is missing
'--repo <this project's configured repo>'.

Hard blocking would be too noisy (many read-only gh calls are fine without
--repo when the local git remote is set correctly). Warning is enough to
surface the anti-pattern in context.

Pattern matched:
  - 'gh api ...' without '--repo <target repo>'
  - 'gh pr ...' or 'gh issue ...' without '--repo <target repo>'

The target repo is resolved at warn time through backend._repo, the
project's canonical resolver (env var -> state-dir project.json -> repo-root
project.json -> the origin remote). It used to be a module-level literal
naming this project's pre-rename slug, which kept resolving only because
GitHub redirects renamed repos — so the warning told every adopter to scope
their calls at *our* repo, and would have gone on doing that silently
through any future rename or move. If nothing resolves, the hook says so
rather than naming a repo the caller may not own.

Exit codes:
  0 — allow (always; warnings go to stderr)

Telemetry: logs warn events to
  .autonomous-team/hook-events/repo-scope-warns-YYYY-MM-DD.jsonl

Timing: 10ms hard timeout — fail-open on timeout or any IO error. The
resolver import happens only on the warn path, after the timeout check, so
the common (no-warning) case pays nothing for it.
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

# Matches gh subcommands that mutate or query scoped resources
_GH_SCOPED_RE = re.compile(
    r"\bgh\s+(api|pr|issue|release|run)\b", re.IGNORECASE
)

# Already has --repo flag
_HAS_REPO_RE = re.compile(r"--repo\s+\S+", re.IGNORECASE)


def resolve_target_repo() -> str | None:
    """This project's repo slug, or None if it cannot be resolved.

    Delegates to backend._repo, which deliberately raises rather than falling
    back to a hard-coded slug when an adopter has configured nothing. A hook
    must never turn that into a failure, so the exception becomes None and the
    caller emits a slug-free warning instead.
    """
    try:
        root = str(_REPO_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        from backend._repo import REPO

        return REPO or None
    except Exception:
        return None


def _log_warn(command: str, target_repo: str | None) -> None:
    try:
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"repo-scope-warns-{date.today().isoformat()}.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "command_excerpt": command[:300],
            "target_repo": target_repo,
        }
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def warning_text(target_repo: str | None) -> str:
    """The stderr warning for a gh call missing --repo."""
    if target_repo:
        return (
            f"WARN: gh command missing --repo {target_repo}.\n"
            f"Add: --repo {target_repo} to scope this call correctly.\n"
        )
    return (
        "WARN: gh command missing --repo <this project's repo>.\n"
        "Could not resolve the project repo slug. Set AUTONOMOUS_TEAM_REPO, or add a\n"
        '"repo" field to .autonomous-team/project.json, then scope this call.\n'
    )


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
        target_repo = resolve_target_repo()
        _log_warn(command, target_repo)
        sys.stderr.write(warning_text(target_repo))

    # Always exit 0 — this hook warns only, never blocks
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
