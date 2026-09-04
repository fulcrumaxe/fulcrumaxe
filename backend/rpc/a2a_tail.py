"""RPC handler: a2a.tail

Return the last N messages from the A2A audit log (messages.jsonl).
Used by the dashboard for the message history view.

Fleet audit (D#944 PR1): audit_path is derived from state_paths.STATE_DIR which
reads AUTONOMOUS_TEAM_STATE_DIR at import time. No hardcoded paths — no code
changes required for multi-project support.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def handle(params: dict) -> dict:
    """Return tail of the A2A audit log.

    Params: {"n": 20, "agent_id": "optional-filter"}
    Response: {"entries": [...], "count": N}
    Each entry has: id, from, to, kind, ts, body_sha256 (no body — audit only)
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    from state_paths import STATE_DIR

    n = int(params.get("n", 20))
    agent_filter = params.get("agent_id", "")

    audit_path = STATE_DIR / "a2a" / "messages.jsonl"

    if not audit_path.exists():
        return {"entries": [], "count": 0}

    entries: list[dict] = []
    try:
        with open(audit_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if agent_filter:
                        if entry.get("from") != agent_filter and entry.get("to") != agent_filter:
                            continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    pass
    except Exception:
        return {"entries": [], "count": 0}

    tail = entries[-n:]
    return {"entries": tail, "count": len(tail)}
