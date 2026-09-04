#!/usr/bin/env bash
# start-live-analyst.sh — Start the live-analyst background daemon.
#
# Usage:
#   bash scripts/start-live-analyst.sh [--poll-interval N] [--dry-run]
#
# Requires: gates.live_run_analyst = true
# PID file: .autonomous-team/live-analyst.pid

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$REPO_ROOT/.autonomous-team/live-analyst.pid"
LOG_FILE="$REPO_ROOT/.autonomous-team/live-analyst.log"

# --- Gate check ---------------------------------------------------------------
GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.live_run_analyst 2>/dev/null | tr -d '"' || echo "false")
if [ "$GATE" != "true" ]; then
  echo "ERROR: gates.live_run_analyst is not enabled."
  echo "  Enable with: python3 backend/control_plane.py set gates.live_run_analyst true"
  exit 1
fi

# --- Already running? ---------------------------------------------------------
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "live-analyst is already running (pid $OLD_PID)"
    exit 0
  fi
  echo "Removing stale PID file (pid $OLD_PID is dead)"
  rm -f "$PID_FILE"
fi

# --- Forward extra flags to the daemon ----------------------------------------
DAEMON_ARGS=()
for arg in "$@"; do
  DAEMON_ARGS+=("$arg")
done

# --- Start daemon in background -----------------------------------------------
mkdir -p "$(dirname "$LOG_FILE")"
nohup python3 "$REPO_ROOT/backend/live_analyst_daemon.py" \
  "${DAEMON_ARGS[@]}" \
  >> "$LOG_FILE" 2>&1 &

DAEMON_PID=$!
echo "$DAEMON_PID" > "$PID_FILE"

# Wait briefly to confirm it didn't exit immediately
sleep 1
if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
  echo "ERROR: live-analyst daemon exited immediately — check $LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi

echo "live-analyst daemon started (pid $DAEMON_PID)"
echo "  log: $LOG_FILE"
echo "  pid: $PID_FILE"
