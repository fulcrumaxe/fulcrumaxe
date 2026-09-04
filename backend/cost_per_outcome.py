"""
backend/cost_per_outcome.py — Cost-per-merged-PR view.

Aggregates cost_tracker.per_pr_summary() across recently-merged PRs
and enriches each row with a fix_rounds count from agent_run rows.

CLI usage:
    python backend/cost_per_outcome.py [--days N] [--json] [--limit N]

Library usage:
    from backend.cost_per_outcome import cost_per_outcome_rows
    rows = cost_per_outcome_rows(days=30)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend._repo import REPO  # noqa: E402 (after sys.path.insert)


def _get_merged_prs(days: int = 30) -> list[dict]:
    """Return recently-merged PR numbers from gh CLI.

    Falls back to an empty list if gh is unavailable or returns no results.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", REPO,
                "--state", "merged",
                "--limit", "200",
                "--json", "number,mergedAt,title",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        prs = json.loads(result.stdout)
        # Filter to PRs merged within the window
        filtered = []
        for pr in prs:
            merged_at = pr.get("mergedAt") or ""
            if merged_at >= since_str:
                filtered.append(pr)
        return filtered
    except Exception:  # noqa: BLE001
        return []


def _fix_rounds_for_pr(pr_number: int) -> int:
    """Count executor fix-round runs for a PR from agent_run table.

    A fix round is any executor run on the PR with verdict needs-fix or fail
    (i.e. runs that were NOT the first successful pass).
    Returns 0 on any error or missing data.
    """
    try:
        from backend.agent_run_reader import _connect  # noqa: PLC0415
        conn = _connect()
    except Exception:  # noqa: BLE001
        return 0

    try:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM agent_run
            WHERE pr = ?
              AND role = 'executor'
              AND verdict IN ('needs-fix', 'fail')
            """,
            [pr_number],
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0
    finally:
        conn.close()


def cost_per_outcome_rows(days: int = 30) -> list[dict]:
    """Return ranked cost-per-merged-PR rows.

    Each row contains:
        pr           — PR number (int)
        usd          — total USD spend (float, rounded to 6dp)
        total_tokens — total tokens (int)
        fix_rounds   — number of executor fix-round runs (int)
        by_role      — list of {role, input_tokens, output_tokens, usd}

    PRs with no cost records are omitted.
    Rows are sorted by usd descending (most expensive first).
    """
    from backend.cost_tracker import CostTracker  # noqa: PLC0415

    ct = CostTracker()
    merged_prs = _get_merged_prs(days=days)

    rows: list[dict] = []
    for pr_meta in merged_prs:
        pr_number = pr_meta["number"]
        summary = ct.per_pr_summary(pr_number)
        if summary is None:
            continue  # no records — omit per spec

        fix_rounds = _fix_rounds_for_pr(pr_number)
        rows.append(
            {
                "pr": pr_number,
                "usd": summary["usd"],
                "total_tokens": summary["total_tokens"],
                "fix_rounds": fix_rounds,
                "by_role": summary["by_role"],
            }
        )

    rows.sort(key=lambda r: r["usd"], reverse=True)
    return rows


def _render_table(rows: list[dict]) -> None:
    """Print a human-readable ranked table to stdout."""
    if not rows:
        print("No cost records found for merged PRs in window.")
        return

    header = f"{'PR':>6}  {'USD':>9}  {'Tokens':>10}  {'Fix Rounds':>10}  Top Role"
    print(header)
    print("-" * len(header))
    for row in rows:
        top_role = row["by_role"][0]["role"] if row["by_role"] else "—"
        print(
            f"#{row['pr']:5d}  ${row['usd']:8.4f}  {row['total_tokens']:10,d}"
            f"  {row['fix_rounds']:10d}  {top_role}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Cost-per-merged-PR summary",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look back N days for merged PRs",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap output to top N rows (0 = no cap)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit JSON array instead of table",
    )
    args = parser.parse_args(argv)

    rows = cost_per_outcome_rows(days=args.days)
    if args.limit > 0:
        rows = rows[: args.limit]

    if args.as_json:
        print(json.dumps(rows, indent=2))
    else:
        _render_table(rows)


if __name__ == "__main__":
    main()
