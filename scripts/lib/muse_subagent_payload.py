#!/usr/bin/env python3
"""scripts/lib/muse_subagent_payload.py — resolve a Muse wait-ready stop into
agent_run row fields (Muse twin of scripts/lib/subagent_payload.py).

Why this exists: the Claude twin recovers the spawn's join key from the
SubagentStop payload + transcript. Muse has no stop hook and no transcript in
the parent stream — but it has something better: parent-side join keys. The
assembler (scripts/lib/muse-spawn-prompt.sh) tells Team Lead to pass its
--event-id as the spawn's task name, and the spawn-accept echoes it back in
``agent_path`` (``main/<event-id>/1``). The resolver reads the event id from
there and looks the role/discussion up in the spawn registry — truth the
calling process wrote before the spawn, never anything the child produced.

Input (stdin JSON)::

    {"spawn_accept": {...}, "wait_result": {...}, "wait_is_error": bool}

Output: one flat JSON object (same *shape of contract* as the Claude twin's
FLAT dict — agent_id/role/verdict/tokens the shell can read without quoting
agent-authored content)::

    {"agent_id": ..., "subagent_id": ..., "role": ..., "discussion": ...,
     "verdict": "done|fail|unknown", "summary": ..., "task_ref": ...,
     "input_tok": null, "output_tok": null, "model": ...}

Tokens are ALWAYS 0 here: a wait-ready carries the child's summary, not
its usage — 0 is the established unknown-sentinel on this path (the Claude
twin falls back to 0 when the envelope omits tokens_used, and post-agent-hook
passes input/output tokens unconditionally). Token backfill from the child
session log (see evidence_refs) replaces them later.

``resolve()`` never raises: any input shape it doesn't recognise degrades to
the same all-defaults object, so the caller's exit-0 contract holds.

CLI usage:
  python3 muse_subagent_payload.py < stop.json
    Reads the stop JSON on stdin, prints one resolved JSON object on stdout.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Allow running as a script: repo root is two levels above scripts/lib/.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _defaults() -> dict[str, Any]:
    return {
        "agent_id": "",
        "subagent_id": "",
        "role": None,
        "discussion": None,
        "verdict": "unknown",
        "summary": "",
        "task_ref": "",
        "evidence_refs": [],
        "input_tok": 0,
        "output_tok": 0,
        "model": os.environ.get("AF_MODEL"),
    }


def _event_name_from_path(agent_path: Any) -> str:
    """Middle segment of ``main/<event-id>/1``; "" when unparseable."""
    if not isinstance(agent_path, str):
        return ""
    segments = [seg for seg in agent_path.split("/") if seg]
    if len(segments) >= 2:
        return segments[-2]
    return ""


def resolve(payload: Any) -> dict[str, Any]:
    """Resolve stop JSON into flat agent_run fields. Never raises."""
    out = _defaults()
    try:
        if not isinstance(payload, dict):
            return out
        accept = payload.get("spawn_accept")
        wait = payload.get("wait_result")
        if not isinstance(accept, dict) or not isinstance(wait, dict):
            return out
        accept_id = accept.get("subagent_id")
        wait_id = wait.get("subagent_id")
        if not accept_id or accept_id != wait_id:
            return out
        out["subagent_id"] = wait_id
        event_name = _event_name_from_path(accept.get("agent_path"))
        if not event_name:
            return out
        try:
            from backend.spawn_registry import find_by_event_id
        except Exception:
            return out
        rows = find_by_event_id(event_name)
        if not rows:
            return out
        row = rows[0]
        out["agent_id"] = event_name
        out["role"] = row.get("role")
        out["discussion"] = row.get("discussion")
        summary = wait.get("summary")
        out["summary"] = str(summary) if summary is not None else ""
        task_ref = wait.get("task_ref")
        out["task_ref"] = str(task_ref) if task_ref is not None else ""
        refs = wait.get("evidence_refs")
        out["evidence_refs"] = [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []
        failed = bool(payload.get("wait_is_error")) or bool(wait.get("errorKind"))
        out["verdict"] = "fail" if failed else "done"
        return out
    except Exception:
        return _defaults()


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else None
    except Exception:
        payload = None
    print(json.dumps(resolve(payload)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
