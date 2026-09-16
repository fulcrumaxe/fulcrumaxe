#!/usr/bin/env bash
# backfill-codex-runs.sh — reconstruct agent_run rows for Muse completions.
#
# Joins a parent session log's subagent.control.* records to each completed
# child's subagent/<child-session-id>/session.jsonl: one agent_run row per
# completion with REAL measured tokens and routed_via=muse. Idempotent on
# agent id (muse-<child-session>); session logs are READ-ONLY evidence.
#
# Usage:
#   bash scripts/backfill-codex-runs.sh --parent-log /path/to/session.jsonl
#     [--discussion N] [--db-path /path/to/stats.duckdb]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PARENT_LOG=""
DB_ARG=""
DISCUSSION_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --parent-log)
            PARENT_LOG="$2"
            shift 2
            ;;
        --db-path)
            DB_ARG="--db-path $2"
            shift 2
            ;;
        --discussion)
            DISCUSSION_ARG="--discussion $2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$PARENT_LOG" ]]; then
    echo "Error: --parent-log is required" >&2
    exit 1
fi

cd "$REPO_ROOT"

# shellcheck disable=SC2086
python3 -m backend.codex_run_backfill --parent-log "$PARENT_LOG" $DB_ARG $DISCUSSION_ARG
