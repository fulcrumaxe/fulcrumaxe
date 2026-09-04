#!/usr/bin/env python3
"""
backfill-cost-tracker.py — rescore historical agent spend records using corrected pricing.

Usage:
    python3 scripts/backfill-cost-tracker.py [--dry-run] [--quiet]

What it does:
    1. Reads every budget/agents/* record from the blackboard.
    2. Recomputes cost_usd using the current pricing table (including cache tokens
       if present in the record).
    3. Prints a before/after summary.
    4. Writes a one-line "backfill done: real total $X" to team-log (unless --dry-run).

The script does NOT modify blackboard records — cost_usd is computed on-the-fly by
CostTracker from raw token counts plus the pricing table. The fix to the pricing table
in cost_tracker.py and config.json is sufficient; this script just surfaces the corrected
totals for auditing purposes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard  # noqa: E402
from backend.cost_tracker import CostTracker, _DEFAULT_PRICING, _compute_cost  # noqa: E402

# Old pricing table (pre-fix) — used to compute what we were reporting before
_OLD_PRICING: dict[str, dict[str, float]] = {
    "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "claude-sonnet-4-20250514": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "claude-opus-4-20250514": {"input_per_1k": 0.015, "output_per_1k": 0.075},
    "kimi-k2-0711": {"input_per_1k": 0.0006, "output_per_1k": 0.002},
}

_AGENTS_PREFIX = "budget/agents/"


def run_backfill(dry_run: bool = False, quiet: bool = False) -> int:
    bb = Blackboard()
    ct = CostTracker(bb=bb)

    agent_keys = bb.list_keys(_AGENTS_PREFIX)
    if not agent_keys:
        print("No agent spend records found in blackboard.")
        return 0

    old_total = 0.0
    new_total = 0.0
    changed_count = 0
    unknown_models: set[str] = set()

    rows = []
    for key in agent_keys:
        record = bb.read(key)
        if not isinstance(record, dict):
            continue

        input_tokens = int(record.get("input", 0))
        output_tokens = int(record.get("output", 0))
        cache_read = int(record.get("cache_read_tokens", 0))
        cache_write = int(record.get("cache_write_tokens", 0))
        model = record.get("model", "default") or "default"
        agent_id = record.get("agent_id", key.replace(_AGENTS_PREFIX, ""))
        role = record.get("agent", "unknown")

        old_cost = _compute_cost(input_tokens, output_tokens, model, _OLD_PRICING)
        new_cost = _compute_cost(
            input_tokens, output_tokens, model, ct._pricing,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

        old_total += old_cost
        new_total += new_cost

        if model not in _OLD_PRICING and model != "default":
            unknown_models.add(model)

        delta = new_cost - old_cost
        if abs(delta) > 1e-9:
            changed_count += 1
            rows.append((agent_id, role, model, old_cost, new_cost, delta))

    multiplier = new_total / old_total if old_total > 0 else float("inf")

    if not quiet:
        print(f"Backfill summary")
        print(f"{'='*60}")
        print(f"  Records scanned:    {len(agent_keys)}")
        print(f"  Records repriced:   {changed_count}")
        print(f"  Old total cost:     ${old_total:.4f}")
        print(f"  New total cost:     ${new_total:.4f}")
        print(f"  Multiplier:         {multiplier:.2f}x")
        if unknown_models:
            print(f"  Previously unknown models now priced: {', '.join(sorted(unknown_models))}")
        print()

        if rows and not quiet:
            print(f"{'Agent ID':<40} {'Role':<20} {'Model':<30} {'Old $':>8} {'New $':>8} {'Delta':>8}")
            print("-" * 120)
            for agent_id, role, model, old_c, new_c, delta in sorted(rows, key=lambda x: abs(x[5]), reverse=True)[:20]:
                print(f"  {agent_id:<38} {role:<20} {model:<30} ${old_c:>7.4f} ${new_c:>7.4f} {'+' if delta>0 else ''}{delta:>7.4f}")
            if len(rows) > 20:
                print(f"  ... and {len(rows)-20} more")
            print()

    if not dry_run:
        # Emit to team-log via rotate-team-log.sh
        msg = (
            f"[backfill-cost-tracker] repricing complete: "
            f"{len(agent_keys)} records, "
            f"old=${old_total:.2f} → new=${new_total:.2f} ({multiplier:.1f}x). "
            f"Models newly priced: {', '.join(sorted(unknown_models)) if unknown_models else 'none'}."
        )
        script_dir = Path(__file__).resolve().parent
        rotate_log = script_dir / "rotate-team-log.sh"
        if rotate_log.exists():
            try:
                subprocess.run(
                    ["bash", str(rotate_log), "comment", msg],
                    check=False,
                    capture_output=True,
                )
            except OSError:
                pass
        print(f"Team-log: {msg}")
    else:
        print("(dry-run — no team-log write)")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rescore historical agent spend records with corrected pricing.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing to team-log.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-record table; only print summary.",
    )
    args = parser.parse_args()
    return run_backfill(dry_run=args.dry_run, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
