"""
flaky_sentinel.py — record per-suite pass/fail history and compute flake scores.

Usage (CLI):
  python3 backend/flaky_sentinel.py record --test-id <id> --exit-code <code>
  python3 backend/flaky_sentinel.py flake-score --test-id <id>
  python3 backend/flaky_sentinel.py list [--json]
  python3 backend/flaky_sentinel.py is-quarantined --test-id <id>

State persists to STATE_DIR/flaky-history.jsonl (outside the repo).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import time
from typing import Sequence

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Allow override via env var for tests
import os as _os

def _history_path() -> Path:
    override = _os.environ.get("FLAKY_HISTORY_PATH")
    if override:
        return Path(override)
    from backend.state_paths import STATE_DIR, ensure_state_dir
    ensure_state_dir()
    return STATE_DIR / "flaky-history.jsonl"


# ---------------------------------------------------------------------------
# Window config
# ---------------------------------------------------------------------------

#: Only consider the most recent N runs per test_id when computing flake_score.
WINDOW = 20


# ---------------------------------------------------------------------------
# Core I/O
# ---------------------------------------------------------------------------

def _load_history(path: Path) -> list[dict]:
    """Return all records from the JSONL file (silently empty on missing/corrupt)."""
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return records


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record(test_id: str, exit_code: int, ts: float | None = None) -> dict:
    """Append a run record and return it.

    Parameters
    ----------
    test_id:
        Identifies the test suite (typically the command string).
    exit_code:
        0 = pass, non-zero = fail.
    ts:
        Unix timestamp; defaults to now.
    """
    row = {
        "test_id": test_id,
        "exit_code": int(exit_code),
        "passed": int(exit_code) == 0,
        "ts": ts if ts is not None else time.time(),
    }
    _append(_history_path(), row)
    return row


def _window_runs(test_id: str) -> list[dict]:
    """Return the most recent WINDOW runs for *test_id*."""
    all_rows = _load_history(_history_path())
    rows = [r for r in all_rows if r.get("test_id") == test_id]
    return rows[-WINDOW:]


def flake_score(test_id: str) -> float:
    """Fraction of fail-then-pass transitions in the bounded window.

    A score of 0 means "always consistent" (always-pass or always-fail).
    A score > 0 means the suite has been observed to pass after failing.

    The metric is: (number of fail→pass transitions) / (window_size - 1).
    Returns 0.0 when there are fewer than 2 runs.
    """
    runs = _window_runs(test_id)
    if len(runs) < 2:
        return 0.0
    transitions = sum(
        1
        for prev, curr in zip(runs, runs[1:])
        if not prev["passed"] and curr["passed"]
    )
    return transitions / (len(runs) - 1)


def is_quarantined(test_id: str) -> bool:
    """Pure read — placeholder for future quarantine enforcement.

    Always returns False in PR1; the consumer has not been wired yet.
    """
    return False


def list_tests() -> list[dict]:
    """Return a summary row per unique test_id."""
    all_rows = _load_history(_history_path())
    seen: dict[str, list[dict]] = {}
    for r in all_rows:
        seen.setdefault(r.get("test_id", ""), []).append(r)
    result = []
    for tid, rows in seen.items():
        window = rows[-WINDOW:]
        last = rows[-1]
        result.append(
            {
                "test_id": tid,
                "runs": len(rows),
                "flake_score": flake_score(tid),
                "quarantined": is_quarantined(tid),
                "last_exit_code": last.get("exit_code"),
                "last_ts": last.get("ts"),
            }
        )
    return result


def status(test_id: str) -> dict:
    runs = _window_runs(test_id)
    return {
        "test_id": test_id,
        "window_runs": len(runs),
        "flake_score": flake_score(test_id),
        "quarantined": is_quarantined(test_id),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flaky_sentinel",
        description="Record and score test-suite flakiness.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_record = sub.add_parser("record", help="Append a run record")
    p_record.add_argument("--test-id", required=True)
    p_record.add_argument("--exit-code", required=True, type=int)
    p_record.add_argument("--ts", type=float, default=None)

    p_score = sub.add_parser("flake-score", help="Print flake score for a test_id")
    p_score.add_argument("--test-id", required=True)

    p_list = sub.add_parser("list", help="List all tracked test suites")
    p_list.add_argument("--json", dest="as_json", action="store_true")

    p_status = sub.add_parser("is-quarantined", help="Check quarantine status for one test_id")
    p_status.add_argument("--test-id", required=True)
    p_status.add_argument("--json", dest="as_json", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "record":
        row = record(args.test_id, args.exit_code, args.ts)
        print(json.dumps(row))

    elif args.cmd == "flake-score":
        print(flake_score(args.test_id))

    elif args.cmd == "list":
        rows = list_tests()
        if args.as_json:
            print(json.dumps(rows, indent=2))
        else:
            if not rows:
                print("No test history recorded.")
            else:
                print(f"{'TEST_ID':<60} {'RUNS':>5} {'SCORE':>7} {'QUAR':>5}")
                for r in rows:
                    print(
                        f"{r['test_id']:<60} {r['runs']:>5} "
                        f"{r['flake_score']:>7.3f} {str(r['quarantined']):>5}"
                    )

    elif args.cmd == "is-quarantined":
        row = status(args.test_id)
        if args.as_json:
            print(json.dumps(row, indent=2))
        else:
            print(f"test_id    : {row['test_id']}")
            print(f"window_runs: {row['window_runs']}")
            print(f"flake_score: {row['flake_score']:.4f}")
            print(f"quarantined: {row['quarantined']}")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
