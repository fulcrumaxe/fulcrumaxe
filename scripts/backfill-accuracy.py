#!/usr/bin/env python3
"""
backfill-accuracy.py — backfill actual_hours (and inferred estimated_hours) on
closed Discussions from the last 30 days.

For each closed Discussion that is missing a <!-- COMPLETION --> block:
  1. Find the linked merged PR via gh api.
  2. Compute actual_hours = (merged_at - discussion_created_at) / 3600.
  3. Append a <!-- COMPLETION --> block to the Discussion body via GraphQL.
  4. Write a frontmatter _inferred:true estimated_hours derived from
     time_to_merge_seconds / 3600 rounded (cheap heuristic, flagged so it
     won't pollute accuracy stats once real estimates exist).

Rate-limit policy: if any gh call returns HTTP 403 or "secondary rate limit",
stop immediately and return exit code 2. NO sleep loops.

Idempotency: Discussions that already contain <!-- COMPLETION --> are skipped.

Usage:
    python3 scripts/backfill-accuracy.py [--dry-run] [--days N] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import os
import json as _json


def _load_repo() -> str:
    """Resolve repo from config.json → env → fail loudly.

    Matches the precedence in scripts/lib/repo-resolve.sh and
    ts-backend/src/config/repo.ts's resolveRepo(): config.json's "repo"
    field wins first so this script stays consistent even when a stale
    .autonomous-team/project.json is also present (see D#1635 Wave 1
    review — project.json used to shadow config.json here).

    No hard-coded slug fallback: .autonomous-team/ never ships in the
    open-source export, so a forked adopter with neither config.json nor
    the env var set gets an actionable error instead of silently
    inheriting this project's own repo slug (D#1870).
    """
    cj = Path(__file__).resolve().parent.parent / ".autonomous-team" / "config.json"
    try:
        data = _json.load(cj.open())
        r = data.get("repo")
        if r:
            return r
    except (OSError, ValueError):
        pass
    env_repo = os.environ.get("AUTONOMOUS_TEAM_REPO")
    if env_repo:
        return env_repo
    raise RuntimeError(
        "backfill-accuracy.py: could not resolve a repo slug. Set "
        "AUTONOMOUS_TEAM_REPO or add a \"repo\" field to "
        ".autonomous-team/config.json."
    )


REPO = _load_repo()
OWNER, NAME = REPO.split("/", 1) if "/" in REPO else (REPO, REPO)

COMPLETION_MARKER = "<!-- COMPLETION -->"

# ─────────────────────────── helpers ────────────────────────────────────────


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _gh(*args: str) -> tuple[int, str]:
    """Run a gh CLI call.  Returns (returncode, stdout_or_stderr_text)."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).strip()
        return result.returncode, combined
    return 0, result.stdout.strip()


def _is_rate_limited(text: str) -> bool:
    indicators = ["secondary rate limit", "rate limit", "403"]
    lower = text.lower()
    return any(ind in lower for ind in indicators)


# ─────────────────────────── GitHub queries ─────────────────────────────────


def fetch_closed_discussions(days: int) -> tuple[int, list[dict[str, Any]]]:
    """Return (rc, [discussion...]) for closed Discussions in the last N days.

    Rate-limit: returns rc=2 on 403.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    query = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 100, after: $cursor, states: [CLOSED], orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        body
        createdAt
        closedAt
      }
    }
  }
}
"""
    discussions: list[dict[str, Any]] = []
    cursor = None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    while True:
        variables: dict[str, Any] = {"owner": OWNER, "name": NAME}
        if cursor:
            variables["cursor"] = cursor

        rc, out = _gh(
            "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={OWNER}",
            "-f", f"name={NAME}",
            *([] if not cursor else ["-f", f"cursor={cursor}"]),
        )
        if rc != 0:
            if _is_rate_limited(out):
                return 2, discussions
            return rc, discussions

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return 1, discussions

        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("discussions", {})
            .get("nodes", [])
        )
        page_info = (
            data.get("data", {})
            .get("repository", {})
            .get("discussions", {})
            .get("pageInfo", {})
        )

        stop_pagination = False
        for disc in nodes:
            created = _parse_iso(disc.get("createdAt", ""))
            if created and created < cutoff:
                stop_pagination = True
                break
            discussions.append(disc)

        if stop_pagination or not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return 0, discussions


def find_linked_pr(disc_number: int) -> tuple[int, dict[str, Any] | None]:
    """Find the most-recently merged PR that references this Discussion.

    Returns (rc, pr_data | None). rc=2 means rate-limited.
    Uses REST search endpoint (simpler, no GraphQL needed).
    """
    rc, out = _gh(
        "api",
        f"repos/{REPO}/pulls",
        "--method", "GET",
        "-f", "state=closed",
        "-f", "per_page=50",
    )
    if rc != 0:
        if _is_rate_limited(out):
            return 2, None
        return rc, None

    try:
        prs = json.loads(out)
    except json.JSONDecodeError:
        return 1, None

    # Match PRs whose title or body reference #<disc_number>
    pattern = re.compile(rf"(?:^|\D)#{disc_number}(?:\D|$)")
    for pr in prs:
        title = pr.get("title") or ""
        body = pr.get("body") or ""
        if pattern.search(title) or pattern.search(body):
            if pr.get("merged_at"):
                return 0, pr

    # Also try searching by title pattern disc-<number>
    title_pattern = re.compile(rf"disc[^0-9]*{disc_number}", re.IGNORECASE)
    for pr in prs:
        title = pr.get("title") or ""
        if title_pattern.search(title) and pr.get("merged_at"):
            return 0, pr

    return 0, None  # not found is not an error


