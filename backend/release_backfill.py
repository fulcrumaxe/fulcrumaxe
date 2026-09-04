"""
release_backfill.py — One-shot backfill of release records from git log.

Walks git log for squash-merge commit subjects matching "(#NNN)" over a
--since window, then calls record_release() for each PR not already in
.autonomous-team/releases/.

Date sourcing: merge dates are bulk-fetched in a SINGLE gh pr list call
(not one gh pr view per PR). This avoids GitHub secondary rate-limits
that previously corrupted ~200 records with datetime.now() timestamps.

Idempotency: pre-builds the set of already-recorded pr_numbers from
existing release files and skips PRs already present. record_release()
is NOT idempotent by itself (IDs are date-seq), so the guard lives here.

Usage:
    python3 backend/release_backfill.py [--since 7d] [--dry-run]
    python3 backend/release_backfill.py --clear [--since 7d]  # wipe + re-run

Options:
    --since  Age of oldest merge to include, e.g. "7d", "30d", "90d".
             Default: 7d
    --dry-run  Print what would be written without touching disk.
    --clear    Delete all existing release files before backfilling.
               Use this to fix corrupted records.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.release_manager import REPO, record_release  # noqa: E402

_RELEASES_DIR = REPO_ROOT / ".autonomous-team" / "releases"

# Matches squash-merge subjects like "fix some thing (#1234)"
_PR_NUMBER_RE = re.compile(r"\(#(\d+)\)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_since(since: str) -> datetime:
    """Parse a duration string like '7d', '30d' into an absolute UTC datetime."""
    m = re.fullmatch(r"(\d+)d", since.strip())
    if not m:
        raise ValueError(f"--since must be in the form Nd (e.g. 7d), got: {since!r}")
    days = int(m.group(1))
    return datetime.now(timezone.utc) - timedelta(days=days)


def _already_recorded_pr_numbers() -> set[int]:
    """Return the set of PR numbers already present in release files."""
    recorded: set[int] = set()
    if not _RELEASES_DIR.exists():
        return recorded
    for rf in _RELEASES_DIR.glob("*.json"):
        if rf.name == ".gitkeep":
            continue
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            for pr_num in data.get("pr_numbers", []):
                recorded.add(int(pr_num))
        except Exception:
            pass
    return recorded


def _git_log_pr_numbers(since: datetime) -> list[int]:
    """Return PR numbers from squash-merge subjects since the given datetime.

    Parses git log subjects for the pattern "(#NNN)" — the format GitHub
    uses for squash-merge commit messages.
    """
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = subprocess.run(
        [
            "git", "log",
            f"--after={since_iso}",
            "--oneline",
            "--format=%s",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git log failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    pr_numbers: list[int] = []
    seen: set[int] = set()
    for line in result.stdout.splitlines():
        for m in _PR_NUMBER_RE.finditer(line):
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                pr_numbers.append(n)
    return pr_numbers


def _bulk_fetch_merged_at(pr_numbers: list[int]) -> dict[int, str]:
    """Fetch real mergedAt timestamps for a list of PR numbers in O(few) gh calls.

    Uses gh pr list --json number,mergedAt with a high --limit to retrieve
    all merged PRs in one or two paginated requests. This avoids per-PR
    gh pr view calls (which hit GitHub secondary rate limits after ~90 calls).

    Returns
    -------
    dict mapping pr_number (int) to mergedAt ISO-8601 string (with Z suffix).
    PRs not found in the bulk result are absent from the returned dict.
    Callers MUST skip (not now()-stamp) PRs missing from this map.
    """
    if not pr_numbers:
        return {}

    merged_at_map: dict[int, str] = {}

    # One bulk call with limit 1000 covers typical repos. For repos with
    # >1000 PRs in the window, use --search to narrow the date range.
    result = subprocess.run(
        [
            "gh", "pr", "list",
            "--repo", REPO,
            "--state", "merged",
            "--json", "number,mergedAt",
            "--limit", "1000",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr list failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse gh pr list output: {exc}") from exc

    target_set = set(pr_numbers)
    for pr in prs:
        num = pr.get("number")
        ma = pr.get("mergedAt")
        if num is not None and ma and int(num) in target_set:
            # Normalise to Z suffix
            merged_at_map[int(num)] = ma.replace("+00:00", "Z")

    return merged_at_map


def _clear_release_files() -> int:
    """Delete all existing release JSON files. Returns count deleted."""
    if not _RELEASES_DIR.exists():
        return 0
    deleted = 0
    for rf in _RELEASES_DIR.glob("*.json"):
        if rf.name == ".gitkeep":
            continue
        rf.unlink()
        deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Core backfill function
# ---------------------------------------------------------------------------

def backfill(since_str: str = "7d", dry_run: bool = False) -> dict:
    """Run the backfill and return a summary dict.

    Date sourcing: ONE bulk gh pr list call fills the merged_at map.
    Per-PR gh pr view calls are NOT made from this module.

    Returns
    -------
    dict with keys:
        discovered  — total PR numbers found in git log
        skipped     — already recorded (idempotent) or missing from bulk map
        skipped_no_date — PRs absent from bulk map (logged as warnings)
        written     — new records written (0 when dry_run=True)
        errors      — list of (pr_num, error_message) tuples
    """
    since_dt = _parse_since(since_str)
    already = _already_recorded_pr_numbers()

    all_pr_numbers = _git_log_pr_numbers(since_dt)

    to_backfill = [n for n in all_pr_numbers if n not in already]
    skipped_already = len(all_pr_numbers) - len(to_backfill)

    # Bulk-fetch real mergedAt dates — O(1) gh call instead of O(N)
    merged_at_map: dict[int, str] = {}
    if to_backfill:
        merged_at_map = _bulk_fetch_merged_at(to_backfill)

    written = 0
    errors: list[tuple[int, str]] = []
    skipped_no_date: list[int] = []

    for pr_num in to_backfill:
        real_merged_at = merged_at_map.get(pr_num)
        if real_merged_at is None:
            # PR missing from bulk map — skip, never now()-stamp
            warnings.warn(
                f"PR #{pr_num}: no mergedAt from bulk fetch — SKIPPING (will not stamp now())",
                stacklevel=2,
            )
            skipped_no_date.append(pr_num)
            continue

        try:
            record_release(pr_numbers=[pr_num], merged_at=real_merged_at, dry_run=dry_run)
            if not dry_run:
                written += 1
        except Exception as exc:
            errors.append((pr_num, str(exc)))

    return {
        "discovered": len(all_pr_numbers),
        "skipped": skipped_already + len(skipped_no_date),
        "skipped_no_date": skipped_no_date,
        "written": written,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill release records from git log.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--since",
        default="7d",
        help="Backfill window, e.g. '7d', '30d'. Default: 7d",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching disk.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete all existing release files before backfilling (fixes corrupted records).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.clear and not args.dry_run:
        deleted = _clear_release_files()
        print(f"[clear] Deleted {deleted} existing release files.")

    try:
        result = backfill(since_str=args.since, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    mode = "[dry-run] " if args.dry_run else ""
    print(
        f"{mode}backfill since={args.since}: "
        f"discovered={result['discovered']} "
        f"skipped={result['skipped']} "
        f"written={result['written']} "
        f"errors={len(result['errors'])}"
    )
    if result["skipped_no_date"]:
        print(
            f"  WARNING: {len(result['skipped_no_date'])} PRs had no mergedAt in bulk fetch "
            f"and were SKIPPED (not stamped with now()): {result['skipped_no_date']}",
            file=sys.stderr,
        )
    if result["errors"]:
        for pr_num, msg in result["errors"]:
            print(f"  ERROR pr#{pr_num}: {msg}", file=sys.stderr)
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
