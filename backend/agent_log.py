"""
agent_log.py — Write structured JSONL events to the agent feed file.

Agents call log_event() to append a single JSON line to
.autonomous-team/agent-feed.jsonl. Uses fcntl.flock for safe concurrent writes
from multiple agents running in parallel.

CLI usage:
    python backend/agent_log.py write --agent <id> --role <role> --event <event> \
        --detail <detail> [--discussion <n>]
"""

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone

FEED_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".autonomous-team",
    "agent-feed.jsonl",
)

MAX_LINES = 500
KEEP_LINES = 200


def log_event(
    agent: str,
    role: str,
    event: str,
    detail: str,
    discussion: int | None = None,
    feed_path: str = FEED_FILE,
) -> None:
    """Append one JSON event line to the agent feed file.

    Safe for concurrent calls — uses an exclusive flock before writing.
    If the feed file does not exist it is created (including parent dirs).
    """
    os.makedirs(os.path.dirname(feed_path), exist_ok=True)

    record: dict = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "agent": agent,
        "role": role,
        "event": event,
        "detail": detail[:200],  # cap at 200 chars per spec
    }
    if discussion is not None:
        record["discussion"] = discussion

    line = json.dumps(record, ensure_ascii=False) + "\n"

    with open(feed_path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def truncate_if_needed(feed_path: str = FEED_FILE) -> None:
    """Keep the feed file under MAX_LINES by retaining the last KEEP_LINES lines.

    Called by the Team Lead loop cleanup step.
    """
    if not os.path.exists(feed_path):
        return

    with open(feed_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    if len(lines) <= MAX_LINES:
        return

    kept = lines[-KEEP_LINES:]
    with open(feed_path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.writelines(kept)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Write an event to the agent feed file.")
    sub = parser.add_subparsers(dest="cmd")

    write_p = sub.add_parser("write", help="Append one event line.")
    write_p.add_argument("--agent", default="cli", help="Agent ID string")
    write_p.add_argument("--role", required=True, help="Agent role (executor, code-reviewer, …)")
    write_p.add_argument("--event", default="message", help="Event type (spawn, tool_call, …)")
    write_p.add_argument("--detail", required=True, help="Human-readable detail (max 200 chars)")
    write_p.add_argument("--discussion", type=int, default=None, help="Discussion number")

    sub.add_parser("truncate", help="Truncate feed file to last 200 lines if over 500.")

    args = parser.parse_args()

    if args.cmd == "write":
        log_event(
            agent=args.agent,
            role=args.role,
            event=args.event,
            detail=args.detail,
            discussion=args.discussion,
        )
        print(f"Logged: [{args.role}] {args.event}: {args.detail}")
    elif args.cmd == "truncate":
        truncate_if_needed()
        print("Truncation check complete.")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    _cli()
