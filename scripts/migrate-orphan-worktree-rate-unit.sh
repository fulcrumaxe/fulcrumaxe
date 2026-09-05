#!/usr/bin/env bash
# migrate-orphan-worktree-rate-unit.sh
#
# One-shot data migration: rewrite all existing metric_event rows for
# orphan_worktree_rate where unit='ratio' → unit='count'.
#
# Background: PR #1040 changed reap-worktrees.sh to emit orphan_worktree_rate
# with unit='count' (orphans per hour, a rate). Rows written before that fix
# still carry unit='ratio', which makes MetricSparkline multiply them by 100
# and display ~2,000,000% instead of a small count.
#
# This script is idempotent — re-running it is safe (WHERE unit='ratio' means
# already-migrated rows with unit='count' are untouched).
#
# Usage:
#   bash scripts/migrate-orphan-worktree-rate-unit.sh [--dry-run]
#
# Dry-run prints how many rows would be updated without modifying anything.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# ---------------------------------------------------------------------------
# Discover all stats.duckdb paths to migrate.
# Covers: the primary autonomous-forever state dir plus any other project
# state dirs that exist on this host.
# ---------------------------------------------------------------------------

python3 -c "
import os, sys, pathlib, json

repo_root = '$REPO_ROOT'
dry_run = '$DRY_RUN' == 'true'

sys.path.insert(0, repo_root)

try:
    import duckdb
except ImportError:
    print('ERROR: duckdb not installed — run: pip install duckdb', file=sys.stderr)
    sys.exit(1)

from backend import state_paths as _state_paths

# Build list of candidate DB paths:
# 1. Primary project DB
# 2. All *-state dirs in HOME that have a stats.duckdb
home = pathlib.Path.home()
candidates = {}

# Primary
candidates[str(_state_paths.STATS_DB)] = 'autonomous-forever (primary)'

# Other project state dirs
for state_dir in sorted(home.glob('*-state')):
    db = state_dir / 'stats.duckdb'
    if db.exists() and db.stat().st_size > 0 and str(db) not in candidates:
        project_name = state_dir.name.removeprefix('.').removesuffix('-state')
        candidates[str(db)] = project_name

total_updated = 0
total_checked = 0

for db_path, label in candidates.items():
    p = pathlib.Path(db_path)
    if not p.exists() or p.stat().st_size == 0:
        print(f'  {label}: no stats.duckdb — skipping')
        continue

    try:
        conn = duckdb.connect(db_path, read_only=False)
    except Exception as e:
        print(f'  {label}: cannot open {db_path}: {e} — skipping', file=sys.stderr)
        continue

    try:
        # Check for the affected rows
        count_row = conn.execute(
            \"\"\"SELECT COUNT(*) FROM metric_event
               WHERE metric = 'orphan_worktree_rate' AND unit = 'ratio'\"\"\"
        ).fetchone()
        affected = count_row[0] if count_row else 0
        total_checked += 1

        if affected == 0:
            print(f'  {label}: 0 rows to migrate (already clean or no data)')
            continue

        if dry_run:
            print(f'  {label}: would update {affected} row(s) — DRY RUN')
        else:
            conn.execute(
                \"\"\"UPDATE metric_event
                   SET unit = 'count'
                   WHERE metric = 'orphan_worktree_rate' AND unit = 'ratio'\"\"\"
            )
            # Verify
            remaining = conn.execute(
                \"\"\"SELECT COUNT(*) FROM metric_event
                   WHERE metric = 'orphan_worktree_rate' AND unit = 'ratio'\"\"\"
            ).fetchone()[0]
            if remaining != 0:
                print(f'  {label}: ERROR — {remaining} row(s) still have unit=ratio after UPDATE', file=sys.stderr)
                sys.exit(1)
            print(f'  {label}: updated {affected} row(s) unit ratio→count')
            total_updated += affected

    finally:
        conn.close()

if dry_run:
    print(f'migrate-orphan-worktree-rate-unit: dry-run complete, {total_checked} DB(s) checked')
else:
    print(f'migrate-orphan-worktree-rate-unit: done — {total_updated} row(s) updated across {total_checked} DB(s)')
"
