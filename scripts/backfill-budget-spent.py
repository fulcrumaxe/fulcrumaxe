#!/usr/bin/env python3
"""
backfill-budget-spent.py — recompute session_spent from the agents[] array.

After budget.py switched to deriving `spent` from agents[] at read time,
this script backfills the session_spent blackboard key so it reflects the
true historical total.  Run once after deploying the budget.py fix.

Usage:
    python3 scripts/backfill-budget-spent.py [--dry-run]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard  # noqa: E402
from backend.budget import _AGENTS_PREFIX, _KEY_SESSION_SPENT  # noqa: E402

DOLLARS_PER_INPUT_MILLION = 3.0   # rough Sonnet-4 pricing; used for display only
DOLLARS_PER_OUTPUT_MILLION = 15.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill budget/session_spent from agents[]")
    parser.add_argument("--dry-run", action="store_true", help="Print totals but do not write")
    args = parser.parse_args()

    bb = Blackboard()
    agent_keys = bb.list_keys(_AGENTS_PREFIX)

    total_input = 0
    total_output = 0
    for key in agent_keys:
        val = bb.read(key)
        if val is not None:
            total_input += val.get("input", 0) or 0
            total_output += val.get("output", 0) or 0

    real_spent = total_input + total_output
    approx_dollars = (
        (total_input / 1_000_000) * DOLLARS_PER_INPUT_MILLION
        + (total_output / 1_000_000) * DOLLARS_PER_OUTPUT_MILLION
    )

    old_val = bb.read(_KEY_SESSION_SPENT)

    print(f"Agent entries: {len(agent_keys)}")
    print(f"Input tokens:  {total_input:,}")
    print(f"Output tokens: {total_output:,}")
    print(f"Real total:    {real_spent:,} tokens (~${approx_dollars:.2f})")
    print(f"Old session_spent: {old_val}")

    if args.dry_run:
        print("[dry-run] Would write session_spent =", real_spent)
        return 0

    bb.write(_KEY_SESSION_SPENT, real_spent, updated_by="backfill-budget-spent")
    print(f"Written session_spent = {real_spent:,}")

    # Emit team-log line (best-effort)
    try:
        import subprocess
        repo_root = Path(__file__).resolve().parent.parent
        msg = (
            f"backfill-budget-spent: real total {real_spent:,} tokens "
            f"(~${approx_dollars:.2f}) across {len(agent_keys)} agent runs; "
            f"session_spent updated from {old_val} → {real_spent}"
        )
        subprocess.run(
            ["bash", "scripts/rotate-team-log.sh", "comment", msg],
            cwd=repo_root,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
