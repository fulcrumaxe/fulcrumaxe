#!/usr/bin/env bash
# codex-subagent-hook.sh — mandatory stats write at the end of every Muse
# subagent completion. Run after EACH finish (idempotent: re-recording an
# already-recorded completion is a no-op). Records one agent_run row with
# REAL measured tokens from the child's own session.jsonl, routed_via=muse.
# NULL tokens ONLY when the subagent log is genuinely missing — never
# self-reported numbers, never estimates. Non-fatal: always exits 0, never
# blocks completion.
#
# Usage:
#   bash scripts/codex-subagent-hook.sh \
#     --session-dir <parent-session-dir> --subagent-id <subagent-id> \
#     [--discussion <N>] [--db-path /path/to/stats.duckdb]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SESSION_DIR=""
SUBAGENT_ID=""
DB_ARG=""
DISCUSSION_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --session-dir)
            SESSION_DIR="$2"
            shift 2
            ;;
        --subagent-id)
            SUBAGENT_ID="$2"
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
            echo "[codex-subagent-hook] Unknown argument: $1" >&2
            exit 0
            ;;
    esac
done

if [[ -z "$SESSION_DIR" || -z "$SUBAGENT_ID" ]]; then
    echo "[codex-subagent-hook] Error: --session-dir and --subagent-id are required" >&2
    exit 0
fi

PARENT_LOG="$SESSION_DIR/session.jsonl"
if [[ ! -f "$PARENT_LOG" ]]; then
    echo "[codex-subagent-hook] WARN: parent log not found: $PARENT_LOG (non-fatal)" >&2
    exit 0
fi

cd "$REPO_ROOT"

# shellcheck disable=SC2086
python3 -m backend.codex_run_backfill \
    --parent-log "$PARENT_LOG" --subagent-id "$SUBAGENT_ID" \
    $DB_ARG $DISCUSSION_ARG 2>&1 \
    || echo "[codex-subagent-hook] WARN: stats write failed for $SUBAGENT_ID (non-fatal)" >&2

exit 0
