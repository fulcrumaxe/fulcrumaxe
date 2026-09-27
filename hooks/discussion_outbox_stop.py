#!/usr/bin/env python3
"""hooks/discussion_outbox_stop.py — SubagentStop entry point for the
Discussion outbox (D#2615 ENG-0, correction C2).

Deliberately a thin entry point: reads the raw SubagentStop payload from
stdin and hands it straight to `backend.discussion_outbox.process_stop_event`
with the payload untouched — never the envelope's `agent`/`discussion`
fields, never `scripts/lib/subagent_payload.py`'s resolved `role` (which
prefers the envelope), never a transcript scan for identity. See
`backend/discussion_outbox.py`'s module docstring for the full design and
`.claude/agents/executor.md`'s note on why this can't be wired through
`scripts/hooks/post-agent.d/` instead: that path resolves role/discussion
from the agent's own envelope, which the outbox threat model forbids as an
identity source.

Same shape as `hooks/fleet_unregister.py`: parses stdin defensively, never
raises past `main()`, and always exits 0 — a SubagentStop hook must never
block a stop (criterion 15).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hooks.repo_root import resolve_main_repo_root  # noqa: E402


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    try:
        from backend.discussion_outbox import process_stop_event  # noqa: PLC0415

        repo_root = resolve_main_repo_root()
        process_stop_event(payload, repo_root)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"[discussion_outbox_stop] WARNING: outbox processing skipped (non-fatal): {exc}\n"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    # Always allow — this hook never blocks a stop (criterion 15).
    sys.exit(0)
