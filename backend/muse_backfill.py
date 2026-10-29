"""backend/muse_backfill.py — token backfill from Muse child session logs.

A Muse wait-ready carries the child's summary but not its usage. The child's
session log (wait_result.evidence_refs, a session.jsonl path) records one
assistant_message_committed event per assistant message (the LAST is the
child's final text) and one goal_usage_attribution event per model call
(input/output/cached tokens in record.quantity). This module sums them into
the agent_run row via complete_run (upsert — idempotent, safe to re-run).

Used by scripts/muse-subagent-stop.sh right after the stop row is recorded,
and standalone for retries:

  python -m backend.muse_backfill backfill --agent-id <event-id> --ref <path> [--ref ...]

Exit 0 always; unusable refs (missing files, non-paths, malformed logs) are
skipped with a warning, never fatal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number >= 0 else 0


def read_child_result(log_path: str) -> dict[str, Any] | None:
    """Read a child session.jsonl into {text, input_tok, output_tok}.

    Returns None when the path is unusable (missing, unreadable, no usable
    events). Malformed lines are skipped. Never raises.
    """
    try:
        handle = open(log_path, encoding="utf-8")
    except OSError:
        return None
    text: str | None = None
    input_tok = 0
    output_tok = 0
    try:
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                event = (entry.get("payload") or {}).get("event") or {}
                if not isinstance(event, dict):
                    continue
                kind = event.get("kind")
                if kind == "assistant_message_committed":
                    message = event.get("text")
                    if isinstance(message, str):
                        text = message
                elif kind == "goal_usage_attribution":
                    quantity = ((event.get("record") or {}).get("quantity")) or {}
                    if isinstance(quantity, dict):
                        input_tok += _as_int(quantity.get("input_tokens"))
                        output_tok += _as_int(quantity.get("output_tokens"))
    except Exception as exc:  # noqa: BLE001 — unreadable log degrades to None
        logger.warning("muse_backfill: failed reading %s: %s", log_path, exc)
        return None
    if text is None and input_tok == 0 and output_tok == 0:
        return None
    return {"text": text or "", "input_tok": input_tok, "output_tok": output_tok}


def backfill_tokens(agent_id: str, evidence_refs: list[str]) -> dict[str, Any] | None:
    """Write summed child usage into the agent_run row. Never raises.

    Uses the first ref that reads as a session log; returns the summary that
    was written, or None when nothing was usable.
    """
    if not agent_id or not evidence_refs:
        return None
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref.endswith(".jsonl"):
            continue
        if not os.path.isfile(ref):
            continue
        result = read_child_result(ref)
        if result is None:
            continue
        try:
            from backend.agent_run_tracker import complete_run  # noqa: PLC0415

            complete_run(
                agent_id=agent_id,
                input_tok=result["input_tok"],
                output_tok=result["output_tok"],
            )
        except Exception as exc:  # noqa: BLE001 — backfill is best-effort
            logger.warning("muse_backfill: complete_run failed for %s: %s", agent_id, exc)
            return None
        return {"agent_id": agent_id, **result}
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="muse_backfill")
    sub = parser.add_subparsers(dest="command", required=True)
    backfill = sub.add_parser("backfill", help="Backfill one run's tokens from child log refs")
    backfill.add_argument("--agent-id", required=True)
    backfill.add_argument("--ref", dest="refs", action="append", default=[])
    args = parser.parse_args(argv)
    if args.command == "backfill":
        out = backfill_tokens(args.agent_id, args.refs)
        if out is not None:
            print(json.dumps({"agent_id": out["agent_id"], "input_tok": out["input_tok"], "output_tok": out["output_tok"]}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
