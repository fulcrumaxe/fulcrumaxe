"""
backend/jsonl_rotator.py — JSONL file rotation utility.

Rotate a JSONL file when any configured threshold is exceeded:
  - max_size_mb: file size in megabytes
  - max_age_days: age of the file in days (mtime-based)
  - max_lines: number of lines in the file

On rotation: rename <path> → <path>.<YYYY-MM-DD-HHMMSS> then create empty original.
Archive pruning: keep at most keep_archives historical files (delete oldest by mtime).

Usage as a CLI module:
  python3 -m backend.jsonl_rotator rotate <path> [--max-size-mb N] [--max-age-days N] [--max-lines N] [--keep-archives N]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _file_age_days(path: str) -> float:
    """Return age of file in days based on mtime. Returns 0.0 if file doesn't exist."""
    try:
        mtime = os.path.getmtime(path)
        return (time.time() - mtime) / 86400.0
    except OSError:
        return 0.0


def _file_size_mb(path: str) -> float:
    """Return file size in MB. Returns 0.0 if file doesn't exist."""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def _count_lines(path: str) -> int:
    """Count lines in a file. Returns 0 if file doesn't exist or unreadable."""
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _archive_glob(path: str) -> list[str]:
    """Return list of archive files for path, sorted by mtime ascending (oldest first)."""
    pattern = path + ".*"
    candidates = []
    for p in glob.glob(pattern):
        # Only match our timestamp format: <path>.YYYY-MM-DD-HHMMSS
        suffix = p[len(path) :]
        if suffix.startswith(".") and len(suffix) == 18:  # .YYYY-MM-DD-HHMMSS = 18 chars total
            try:
                candidates.append((os.path.getmtime(p), p))
            except OSError:
                pass
    candidates.sort()
    return [p for _, p in candidates]


def _prune_archives(path: str, keep_archives: int) -> int:
    """Delete oldest archives beyond keep_archives. Returns number pruned."""
    archives = _archive_glob(path)
    pruned = 0
    while len(archives) > keep_archives:
        oldest = archives.pop(0)
        try:
            os.unlink(oldest)
            pruned += 1
        except OSError as e:
            print(f"[jsonl_rotator] WARN: could not delete archive {oldest}: {e}", file=sys.stderr)
            break
    return pruned


def rotate_if_needed(
    path: str,
    max_size_mb: Optional[int] = None,
    max_age_days: Optional[int] = None,
    max_lines: Optional[int] = None,
    keep_archives: int = 5,
) -> dict:
    """
    Rotate `path` if any threshold is exceeded.

    Returns:
        {
            "rotated": bool,
            "archive": str | None,   # path of archive file if rotated
            "pruned": int,           # number of old archives deleted
            "error": str | None,     # set on permission/lock errors (not raised)
        }
    """
    result: dict = {"rotated": False, "archive": None, "pruned": 0, "error": None}

    # All thresholds None → no-op
    if max_size_mb is None and max_age_days is None and max_lines is None:
        return result

    # File doesn't exist → nothing to rotate
    if not os.path.exists(path):
        return result

    # Evaluate thresholds
    should_rotate = False

    if max_size_mb is not None:
        size = _file_size_mb(path)
        if size >= max_size_mb:
            should_rotate = True

    if not should_rotate and max_age_days is not None:
        age = _file_age_days(path)
        if age >= max_age_days:
            should_rotate = True

    if not should_rotate and max_lines is not None:
        lines = _count_lines(path)
        if lines >= max_lines:
            should_rotate = True

    if not should_rotate:
        return result

    # Build archive path with timestamp
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    archive_path = f"{path}.{ts}"

    try:
        # Atomic rename — single inode swap, no copy
        os.rename(path, archive_path)
        # Create empty replacement
        Path(path).touch()
        result["rotated"] = True
        result["archive"] = archive_path
    except OSError as e:
        msg = f"rotation failed for {path}: {e}"
        print(f"[jsonl_rotator] WARN: {msg}", file=sys.stderr)
        result["error"] = msg
        return result

    # Prune oldest archives beyond keep_archives
    try:
        pruned = _prune_archives(path, keep_archives)
        result["pruned"] = pruned
    except Exception as e:
        print(f"[jsonl_rotator] WARN: archive pruning failed for {path}: {e}", file=sys.stderr)

    return result


# ── CLI entry point ──────────────────────────────────────────────────────────

def _cli_rotate(args: argparse.Namespace) -> int:
    result = rotate_if_needed(
        path=args.path,
        max_size_mb=args.max_size_mb,
        max_age_days=args.max_age_days,
        max_lines=args.max_lines,
        keep_archives=args.keep_archives,
    )
    print(json.dumps(result))
    if result.get("error"):
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="JSONL file rotation utility",
        prog="python3 -m backend.jsonl_rotator",
    )
    sub = parser.add_subparsers(dest="command")

    rot = sub.add_parser("rotate", help="Rotate a JSONL file if thresholds are exceeded")
    rot.add_argument("path", help="Path to the JSONL file")
    rot.add_argument("--max-size-mb", type=int, default=None, help="Rotate if file exceeds this size in MB")
    rot.add_argument("--max-age-days", type=int, default=None, help="Rotate if file is older than this many days")
    rot.add_argument("--max-lines", type=int, default=None, help="Rotate if file has more than this many lines")
    rot.add_argument("--keep-archives", type=int, default=5, help="Keep at most this many archive files (default: 5)")

    parsed = parser.parse_args(argv)

    if parsed.command == "rotate":
        return _cli_rotate(parsed)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