def update_discussion_body(disc_id: str, new_body: str) -> tuple[int, str]:
    """Update a Discussion body via GraphQL mutation."""
    mutation = """
mutation($discussionId: ID!, $body: String!) {
  updateDiscussion(input: {discussionId: $discussionId, body: $body}) {
    discussion { id number }
  }
}
"""
    rc, out = _gh(
        "api", "graphql",
        "-f", f"query={mutation}",
        "-f", f"discussionId={disc_id}",
        "-f", f"body={new_body}",
    )
    if rc != 0:
        return rc, out
    return 0, out


# ─────────────────────────── core logic ─────────────────────────────────────


def build_completion_block(
    actual_hours: float,
    merged_at: str,
    merged_pr: int | None,
    inferred_estimated_hours: float,
) -> str:
    lines = [
        COMPLETION_MARKER,
        f"actual_hours: {round(actual_hours, 2)}",
        f"merged_at: {merged_at}",
    ]
    if merged_pr:
        lines.append(f"merged_pr: {merged_pr}")
    lines.append(f"estimated_hours: {round(inferred_estimated_hours, 1)}")
    lines.append("_inferred: true")
    lines.append("<!-- /COMPLETION -->")
    return "\n".join(lines)


def process_discussion(
    disc: dict[str, Any],
    *,
    dry_run: bool,
    verbose: bool,
) -> str:
    """Process one Discussion.  Returns a status string."""
    number = disc["number"]
    disc_id = disc["id"]
    body = disc.get("body") or ""
    created_at = disc.get("createdAt", "")

    # Idempotency: skip if already has a COMPLETION block
    if COMPLETION_MARKER in body:
        if verbose:
            print(f"  D#{number}: already has COMPLETION block — skipping")
        return "skip"

    # Find merged PR
    rc, pr = find_linked_pr(number)
    if rc == 2:
        return "rate_limited"
    if rc != 0 or pr is None:
        if verbose:
            print(f"  D#{number}: no linked merged PR found — skipping")
        return "no_pr"

    merged_at = pr.get("merged_at", "")
    pr_number = pr.get("number")

    created_dt = _parse_iso(created_at)
    merged_dt = _parse_iso(merged_at)

    if not created_dt or not merged_dt:
        if verbose:
            print(f"  D#{number}: could not parse timestamps — skipping")
        return "bad_timestamps"

    elapsed_seconds = (merged_dt - created_dt).total_seconds()
    if elapsed_seconds <= 0:
        if verbose:
            print(f"  D#{number}: merged_at <= created_at — skipping")
        return "bad_timestamps"

    actual_hours = elapsed_seconds / 3600.0
    # Heuristic estimated_hours: use time_to_merge / 3600 rounded to nearest 0.5
    raw_est = elapsed_seconds / 3600.0
    inferred_est = max(0.5, round(raw_est * 2) / 2)  # round to nearest 0.5h

    completion_block = build_completion_block(
        actual_hours=actual_hours,
        merged_at=merged_at,
        merged_pr=pr_number,
        inferred_estimated_hours=inferred_est,
    )

    new_body = body.rstrip() + "\n\n" + completion_block

    print(
        f"  D#{number}: actual_hours={actual_hours:.2f}  "
        f"inferred_est={inferred_est}h  "
        f"PR #{pr_number}"
        + ("  [dry-run]" if dry_run else "")
    )

    if dry_run:
        return "dry_run"

    rc, out = update_discussion_body(disc_id, new_body)
    if rc == 2 or _is_rate_limited(out):
        return "rate_limited"
    if rc != 0:
        print(f"  D#{number}: update failed: {out}", file=sys.stderr)
        return "error"

    return "updated"


# ─────────────────────────── main ───────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill actual_hours + inferred estimated_hours on closed Discussions"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without writing to GitHub",
    )
    parser.add_argument(
        "--days",
        "--since-days",
        type=int,
        default=30,
        help="How far back to look (default: 30 days)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N discussions (0 = unlimited)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print skipped discussions",
    )
    args = parser.parse_args()

    print(
        f"Backfilling accuracy for closed Discussions "
        f"(last {args.days} days)"
        + (" [DRY RUN]" if args.dry_run else "")
    )

    rc, discussions = fetch_closed_discussions(args.days)
    if rc == 2:
        print("ERROR: GitHub rate-limited — aborting.", file=sys.stderr)
        return 2
    if rc != 0:
        print(f"ERROR: could not fetch discussions (rc={rc})", file=sys.stderr)
        return 1

    print(f"Found {len(discussions)} closed discussion(s) in the last {args.days} days")

    counts: dict[str, int] = {
        "updated": 0,
        "skip": 0,
        "no_pr": 0,
        "dry_run": 0,
        "bad_timestamps": 0,
        "error": 0,
    }

    for i, disc in enumerate(discussions):
        if args.limit and i >= args.limit:
            print(f"(--limit {args.limit} reached, stopping)")
            break

        status = process_discussion(disc, dry_run=args.dry_run, verbose=args.verbose)

        if status == "rate_limited":
            print("ERROR: GitHub rate-limited mid-run — aborting.", file=sys.stderr)
            _print_summary(counts)
            return 2

        counts[status] = counts.get(status, 0) + 1

    _print_summary(counts)
    return 0


def _print_summary(counts: dict[str, int]) -> None:
    print(
        f"\nSummary: updated={counts.get('updated',0)}  "
        f"dry_run={counts.get('dry_run',0)}  "
        f"skipped(already done)={counts.get('skip',0)}  "
        f"no_pr={counts.get('no_pr',0)}  "
        f"bad_timestamps={counts.get('bad_timestamps',0)}  "
        f"errors={counts.get('error',0)}"
    )


if __name__ == "__main__":
    sys.exit(main())
