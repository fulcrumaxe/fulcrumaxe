"""backend/spawn_activity.py — Read-only per-role agent spawn activity rollup.

Usage
-----
    python3 backend/spawn_activity.py --since=6h --json
    python3 backend/spawn_activity.py --since=24h

Flags
-----
--since=Nh   Look back N hours (integer hours only, e.g. 6h, 24h).  Default: 24h.
--json       Emit a JSON array of per-role rollup dicts to stdout.

Output shape (per role, --json)
--------------------------------
{
  "role":       "executor",
  "spawns":     7,       # total rows in the window
  "done":       5,       # verdict in ("done", "pass")
  "fail":       1,       # verdict == "fail"
  "avg_tokens": 48213,   # mean(input_tok + output_tok) where both non-null; 0 if none
  "total_usd":  0.9100   # sum cost_usd(input_tok, output_tok, model) rounded to 4 dp
}

Counting rules
--------------
- In-flight row (verdict IS NULL AND end_ts IS NULL) → counted in spawns only.
- verdict in {"done", "pass"}                        → counted in done.
- verdict == "fail"                                  → counted in fail.
- Any other non-null verdict (e.g. "needs-fix")      → counted in spawns only.
- spawns == done + fail + in-flight + other (always).

Error handling
--------------
duckdb missing / DB absent → treat as empty ([], exit 0).  Mirrors agent_run_reader.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure repo root is on sys.path when the module is run as a script
# (python3 backend/spawn_activity.py …) so the `backend.*` imports resolve.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger(__name__)

# Success verdict set per CLAUDE.md Structured Output Protocol.
_DONE_VERDICTS: frozenset[str] = frozenset({"done", "pass"})
_FAIL_VERDICT = "fail"


# ---------------------------------------------------------------------------
# Core rollup function — testable without argparse
# ---------------------------------------------------------------------------


def rollup(since_hours: int) -> list[dict]:
    """Return a per-role rollup of agent_run rows in the last *since_hours* hours.

    Parameters
    ----------
    since_hours:
        Number of hours to look back from now (UTC).

    Returns
    -------
    List of per-role dicts sorted by role ascending.  Empty list when the DB is
    absent, unreadable, or contains no rows in the window.
    """
    # Reuse the canonical read-only connection + DB-path helpers.
    # Import here so the module is importable even without duckdb installed
    # (the test file imports rollup directly).
    try:
        from backend.agent_run_reader import _connect  # noqa: PLC0415
    except ImportError:
        logger.debug("spawn_activity: agent_run_reader not importable")
        return []

    from backend.cost_pricing import cost_usd  # noqa: PLC0415

    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("spawn_activity.rollup: %s", exc)
        return []

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

        cur = conn.execute(
            """
            SELECT role, verdict, end_ts, input_tok, output_tok, model
            FROM   agent_run
            WHERE  start_ts >= ?
            ORDER  BY role ASC
            """,
            [cutoff],
        )
        rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("spawn_activity.rollup query failed: %s", exc)
        return []
    finally:
        conn.close()

    if not rows:
        return []

    # Aggregate in Python so cost_usd() handles per-row model correctly.
    # Structure: {role: {"spawns": int, "done": int, "fail": int,
    #                    "token_sums": list[int], "cost_sum": float}}
    agg: dict[str, dict] = {}

    for role, verdict, end_ts, input_tok, output_tok, model in rows:
        if role not in agg:
            agg[role] = {"spawns": 0, "done": 0, "fail": 0, "token_sums": [], "cost_sum": 0.0}

        a = agg[role]
        a["spawns"] += 1

        # In-flight: no verdict AND no end_ts — count in spawns only.
        in_flight = (verdict is None) and (end_ts is None)

        if not in_flight:
            if verdict in _DONE_VERDICTS:
                a["done"] += 1
            elif verdict == _FAIL_VERDICT:
                a["fail"] += 1
            # Other non-null verdicts (needs-fix, skip …) → spawns only.

        # Token / cost aggregation — only when both token columns are non-null.
        if input_tok is not None and output_tok is not None:
            a["token_sums"].append(int(input_tok) + int(output_tok))
            a["cost_sum"] += cost_usd(
                input_tok=int(input_tok),
                output_tok=int(output_tok),
                model=model,
            )

    result: list[dict] = []
    for role in sorted(agg.keys()):
        a = agg[role]
        token_sums = a["token_sums"]
        avg_tokens = int(sum(token_sums) / len(token_sums)) if token_sums else 0
        result.append(
            {
                "role": role,
                "spawns": a["spawns"],
                "done": a["done"],
                "fail": a["fail"],
                "avg_tokens": avg_tokens,
                "total_usd": round(a["cost_sum"], 4),
            }
        )

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_since(value: str) -> int:
    """Parse a '--since=Nh' string and return N as an int.

    Raises ValueError with a clear message on bad input.
    """
    val = value.strip()
    if not val.endswith("h"):
        raise ValueError(
            f"--since must be in the form Nh (e.g. 6h, 24h), got: {val!r}"
        )
    hours_str = val[:-1]
    if not hours_str.isdigit():
        raise ValueError(
            f"--since hour component must be a positive integer, got: {hours_str!r}"
        )
    hours = int(hours_str)
    if hours <= 0:
        raise ValueError(f"--since hours must be > 0, got: {hours}")
    return hours


def _table_output(rows: list[dict]) -> str:
    """Format *rows* as a small fixed-width table for human consumption."""
    if not rows:
        return "no activity in window"

    header = f"{'role':<25}  {'spawns':>6}  {'done':>6}  {'fail':>6}  {'avg_tokens':>10}  {'total_usd':>10}"
    sep = "-" * len(header)
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"{r['role']:<25}  {r['spawns']:>6}  {r['done']:>6}  "
            f"{r['fail']:>6}  {r['avg_tokens']:>10}  {r['total_usd']:>10.4f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns an integer exit code."""
    parser = argparse.ArgumentParser(
        description="Per-role agent spawn activity rollup over a time window.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--since",
        default="24h",
        metavar="Nh",
        help="Look back N hours (e.g. 6h, 24h).  Default: 24h.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit output as a JSON array.",
    )
    args = parser.parse_args(argv)

    try:
        since_hours = _parse_since(args.since)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rows = rollup(since_hours)

    if args.as_json:
        print(json.dumps(rows, indent=2))
    else:
        print(_table_output(rows))

    return 0


if __name__ == "__main__":
    sys.exit(main())
