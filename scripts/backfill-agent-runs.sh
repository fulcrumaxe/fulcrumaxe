#!/usr/bin/env bash
# backfill-agent-runs.sh — reconstruct agent_run rows from existing audit.jsonl entries.
#
# Safe to run multiple times — idempotent on agent_id (INSERT OR IGNORE for
# new rows, UPDATE for missing end_ts when both endpoints are known).
#
# Usage:
#   bash scripts/backfill-agent-runs.sh
#   bash scripts/backfill-agent-runs.sh --audit-path /path/to/audit.jsonl
#   bash scripts/backfill-agent-runs.sh --db-path /path/to/stats.duckdb
#
# Exit codes:
#   0 — completed without error (rows processed count printed to stdout)
#   1 — backfill raised an unexpected exception

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AUDIT_ARG=""
DB_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --audit-path)
            AUDIT_ARG="--audit-path $2"
            shift 2
            ;;
        --db-path)
            DB_ARG="--db-path $2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

cd "$REPO_ROOT"

# Run the backfill — non-fatal failures are already handled inside the module,
# but we still surface any unexpected Python exceptions as exit code 1.
python3 -m backend.agent_run_tracker backfill $AUDIT_ARG $DB_ARG
