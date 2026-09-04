#!/usr/bin/env bash
# heartbeat.sh — demo scheduled job.
# Writes a timestamp to .autonomous-team/scheduler-heartbeat.txt to prove
# the dispatcher pipeline is working end-to-end.
# token_ceiling: 0 — does not spawn any agent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# Honors AUTONOMOUS_TEAM_STATE_DIR when set (same delegated-mode convention as
# dispatcher.sh's RUN_LOG/LOG_BASE and scripts/start-dashboard.sh) so a test
# invoking this job directly doesn't touch the checked-out .autonomous-team/
# tree (D#2267). Unset in production -- default path unchanged.
HEARTBEAT_FILE="${AUTONOMOUS_TEAM_STATE_DIR:-$REPO_ROOT/.autonomous-team}/scheduler-heartbeat.txt"

mkdir -p "$(dirname "$HEARTBEAT_FILE")"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$TS" > "$HEARTBEAT_FILE"
echo "[heartbeat] wrote timestamp $TS to $HEARTBEAT_FILE"
