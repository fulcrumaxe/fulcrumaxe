#!/usr/bin/env bash
# scripts/cron/cleanup-pre-845-pm-dupes.sh — one-shot cleanup of orphaned
# project-manager rows left by the triple-spawn race fixed in PR #838+#845.
#
# Those PMs were never completed (end_ts IS NULL) and started before the
# PR #845 merge at 2026-05-14 10:25Z.  We mark them 'superseded' so they
# disappear from Stuck Runs without losing the historical start_ts.
#
# Usage:
#   bash scripts/cron/cleanup-pre-845-pm-dupes.sh
#   bash scripts/cron/cleanup-pre-845-pm-dupes.sh --dry-run
#
# Exit codes:
#   0 — completed successfully
#   1 — unexpected error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

python3 - "$DRY_RUN" "$REPO_ROOT" <<'PYEOF'
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

dry_run = sys.argv[1] == "1"
repo_root = sys.argv[2]
sys.path.insert(0, repo_root)

# PR #845 merge time — rows started before this cutoff are candidates.
CUTOFF = datetime(2026, 5, 14, 10, 25, 0, tzinfo=timezone.utc)

try:
    import duckdb
    from backend.agent_run_tracker import _db_path, _ensure_schema
except ImportError as e:
    print(f"[cleanup-pre-845-pm-dupes] ERROR: cannot import dependencies: {e}", file=sys.stderr)
    sys.exit(1)

db = _db_path()
if not db.exists():
    print(f"[cleanup-pre-845-pm-dupes] stats.duckdb not found at {db} — nothing to do")
    sys.exit(0)

conn = duckdb.connect(str(db))
try:
    _ensure_schema(conn)

    # Find candidates: role='project-manager', end_ts IS NULL, start_ts < cutoff
    rows = conn.execute(
        """
        SELECT agent_id, start_ts
        FROM agent_run
        WHERE role = 'project-manager'
          AND end_ts IS NULL
          AND start_ts < ?
        ORDER BY start_ts
        """,
        [CUTOFF],
    ).fetchall()

    print(f"[cleanup-pre-845-pm-dupes] found {len(rows)} orphaned PM row(s) before {CUTOFF.isoformat()}")
    for agent_id, start_ts in rows:
        print(f"  agent_id={agent_id}  start_ts={start_ts}")

    if dry_run:
        print("[cleanup-pre-845-pm-dupes] dry-run mode — no rows updated")
        sys.exit(0)

    if not rows:
        print("[cleanup-pre-845-pm-dupes] nothing to update")
        sys.exit(0)

    now_ts = datetime.now(timezone.utc)

    # Use INSERT ... ON CONFLICT DO UPDATE — same pattern as complete_run()
    # so the update is idempotent and safe to re-run.
    for agent_id, _start_ts in rows:
        conn.execute(
            """
            INSERT INTO agent_run (agent_id, role, start_ts, end_ts, verdict)
            VALUES (?, 'project-manager', ?, ?, 'superseded')
            ON CONFLICT (agent_id) DO UPDATE SET
                end_ts  = excluded.end_ts,
                verdict = excluded.verdict
            """,
            [agent_id, _start_ts, now_ts],
        )

    conn.commit()
    print(f"[cleanup-pre-845-pm-dupes] updated {len(rows)} row(s) — verdict=superseded, end_ts={now_ts.isoformat()}")
finally:
    conn.close()
PYEOF
