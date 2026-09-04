#!/usr/bin/env python3
"""One-shot migration: fix corrupt duration_s / duration_seconds rows in loop-metrics.jsonl.

A corrupt row is one where the duration field exceeds 86400 seconds (24 hours),
which indicates the Unix epoch timestamp was written instead of an elapsed-seconds
delta (e.g. 1_778_856_462 instead of 300).

The fix: rewrite corrupt rows in-place with duration_s=0 and adds corrupt=true so
the chart clamp in server.py never needs to touch them again.  All other fields
are preserved unchanged.

Usage:
    python3 scripts/migrate-loop-metrics.py [--dry-run] [--metrics-file PATH]

Options:
    --dry-run          Print what would change; do not write.
    --metrics-file     Path to loop-metrics.jsonl (default: .autonomous-team/loop-metrics.jsonl)

Exit codes:
    0  migration complete (or nothing to migrate)
    1  fatal error (file not found, JSON parse failure on all lines, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_METRICS = _REPO_ROOT / ".autonomous-team" / "loop-metrics.jsonl"
_MAX_SANE_DURATION = 86_400  # 24 hours


def _duration_value(row: dict) -> float | None:
    """Return the numeric duration from a row, supporting both field names."""
    val = row.get("duration_s")
    if val is None:
        val = row.get("duration_seconds")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def migrate(metrics_file: Path, dry_run: bool) -> tuple[int, int]:
    """Rewrite corrupt duration rows.

    Returns (total_rows, corrupt_rows_fixed).
    """
    if not metrics_file.exists():
        print(f"migrate-loop-metrics: file not found: {metrics_file}", file=sys.stderr)
        sys.exit(1)

    lines = metrics_file.read_text(encoding="utf-8").splitlines(keepends=True)
    total = 0
    fixed = 0
    output_lines: list[str] = []

    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped:
            output_lines.append(line)
            continue

        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            # Preserve malformed lines as-is; they're handled by readers already.
            output_lines.append(line)
            continue

        total += 1
        dur = _duration_value(row)

        if dur is not None and dur > _MAX_SANE_DURATION:
            fixed += 1
            ts = row.get("timestamp") or row.get("ts", "?")
            print(
                f"  corrupt: ts={ts}  old_duration={dur:.0f}  -> duration_s=0, corrupt=true"
            )
            # Rewrite: zero out the duration, mark as corrupt, use canonical field name.
            row.pop("duration_seconds", None)
            row["duration_s"] = 0
            row["corrupt"] = True
            if not dry_run:
                output_lines.append(json.dumps(row) + "\n")
            else:
                # Keep original line content so dry-run is truly non-destructive.
                output_lines.append(line)
        else:
            output_lines.append(line)

    if not dry_run and fixed > 0:
        # Write atomically: write to a temp file then rename.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=metrics_file.parent,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            tmp.writelines(output_lines)
            tmp_path = Path(tmp.name)
        tmp_path.replace(metrics_file)

    return total, fixed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix corrupt duration rows in loop-metrics.jsonl."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing.")
    parser.add_argument(
        "--metrics-file",
        type=Path,
        default=_DEFAULT_METRICS,
        help=f"Path to loop-metrics.jsonl (default: {_DEFAULT_METRICS})",
    )
    args = parser.parse_args()

    print(f"migrate-loop-metrics: scanning {args.metrics_file}")
    if args.dry_run:
        print("  (dry-run mode -- no changes will be written)")

    total, fixed = migrate(args.metrics_file, dry_run=args.dry_run)

    if fixed == 0:
        print(f"  no corrupt rows found in {total} rows -- nothing to do")
    elif args.dry_run:
        print(f"  would fix {fixed}/{total} rows")
    else:
        print(f"  fixed {fixed}/{total} rows")


if __name__ == "__main__":
    main()
