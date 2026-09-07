#!/usr/bin/env python3
"""scripts/migrate-status-new-discussions.py — D#2436 PR-b live migration
runner.

Fixes the ~40 open Discussions that declare the invented ``NEW`` status
(across five measured shapes — see backend/status_migration.py) so their
authoritative marker reads a status the state machine actually acts on.

This is the one part of D#2436's fix that necessarily talks to GitHub — the
migration logic itself (backend/status_migration.py) is pure and covered by
offline unit tests; this script is thin I/O around it: enumerate, migrate,
write, read back, verify.

Discussion plane only (autonomous-agent-7/fulcrumaxe, or whatever
backend._repo.DISCUSSION_REPO resolves to). No code-plane writes.

Usage:
    # Dry run (default) — reports what would change, writes nothing.
    python3 scripts/migrate-status-new-discussions.py

    # Apply — writes up to MIGRATION_BATCH_CAP Discussions, verifies each
    # against a fresh re-fetch from GitHub, and reports actionable-open
    # counts before and after.
    python3 scripts/migrate-status-new-discussions.py --apply

Exit codes:
    0   ran to completion (dry run, or apply with every write verified)
    1   zero bodies examined (vacuous scan), or at least one write failed
        read-back verification
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend._repo import DISCUSSION_REPO  # noqa: E402
from backend.discussion_status import extract_status_anchored  # noqa: E402
from backend.status_migration import (  # noqa: E402
    MIGRATION_BATCH_CAP,
    MIGRATION_TARGET_STATUS,
    content_preserved,
    migrate_body,
    needs_migration,
    verify_migrated,
)

ACTIONABLE_STATUSES = {"DISCUSSING", "SPEC_READY"}


def _registry_status(body: str) -> str:
    """Mirror backend.registry.DiscussionRegistry._parse_status's fail-open
    mapping: an unanchored body (UNKNOWN) counts as DISCUSSING, matching
    what the live registry/queue actually reports."""
    status = extract_status_anchored(body)
    return "DISCUSSING" if status == "UNKNOWN" else status


def actionable_open_count(discussions: list[dict]) -> int:
    return sum(1 for d in discussions if _registry_status(d["body"]) in ACTIONABLE_STATUSES)


def _gh_graphql(query: str, timeout: int = 30) -> dict:
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api graphql failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def fetch_all_open() -> list[dict]:
    """One paginated GraphQL walk over every OPEN Discussion on the
    Discussion plane. Returns [{id, number, body}, ...]."""
    owner, name = DISCUSSION_REPO.split("/", 1)
    out: list[dict] = []
    cursor: str | None = None
    while True:
        after = f', after:"{cursor}"' if cursor else ""
        query = (
            f'query {{ repository(owner:"{owner}", name:"{name}") {{'
            f" discussions(first:50, states:[OPEN]{after}) {{"
            " pageInfo { hasNextPage endCursor }"
            " nodes { id number body } } } }"
        )
        data = _gh_graphql(query)
        disc = data["data"]["repository"]["discussions"]
        out.extend(disc["nodes"])
        if not disc["pageInfo"]["hasNextPage"]:
            break
        cursor = disc["pageInfo"]["endCursor"]
    return out


def fetch_one_body(node_id: str) -> str:
    """Fresh (uncached) re-fetch of a single Discussion's body by node id —
    the read-back verification D#2436 Spec item 12 requires never trusts the
    local pre-write string."""
    query = f'query {{ node(id:"{node_id}") {{ ... on Discussion {{ body }} }} }}'
    data = _gh_graphql(query)
    return data["data"]["node"]["body"]


def write_body(node_id: str, new_body: str) -> None:
    """updateDiscussion mutation — body passed via a temp file (-F @path)
    so arbitrary Discussion content (quotes, backticks, newlines) never
    round-trips through shell quoting."""
    query = (
        "mutation($id:ID!,$body:String!){ "
        "updateDiscussion(input:{discussionId:$id, body:$body}) { discussion { id } } }"
    )
    fd, body_path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_body)
        result = subprocess.run(
            [
                "gh", "api", "graphql",
                "-f", f"query={query}",
                "-f", f"id={node_id}",
                "-F", f"body=@{body_path}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"updateDiscussion failed for {node_id}: {result.stderr.strip()}")
    finally:
        os.unlink(body_path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform writes (default: dry run)")
    args = ap.parse_args()

    print(f"Discussion plane: {DISCUSSION_REPO}")
    print("Fetching all OPEN Discussions...")
    discussions = fetch_all_open()
    print(f"examined {len(discussions)} open Discussion bodies")
    if len(discussions) == 0:
        print("FAIL: zero bodies examined — refusing to report a vacuous scan")
        return 1

    before_count = actionable_open_count(discussions)
    print(f"actionable-open count BEFORE: {before_count}")

    candidates = sorted(
        (d for d in discussions if needs_migration(d["body"])),
        key=lambda d: d["number"],
    )
    print(f"candidates declaring NEW: {len(candidates)}")
    for d in candidates:
        print(f"  D#{d['number']}")

    if not candidates:
        print("nothing to migrate")
        return 0

    batch = candidates[:MIGRATION_BATCH_CAP]
    remainder = candidates[MIGRATION_BATCH_CAP:]
    print(f"batch (cap={MIGRATION_BATCH_CAP}): {[d['number'] for d in batch]}")
    if remainder:
        print(f"deferred to a later run: {[d['number'] for d in remainder]}")

    if not args.apply:
        print("DRY RUN — no writes performed. Pass --apply to write.")
        for d in batch:
            migrate_body(d["body"])  # exercised, not written — proves the batch is migratable
            print(f"  D#{d['number']}: would set status -> {MIGRATION_TARGET_STATUS}")
        return 0

    written: list[int] = []
    failed: list[tuple[int, list[str], bool]] = []
    for d in batch:
        pre_body = d["body"]
        new_body = migrate_body(pre_body)
        write_body(d["id"], new_body)
        time.sleep(1)  # brief pause before the read-back re-fetch
        fetched = fetch_one_body(d["id"])
        ok, problems = verify_migrated(fetched)
        preserved = content_preserved(pre_body, fetched)
        if ok and preserved:
            written.append(d["number"])
            print(f"  D#{d['number']}: OK -> {MIGRATION_TARGET_STATUS}")
        else:
            failed.append((d["number"], problems, preserved))
            print(f"  D#{d['number']}: VERIFICATION FAILED problems={problems} content_preserved={preserved}")

    print(f"written: {len(written)}  failed: {len(failed)}  deferred: {len(remainder)}")

    print("Re-fetching all OPEN Discussions for the after-count...")
    after_discussions = fetch_all_open()
    after_count = actionable_open_count(after_discussions)
    print(f"actionable-open count BEFORE: {before_count}")
    print(f"actionable-open count AFTER:  {after_count}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
