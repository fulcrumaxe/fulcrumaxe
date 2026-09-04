#!/usr/bin/env bash
# stop-live-analyst.sh — Stop the live-analyst background daemon.
#
# Usage:
#   bash scripts/stop-live-analyst.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$REPO_ROOT/.autonomous-team/live-analyst.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "live-analyst is not running (no PID file found)"
  exit 0
fi

PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
if [ -z "$PID" ]; then
  echo "PID file is empty — removing"
  rm -f "$PID_FILE"
  exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
  echo "live-analyst (pid $PID) is not running — removing stale PID file"
  rm -f "$PID_FILE"
  exit 0
fi

echo "Stopping live-analyst daemon (pid $PID)..."
kill -TERM "$PID" 2>/dev/null || true

# Wait up to 10 seconds for graceful shutdown
for i in $(seq 1 10); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "live-analyst stopped"
    rm -f "$PID_FILE"
    exit 0
  fi
  sleep 1
done

# Force kill if still running
echo "Daemon did not stop gracefully — sending SIGKILL"
kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "live-analyst killed"
