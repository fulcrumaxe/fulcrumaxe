"""
flaky_sentinel.py — record per-suite pass/fail history and compute flake scores.

Usage (CLI):
  python3 backend/flaky_sentinel.py record --test-id <id> --exit-code <code>
  python3 backend/flaky_sentinel.py flake-score --test-id <id>
  python3 backend/flaky_sentinel.py list [--json]
  python3 backend/flaky_sentinel.py is-quarantined --test-id <id> [--json]
  python3 backend/flaky_sentinel.py report [--json]
  python3 backend/flaky_sentinel.py advise --test-id <id>   # stderr-only advisory, silent on stdout

State persists to STATE_DIR/flaky-history.jsonl (outside the repo). Reads
canonicalize test_id to group flag-spelling variants of one suite (see
`_canonical_id`); the store itself is never rewritten.
"""

from __future__ import annotations

import argparse
import json
import re
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

#: Quarantine threshold: a canonical id needs at least this many runs in the
#: window before flake_score is trusted at all — a single fail->pass on a
#: two-run history is noise, not signal (12 of today's 45 ids sit at exactly
#: that shape and none of them should quarantine).
QUARANTINE_MIN_RUNS = 4

#: ...and, with enough runs, flake_score must clear this bar.
QUARANTINE_SCORE_THRESHOLD = 0.3


# ---------------------------------------------------------------------------
# Canonicalization (read-side only — the store itself is never rewritten)
# ---------------------------------------------------------------------------

#: Reporting-only flags that don't change which tests run: -q, -x, -v/-vv,
#: --tb=*. Anything else (target paths, subcommands, other flags) is kept and
#: still distinguishes one suite from another.
_REPORT_FLAG_RE = re.compile(r"^(-q|-x|-v+|--tb=\S*)$")


def _canonical_id(test_id: str) -> str:
    """Collapse whitespace and strip reporting-only flags from *test_id* so
    that flag-spelling variants of the same suite (e.g. `pytest a b -x -q`
    and `pytest a b -q`) group under one key. Target paths are never
    stripped, so `pytest tests/` and `pytest tests/ backend/tests/` stay
    distinct suites. Applied on read only — the JSONL store keeps whatever
    raw test_id was recorded.
    """
    if not test_id:
        return test_id
    kept = [tok for tok in test_id.split() if not _REPORT_FLAG_RE.match(tok)]
    return " ".join(kept)


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
    """Return the most recent WINDOW runs whose test_id canonicalizes to the
    same key as *test_id* (see `_canonical_id`)."""
    canon = _canonical_id(test_id)
    all_rows = _load_history(_history_path())
    rows = [r for r in all_rows if _canonical_id(r.get("test_id", "")) == canon]
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
    """Return True when *test_id* (canonicalized) has enough run history to
    trust its flake_score, and that score clears the quarantine bar.

    Threshold: flake_score >= QUARANTINE_SCORE_THRESHOLD (0.3) over at least
    QUARANTINE_MIN_RUNS (4) runs in the window. Below that run floor the
    answer is always False — a single fail->pass on a two-run history reads
    as flake_score 1.0 but is noise, not a suite worth quarantining.
    """
    runs = _window_runs(test_id)
    if len(runs) < QUARANTINE_MIN_RUNS:
        return False
    return flake_score(test_id) >= QUARANTINE_SCORE_THRESHOLD


def list_tests() -> list[dict]:
    """Return a summary row per unique canonical test_id (see
    `_canonical_id`) — flag-spelling variants of one suite are grouped."""
    all_rows = _load_history(_history_path())
    seen: dict[str, list[dict]] = {}
    for r in all_rows:
        canon = _canonical_id(r.get("test_id", ""))
        seen.setdefault(canon, []).append(r)
    result = []
    for tid, rows in seen.items():
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


def report() -> dict:
    """Summarize the full store: how much was read, and which canonical ids
    are flaky. A report that cannot state its own denominator (rows_examined,
    ids_examined) is not trustworthy even when its verdict is right."""
    rows = _load_history(_history_path())
    tests = list_tests()
    return {
        "rows_examined": len(rows),
        "ids_examined": len(tests),
        "tests": tests,
        "flaky": [t for t in tests if t["flake_score"] > 0.0],
    }


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

    p_report = sub.add_parser(
        "report", help="Summarize the store: rows/ids examined and which ids are flaky"
    )
    p_report.add_argument("--json", dest="as_json", action="store_true")

    p_advise = sub.add_parser(
        "advise",
        help="Print a one-line flake advisory to stderr if test_id is flaky; silent otherwise, never touches stdout",
    )
    p_advise.add_argument("--test-id", required=True)

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

    elif args.cmd == "report":
        data = report()
        if args.as_json:
            print(json.dumps(data))
        else:
            print(f"rows_examined: {data['rows_examined']}")
            print(f"ids_examined : {data['ids_examined']}")
            print(f"flaky_ids    : {len(data['flaky'])}")
            for t in data["flaky"]:
                print(f"  {t['test_id']:<60} score={t['flake_score']:.3f} runs={t['runs']}")

    elif args.cmd == "advise":
        # Advisory only — stdout stays untouched so callers piping this
        # process's stdout into a manifest are never corrupted (D#2132).
        row = status(args.test_id)
        if row["flake_score"] > 0.0:
            print(
                f"flaky: {row['test_id']} flake_score={row['flake_score']:.3f} "
                f"quarantined={row['quarantined']}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
