#!/usr/bin/env bash
# tests/test_start_dashboard_sh.sh — synthetic test for start-dashboard.sh
#
# Verifies that all four required ports bind successfully when the dashboard
# is launched from a clean state.
#
# Ports under test: 5173 (Vite), 18099 (API), 8765 (RPC), 8420 (SSE)
#
# Exit 0 on success, non-zero on failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQUIRED_PORTS=(5173 18099 8765 8420)

log() { echo "[test-start-dashboard] $*" >&2; }

# ---- Cleanup on exit ---------------------------------------------------------

cleanup() {
  log "Stopping dashboard services..."
  bash "$REPO_ROOT/scripts/stop-dashboard.sh" 2>/dev/null || true
}
trap cleanup EXIT

# ---- Kill any existing dashboard processes ----------------------------------

log "Stopping any pre-existing dashboard processes..."
bash "$REPO_ROOT/scripts/stop-dashboard.sh" 2>/dev/null || true
sleep 1

# ---- Start dashboard --------------------------------------------------------

log "Starting dashboard..."
AF_DASHBOARD_CI=1 bash "$REPO_ROOT/scripts/start-dashboard.sh"
START_EXIT=$?

if [[ $START_EXIT -ne 0 ]]; then
  log "FAIL: start-dashboard.sh exited with code $START_EXIT"
  exit 1
fi

log "start-dashboard.sh exited 0 — checking ports..."

# ---- Assert all 4 ports are bound ------------------------------------------

FAIL=0
for port in "${REQUIRED_PORTS[@]}"; do
  if ss -tln 2>/dev/null | grep -qE ":$port\b"; then
    log "  port $port: BOUND"
  else
    log "FAIL: port $port is NOT bound"
    FAIL=1
  fi
done

if [[ $FAIL -ne 0 ]]; then
  log "FAIL: one or more required ports not bound"
  exit 1
fi

log "PASS: all 4 ports bound (5173, 18099, 8765, 8420)"
exit 0
