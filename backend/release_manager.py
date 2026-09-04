"""
release_manager.py — library and CLI for release artifact management.

Writes .autonomous-team/releases/<id>.json after each PR merges.
Computes DORA metrics, classifies risk, and provides a rollback command.

Usage (library):
    from backend.release_manager import record_release, compute_dora_snapshot, classify_risk
    record = record_release(pr_numbers=[42])

Usage (CLI):
    python3 backend/release_manager.py record --pr 42
    python3 backend/release_manager.py record --pr 42 --dry-run
    python3 backend/release_manager.py list
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Allow running from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend._repo import REPO  # noqa: E402 (after sys.path.insert)

_RELEASES_DIR = REPO_ROOT / ".autonomous-team" / "releases"
_SCHEMA_PATH = REPO_ROOT / ".autonomous-team" / "schemas" / "release.schema.json"

# High-risk file patterns (touching these → risk=high)
_HIGH_RISK_PATHS = {"backend/server.py", "backend/api.py"}

# Label that must be present for non-high-risk classification
_REVIEW_LABEL = "code-review-passed"


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

def classify_risk(diff_files: list[str], pr_labels: list[str]) -> str:
    """Classify risk level for a set of changed files and PR labels.

    Rules (first match wins):
    1. Any file in _HIGH_RISK_PATHS → high
    2. No code-review-passed label → high
    3. Any file under backend/ → medium
    4. All files under wiki/ or *.md → low
    5. Default → medium
    """
    # Rule 1: high-risk paths
    for f in diff_files:
        if f in _HIGH_RISK_PATHS:
            return "high"

    # Rule 2: no review label → high
    if _REVIEW_LABEL not in pr_labels:
        return "high"

    # Rule 3: backend changes
    for f in diff_files:
        if f.startswith("backend/"):
            return "medium"

    # Rule 4: pure docs
    all_docs = all(
        f.startswith("wiki/") or f.endswith(".md")
        for f in diff_files
    ) if diff_files else False
    if all_docs and diff_files:
        return "low"

    return "medium"


# ---------------------------------------------------------------------------
# DORA metrics
# ---------------------------------------------------------------------------

def compute_dora_snapshot() -> dict:
    """Compute point-in-time DORA metrics from recent merge history.

    Returns a dict with:
      deploy_frequency_per_day  — releases/day over trailing 7 days
      lead_time_minutes_p50     — median minutes PR-open to merge, trailing 7 days
      change_failure_rate_pct   — % releases that triggered a bug fix within 24h

    Returns -1 for metrics where there is insufficient data.
    """
    snapshot: dict = {
        "deploy_frequency_per_day": -1.0,
        "lead_time_minutes_p50": -1.0,
        "change_failure_rate_pct": -1.0,
    }

    # Deploy frequency: count existing release files over trailing 7 days
    try:
        now = datetime.now(timezone.utc)
        cutoff_ts = now.timestamp() - 7 * 24 * 3600
        recent_releases = []
        if _RELEASES_DIR.exists():
            for rf in _RELEASES_DIR.glob("*.json"):
                try:
                    data = json.loads(rf.read_text(encoding="utf-8"))
                    # Explicitly skip records with null/missing merged_at —
                    # they have no timestamp so cannot be placed in any window.
                    if not data.get("merged_at"):
                        continue
                    merged_at = datetime.fromisoformat(
                        data["merged_at"].replace("Z", "+00:00")
                    )
                    if merged_at.timestamp() >= cutoff_ts:
                        recent_releases.append(data)
                except Exception:
                    pass
        freq = len(recent_releases) / 7.0
        snapshot["deploy_frequency_per_day"] = round(freq, 4)
    except Exception:
        pass

    # Lead time: query merged PRs from GitHub
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", REPO,
                "--state", "merged",
                "--json", "number,createdAt,mergedAt",
                "--limit", "50",
            ],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            now = datetime.now(timezone.utc)
            cutoff_ts = now.timestamp() - 7 * 24 * 3600
            lead_times = []
            for pr in prs:
                try:
                    created = datetime.fromisoformat(
                        pr["createdAt"].replace("Z", "+00:00")
                    )
                    merged = datetime.fromisoformat(
                        pr["mergedAt"].replace("Z", "+00:00")
                    )
                    if merged.timestamp() >= cutoff_ts:
                        lead_times.append((merged - created).total_seconds() / 60.0)
                except Exception:
                    pass
            if lead_times:
                lead_times.sort()
                mid = len(lead_times) // 2
                p50 = (
                    lead_times[mid]
                    if len(lead_times) % 2 == 1
                    else (lead_times[mid - 1] + lead_times[mid]) / 2.0
                )
                snapshot["lead_time_minutes_p50"] = round(p50, 2)
    except Exception:
        pass

    # Change failure rate: fraction of recent releases with a follow-up bug fix within 24h
    # Heuristic: count [Bug] discussions created within 24h of a release's merged_at
    # We use -1 when we cannot determine this reliably.
    # (Full implementation deferred until bug-filing rate is tracked in stats.duckdb)
    snapshot["change_failure_rate_pct"] = -1.0

    return snapshot


# ---------------------------------------------------------------------------
# Release ID generation
# ---------------------------------------------------------------------------

def _next_release_id(date_str: str) -> str:
    """Generate the next sequential release ID for today.

    Format: YYYY-MM-DD-NNN (zero-padded 3-digit sequence within the day).
    """
    _RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(_RELEASES_DIR.glob(f"{date_str}-*.json"))
    seq = len(existing) + 1
    return f"{date_str}-{seq:03d}"


# ---------------------------------------------------------------------------
# Core record function
# ---------------------------------------------------------------------------

def record_release(
    pr_numbers: list[int],
    dry_run: bool = False,
    merged_at: str | None = None,
) -> dict:
    """Build a release record for the given PR(s) and write it to disk.

    Parameters
    ----------
    pr_numbers:
        List of PR numbers included in this release.
    dry_run:
        If True, compute the record but do NOT write it to disk.
    merged_at:
        Optional ISO-8601 UTC timestamp (with Z suffix) for the merge date.
        When provided, this value is used as-is and the per-PR gh pr view
        call for mergedAt is skipped. Callers (e.g. release_backfill.py)
        should pass a value obtained from a bulk gh pr list call to avoid
        GitHub secondary rate-limits.
        When None (default), mergedAt is fetched from GitHub via gh pr view.

    Returns
    -------
    dict
        The release record matching release.schema.json.
    """
    # Idempotency: if any existing record already contains ALL of these PRs
    # (membership check), return that record instead of writing a duplicate.
    if not dry_run and _RELEASES_DIR.exists():
        pr_set = set(pr_numbers)
        for rf in sorted(_RELEASES_DIR.glob("*.json")):
            try:
                existing = json.loads(rf.read_text(encoding="utf-8"))
                existing_prs = set(existing.get("pr_numbers", []))
                if pr_set.issubset(existing_prs):
                    return existing
            except Exception:
                pass

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    release_id = _next_release_id(date_str)

    # Collect PR metadata from GitHub
    diff_files: list[str] = []
    merge_shas: list[str] = []
    pr_labels: list[str] = []

    # Use caller-supplied merged_at when available (avoids per-PR gh calls).
    # When None, fall back to per-PR gh pr view (live path).
    merged_at_str: str | None = merged_at

    for pr_num in pr_numbers:
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "view", str(pr_num),
                    "--repo", REPO,
                    "--json", "files,labels,mergeCommit,mergedAt",
                ],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                pr_data = json.loads(result.stdout)
                diff_files.extend(f["path"] for f in pr_data.get("files", []))
                pr_labels.extend(lbl["name"] for lbl in pr_data.get("labels", []))
                mc = pr_data.get("mergeCommit")
                if mc and mc.get("oid"):
                    merge_shas.append(mc["oid"])
                # Only read mergedAt from gh pr view when not overridden by caller
                if merged_at_str is None:
                    ma = pr_data.get("mergedAt")
                    if ma:
                        merged_at_str = ma.replace("Z", "+00:00").replace("+00:00", "Z")
            else:
                logger.warning(
                    "gh pr view failed for PR %s (exit %d) — merged_at will be null",
                    pr_num,
                    result.returncode,
                )
        except Exception as exc:
            logger.warning(
                "gh pr view raised an exception for PR %s — merged_at will be null: %s",
                pr_num,
                exc,
            )

    # If merged_at is still None after all gh calls, leave it None.
    # Callers (backfill) supply it explicitly; live calls that fail gh pr view
    # must NOT stamp datetime.now() — that would silently corrupt DORA metrics.

    risk = classify_risk(diff_files, pr_labels)
    runbook_needed = risk == "high"

    # Build rollback command from the first merge SHA
    if merge_shas:
        rollback_command = f"git revert {merge_shas[0]} --no-edit"
    else:
        rollback_command = f"git revert HEAD --no-edit  # sha unknown for PR #{pr_numbers[0]}"

    dora = compute_dora_snapshot()

    record: dict = {
        "id": release_id,
        "pr_numbers": pr_numbers,
        "merged_at": merged_at_str,
        "merge_shas": merge_shas,
        "risk": risk,
        "rollback_command": rollback_command,
        "runbook_needed": runbook_needed,
        "dora_snapshot": dora,
    }

    if runbook_needed:
        record["follow_up_spawns"] = ["runbook-writer"]

    if not dry_run:
        _RELEASES_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _RELEASES_DIR / f"{release_id}.json"
        out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Release artifact manager.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    record_cmd = sub.add_parser("record", help="Record a new release for a PR.")
    record_cmd.add_argument("--pr", required=True, type=int, help="PR number.")
    record_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the release record without writing to disk.",
    )

    sub.add_parser("list", help="List all release records ordered by date.")

    return parser.parse_args(argv)


def _cmd_record(args: argparse.Namespace) -> int:
    record = record_release(pr_numbers=[args.pr], dry_run=args.dry_run)
    print(json.dumps(record, indent=2))
    if args.dry_run:
        print("\n[dry-run] Release record NOT written to disk.", file=sys.stderr)
    else:
        out_path = _RELEASES_DIR / f"{record['id']}.json"
        print(f"\n[release-manager] Written: {out_path}", file=sys.stderr)
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    if not _RELEASES_DIR.exists():
        print("No releases directory found.", file=sys.stderr)
        return 0
    records = []
    for rf in sorted(_RELEASES_DIR.glob("*.json")):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            records.append(data)
        except Exception as exc:
            print(f"Warning: could not parse {rf}: {exc}", file=sys.stderr)

    records.sort(key=lambda r: r.get("merged_at") or "")
    for rec in records:
        risk = rec.get("risk", "?")
        release_id = rec.get("id", "?")
        prs = rec.get("pr_numbers", [])
        merged_at = rec.get("merged_at", "?")
        print(f"{release_id}  risk={risk}  prs={prs}  merged_at={merged_at}")
    if not records:
        print("No releases recorded yet.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "record":
        return _cmd_record(args)
    if args.command == "list":
        return _cmd_list(args)
    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
