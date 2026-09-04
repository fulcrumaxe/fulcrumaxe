"""backend/fleet/cost_summary.py — per-project rolling cost summary.

Maintains a small JSON file at <state_dir>/cost_summary.json that tracks
billable token spend over a 7-day rolling window.

Cache_read_input_tokens are FREE — they are excluded from billable totals.
Only input_tokens (prompt) and output_tokens are billable.

Called by scripts/hooks/post-agent.d/cost-summary.sh after each agent run.
Read by backend/rpc/fleet_cost.py to aggregate fleet-wide totals.

Schema of cost_summary.json::

    {
      "updated_at": "2026-05-17T12:00:00Z",
      "last_7d": [
        {"date": "2026-05-17", "input_tokens": 12000, "output_tokens": 3000},
        ...
      ]
    }

CLI::

    python3 -m backend.fleet.cost_summary record \\
        --state-dir ~/.fulcrumaxe-state \\
        --input-tokens 12000 \\
        --output-tokens 3000 \\
        --cache-read-tokens 5000
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_WINDOW_DAYS = 7


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_summary(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "cost_summary.json"
    if not path.exists():
        return {"updated_at": "", "last_7d": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": "", "last_7d": []}


def _write_summary(state_dir: Path, summary: dict[str, Any]) -> None:
    path = state_dir / "cost_summary.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    tmp.replace(path)


def update_cost_summary(
    state_dir: Path,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> None:
    """Append one agent run's billable spend to the rolling 7-day window.

    cache_read_tokens are excluded — they are free under Anthropic pricing.
    input_tokens = prompt/context tokens (billable).
    output_tokens = completion tokens (billable).
    """
    summary = _read_summary(state_dir)
    today = _today_utc()

    entries: list[dict[str, Any]] = summary.get("last_7d", [])

    # Find today's entry or create it
    today_entry = next((e for e in entries if e.get("date") == today), None)
    if today_entry is None:
        today_entry = {"date": today, "input_tokens": 0, "output_tokens": 0}
        entries.append(today_entry)

    today_entry["input_tokens"] = today_entry.get("input_tokens", 0) + input_tokens
    today_entry["output_tokens"] = today_entry.get("output_tokens", 0) + output_tokens

    # Prune to last 7 days
    entries.sort(key=lambda e: e.get("date", ""))
    entries = entries[-_WINDOW_DAYS:]

    summary["last_7d"] = entries
    summary["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    _write_summary(state_dir, summary)


def read_cost_summary(state_dir: Path) -> dict[str, Any]:
    """Return the cost summary for a single project, enriched with totals."""
    summary = _read_summary(state_dir)
    entries = summary.get("last_7d", [])
    today = _today_utc()

    # 24h = today only
    today_entry = next((e for e in entries if e.get("date") == today), None)
    tokens_24h = 0
    if today_entry:
        tokens_24h = today_entry.get("input_tokens", 0) + today_entry.get("output_tokens", 0)

    # 7d = full window
    tokens_7d = sum(
        e.get("input_tokens", 0) + e.get("output_tokens", 0)
        for e in entries
    )

    # Projected EOD: linear extrapolation from hours elapsed today
    now = datetime.now(timezone.utc)
    hours_elapsed = now.hour + now.minute / 60.0
    projected_eod = 0
    if hours_elapsed > 0 and tokens_24h > 0:
        projected_eod = int(tokens_24h / hours_elapsed * 24)

    return {
        "tokens_24h": tokens_24h,
        "tokens_7d": tokens_7d,
        "projected_eod_tokens": projected_eod,
        "by_day": entries,
        "updated_at": summary.get("updated_at", ""),
    }


def _cli_record(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir).expanduser()
    update_cost_summary(
        state_dir=state_dir,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cache_read_tokens=args.cache_read_tokens,
    )
    print(f"Updated cost_summary.json in {state_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage per-project cost summary")
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("record", help="Record one agent run's token spend")
    rec.add_argument("--state-dir", required=True, help="Path to project state dir")
    rec.add_argument("--input-tokens", type=int, default=0)
    rec.add_argument("--output-tokens", type=int, default=0)
    rec.add_argument("--cache-read-tokens", type=int, default=0)

    args = parser.parse_args()
    if args.command == "record":
        _cli_record(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
