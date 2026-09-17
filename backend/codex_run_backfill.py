"""
backend/codex_run_backfill.py — Record Muse subagent completions in agent_run.

The runtime keeps real per-agent usage for Muse subagents outside the result
envelope: each delegated agent gets ``subagent/<child-session-id>/session.jsonl``
under the parent session dir, and that child log's ``model_completed`` events
carry measured ``usage``. The parent log carries the join key —
``subagent.control.spawn_accepted`` (subagent_id, role),
``subagent.control.child_session_bound`` (subagent_id to child dir name), and
``subagent.control.result_ready`` (verdict, end time, child transcript_path).

Tokens come ONLY from the child log. A genuinely missing child log still gets
a row (verdict and timing are known) with NULL tokens — never self-reported
numbers, never estimates. Session logs are READ-ONLY: never written by this
module. Only completions (spawns with a result_ready) are recorded, so
re-running never creates start-only rows; ``complete_run`` upserts on
``muse-<child-session>`` with COALESCE, so re-runs only fill open rows.

CLI::

    python3 -m backend.codex_run_backfill --parent-log /path/to/session.jsonl
    python3 -m backend.codex_run_backfill --parent-log ... --db-path ... \\
        --discussion 2604 [--subagent-id <id>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

#: agent_run.routed_via value for every row written here.
CODEX_ROUTED_VIA = "muse"


def _event_records(path: Path):
    """Yield parsed JSON objects from a session log, skipping bad lines."""
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _recorded_at_dt(record: dict[str, Any]) -> datetime | None:
    """Convert session-log ``recorded_at`` (microseconds) to UTC datetime."""
    ts = record.get("recorded_at")
    if not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_child_usage(child_log: Path) -> tuple[int | None, int | None, str | None]:
    """Sum measured tokens over a child log's ``model_completed`` events.

    Returns ``(input, output, model)``; ``(None, None, None)`` when the log
    holds no model_completed events at all (missing file included) so the
    caller writes NULL, never a guessed 0.
    """
    total_in = total_out = 0
    model: str | None = None
    found = False
    for record in _event_records(child_log):
        if record.get("payload_type") != "runtime.session":
            continue
        event = (record.get("payload") or {}).get("event") or {}
        if event.get("kind") != "model_completed":
            continue
        usage = event.get("usage") or {}
        try:
            total_in += int(usage.get("input_tokens") or 0)
            total_out += int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if event.get("model"):
            model = str(event["model"])
        found = True
    return (total_in, total_out, model) if found else (None, None, None)


def parse_parent_spawns(parent_log: Path) -> dict[str, dict[str, Any]]:
    """Join a parent log's spawn records by subagent_id.

    Returns ``{subagent_id: {role, start_ts, child_session_id, end_ts,
    error_kind, transcript_path}}`` — completions only (a spawn without a
    result_ready is still running and is skipped).
    """
    spawns: dict[str, dict[str, Any]] = {}
    bound: dict[str, str] = {}
    ready: dict[str, dict[str, Any]] = {}
    for record in _event_records(parent_log):
        pt = record.get("payload_type")
        if pt not in (
            "subagent.control.spawn_accepted",
            "subagent.control.child_session_bound",
            "subagent.control.result_ready",
        ):
            continue
        rec = (record.get("payload") or {}).get("record") or {}
        if not isinstance(rec, dict):
            continue
        if pt == "subagent.control.spawn_accepted":
            if rec.get("subagent_id"):
                spawns[str(rec["subagent_id"])] = {
                    "role": rec.get("role"),
                    "start_ts": _recorded_at_dt(record),
                }
        elif pt == "subagent.control.child_session_bound":
            if rec.get("subagent_id") and rec.get("child_session_id"):
                bound[str(rec["subagent_id"])] = str(rec["child_session_id"])
        elif rec.get("subagent_id"):
            ready[str(rec["subagent_id"])] = {
                "end_ts": _recorded_at_dt(record),
                "error_kind": rec.get("error_kind"),
                "transcript_path": rec.get("transcript_path"),
            }
    return {
        sid: {
            **info,
            "child_session_id": bound.get(sid),
            **ready[sid],
        }
        for sid, info in spawns.items()
        if sid in ready
    }


def _child_log_for(parent_log: Path, completion: dict[str, Any]) -> Path | None:
    """Resolve the child session log: transcript_path first, then dir join."""
    if completion.get("transcript_path"):
        cand = Path(str(completion["transcript_path"]))
        if cand.is_file():
            return cand
    if completion.get("child_session_id"):
        cand = parent_log.parent / "subagent" / str(completion["child_session_id"]) / "session.jsonl"
        if cand.is_file():
            return cand
    return None


def backfill_parent_log(
    parent_log: Path | str,
    db_path: Path | str | None = None,
    discussion: int | None = None,
    subagent_id: str | None = None,
) -> dict[str, Any]:
    """Write agent_run rows for Muse completions in one parent session log.

    ``db_path`` overrides the stats DB (tests); ``discussion`` stamps newly
    created rows; ``subagent_id`` records exactly one completion (hook mode).
    Returns ``{parent_log, spawns, rows_written, tokens_null}``. A missing
    parent log yields an all-zero report, never raises.
    """
    from backend import agent_run_tracker  # noqa: PLC0415

    parent_log = Path(parent_log)
    report: dict[str, Any] = {
        "parent_log": str(parent_log),
        "spawns": 0,
        "rows_written": 0,
        "tokens_null": 0,
    }
    if not parent_log.is_file():
        return report
    completions = parse_parent_spawns(parent_log)
    if subagent_id is not None:
        completions = {k: v for k, v in completions.items() if k == subagent_id}

    old_stats_db = os.environ.get("STATS_DB_PATH")
    if db_path is not None:
        os.environ["STATS_DB_PATH"] = str(db_path)
    try:
        for sid, info in completions.items():
            child_log = _child_log_for(parent_log, info)
            if child_log is not None:
                input_tok, output_tok, model = parse_child_usage(child_log)
            else:
                input_tok, output_tok, model = None, None, None
            cid = info.get("child_session_id")
            agent_run_tracker.complete_run(
                agent_id=f"muse-{cid}" if cid else f"muse-{sid}",
                end_ts=info.get("end_ts"),
                start_ts=info.get("start_ts"),
                verdict="done" if info.get("error_kind") is None else "fail",
                model=model,
                input_tok=input_tok,
                output_tok=output_tok,
                routed_via=CODEX_ROUTED_VIA,
                role=info.get("role"),
                discussion=discussion,
            )
            report["spawns"] += 1
            report["rows_written"] += 1
            if input_tok is None and output_tok is None:
                report["tokens_null"] += 1
    finally:
        if db_path is not None:
            if old_stats_db is None:
                os.environ.pop("STATS_DB_PATH", None)
            else:
                os.environ["STATS_DB_PATH"] = old_stats_db
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record Muse subagent completions from session logs into agent_run",
    )
    parser.add_argument("--parent-log", required=True)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--discussion", type=int, default=None)
    parser.add_argument("--subagent-id", default=None)
    args = parser.parse_args(argv)
    report = backfill_parent_log(
        parent_log=args.parent_log,
        db_path=args.db_path,
        discussion=args.discussion,
        subagent_id=args.subagent_id,
    )
    print(
        f"codex-backfill: spawns={report['spawns']} "
        f"rows_written={report['rows_written']} "
        f"tokens_null={report['tokens_null']} "
        f"parent_log={report['parent_log']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
