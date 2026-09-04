"""agent_retros.py — aggregation CLI for the self-observe retro log.

Manages .autonomous-team/agent-retros.jsonl.

Each entry shape:
    {
      "ts": "2026-05-11T10:00:00Z",
      "agent_id": "...",
      "role": "executor",
      "classifier": "git_rm_usage",
      "trigger": "...",
      "why": "...",
      "future_fix": "...",
      "work_corrected": true,
      "shadow_mode": false,
      "turn_idx": 12
    }

Primary key for dedup: (agent_id, classifier, turn_idx).

Usage:
    python3 backend/agent_retros.py list [--since 24h] [--role executor]
    python3 backend/agent_retros.py by-classifier
    python3 backend/agent_retros.py by-role
    python3 backend/agent_retros.py summary
    python3 backend/agent_retros.py append --agent-id X --role Y --classifier Z \\
        --trigger "..." --why "..." --future-fix "..." --work-corrected --turn-idx N \\
        [--shadow-mode]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os
_REPO_ROOT = Path(__file__).resolve().parent.parent
RETROS_FILE = Path(os.environ.get(
    "AF_RETROS_FILE",
    str(_REPO_ROOT / ".autonomous-team" / "agent-retros.jsonl"),
))


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_since(since_str: str) -> datetime:
    import re
    now = datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([dhm])", since_str.strip())
    if not m:
        raise ValueError(f"Invalid since format: {since_str!r}. Use e.g. '7d', '24h'.")
    value, unit = int(m.group(1)), m.group(2)
    delta = {"d": timedelta(days=value), "h": timedelta(hours=value), "m": timedelta(minutes=value)}[unit]
    return now - delta


def load_retros(since: datetime | None = None, role: str | None = None) -> list[dict]:
    """Load retro entries from agent-retros.jsonl, optionally filtered."""
    entries: list[dict] = []
    if not RETROS_FILE.exists():
        return entries
    with open(RETROS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                ts_str = entry.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts < since:
                        continue
                except (ValueError, AttributeError):
                    pass
            if role is not None and entry.get("role") != role:
                continue
            entries.append(entry)
    return entries


def get_latest_retro(discussion_number: int) -> dict | None:
    """Return the most recent retro entry for *discussion_number*, or None.

    Filters retro entries by the ``discussion`` field. Entries written before
    this field was introduced will not match — the function returns None for
    those discussions, which is correct (no prior context available).
    """
    entries = load_retros()
    # Walk in reverse so we get the most recent match first.
    for entry in reversed(entries):
        if entry.get("discussion") == discussion_number:
            return entry
    return None


def append_retro(entry: dict) -> bool:
    """Append a retro entry. Returns False if primary key already exists (dedup)."""
    RETROS_FILE.parent.mkdir(parents=True, exist_ok=True)

    agent_id = entry.get("agent_id", "")
    classifier = entry.get("classifier", "")
    turn_idx = entry.get("turn_idx", -1)

    # Dedup by primary key
    if RETROS_FILE.exists():
        with open(RETROS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing = json.loads(line)
                    if (
                        existing.get("agent_id") == agent_id
                        and existing.get("classifier") == classifier
                        and existing.get("turn_idx") == turn_idx
                    ):
                        return False  # already exists
                except json.JSONDecodeError:
                    continue

    with open(RETROS_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return True


def cmd_list(args: argparse.Namespace) -> int:
    since = _parse_since(args.since) if args.since else None
    entries = load_retros(since=since, role=args.role or None)
    if not entries:
        print("No retro entries found.")
        return 0
    for e in entries:
        shadow = " [SHADOW]" if e.get("shadow_mode") else ""
        corrected = " corrected=yes" if e.get("work_corrected") else " corrected=no"
        print(f"[{e.get('ts','')}] {e.get('role','?')} agent={e.get('agent_id','?')[:20]}"
              f" classifier={e.get('classifier','?')}{corrected}{shadow}")
        print(f"  trigger: {e.get('trigger','')[:100]}")
        print(f"  why:     {e.get('why','')[:100]}")
        print(f"  fix:     {e.get('future_fix','')[:100]}")
    return 0


def cmd_by_classifier(_args: argparse.Namespace) -> int:
    entries = load_retros()
    counts: Counter[str] = Counter(e.get("classifier", "unknown") for e in entries)
    if not counts:
        print("No retro entries found.")
        return 0
    print(f"{'Classifier':<40} {'Count':>5}")
    print("-" * 47)
    for clf, count in counts.most_common():
        # Sample entry
        sample = next((e for e in entries if e.get("classifier") == clf), {})
        sample_text = sample.get("future_fix", "")[:60]
        print(f"{clf:<40} {count:>5}  e.g.: {sample_text}")
    return 0


def cmd_by_role(_args: argparse.Namespace) -> int:
    entries = load_retros()
    by_role: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_role[e.get("role", "unknown")].append(e)
    if not by_role:
        print("No retro entries found.")
        return 0
    print(f"{'Role':<25} {'Total':>5} {'Corrected':>9} {'Shadow':>6}")
    print("-" * 50)
    for role, role_entries in sorted(by_role.items()):
        total = len(role_entries)
        corrected = sum(1 for e in role_entries if e.get("work_corrected"))
        shadow = sum(1 for e in role_entries if e.get("shadow_mode"))
        print(f"{role:<25} {total:>5} {corrected:>9} {shadow:>6}")
    return 0


def cmd_summary(_args: argparse.Namespace) -> int:
    """Print a summary suitable for team_status.py tile."""
    entries = load_retros()
    total = len(entries)
    if total == 0:
        print("agent-retros: 0 entries")
        return 0

    recent_24h = load_retros(since=_parse_since("24h"))
    corrected = sum(1 for e in entries if e.get("work_corrected"))
    shadow = sum(1 for e in entries if e.get("shadow_mode"))
    clf_counts: Counter[str] = Counter(e.get("classifier", "?") for e in recent_24h)
    top3 = clf_counts.most_common(3)

    print(f"agent-retros: {total} total | last 24h: {len(recent_24h)}"
          f" | corrected: {corrected} | shadow: {shadow}")
    if top3:
        top3_str = ", ".join(f"{c}={n}" for c, n in top3)
        print(f"  top classifiers (24h): {top3_str}")
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    """Append a retro entry from CLI (used by agents/scripts)."""
    entry = {
        "ts": _now_utc(),
        "agent_id": args.agent_id,
        "role": args.role,
        "classifier": args.classifier,
        "trigger": args.trigger,
        "why": args.why,
        "future_fix": args.future_fix,
        "work_corrected": bool(args.work_corrected),
        "shadow_mode": bool(args.shadow_mode),
        "turn_idx": args.turn_idx if args.turn_idx is not None else -1,
    }
    written = append_retro(entry)
    if written:
        print(f"Appended retro: agent={args.agent_id[:20]} classifier={args.classifier}")
    else:
        print(f"Skipped (duplicate key): agent={args.agent_id[:20]} "
              f"classifier={args.classifier} turn_idx={args.turn_idx}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent retros CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List recent retros")
    p_list.add_argument("--since", default="24h", help="Time window, e.g. 24h, 7d")
    p_list.add_argument("--role", help="Filter by role")

    sub.add_parser("by-classifier", help="Group by classifier")
    sub.add_parser("by-role", help="Group by role with error rates")
    sub.add_parser("summary", help="One-line summary for team_status tile")

    p_append = sub.add_parser("append", help="Append a retro entry")
    p_append.add_argument("--agent-id", required=True)
    p_append.add_argument("--role", required=True)
    p_append.add_argument("--classifier", required=True)
    p_append.add_argument("--trigger", required=True)
    p_append.add_argument("--why", required=True)
    p_append.add_argument("--future-fix", required=True)
    p_append.add_argument("--work-corrected", action="store_true")
    p_append.add_argument("--shadow-mode", action="store_true")
    p_append.add_argument("--turn-idx", type=int, default=None)

    args = parser.parse_args()

    cmds = {
        "list": cmd_list,
        "by-classifier": cmd_by_classifier,
        "by-role": cmd_by_role,
        "summary": cmd_summary,
        "append": cmd_append,
    }
    fn = cmds.get(args.cmd)
    if fn is None:
        parser.print_help()
        return 1
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
