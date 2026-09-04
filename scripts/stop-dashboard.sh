#!/usr/bin/env bash
# scripts/stop-dashboard.sh — cleanly stop all dashboard services
#
# Reads PIDs from .autonomous-team/dashboard-runtime.json and terminates
# the four services started by start-dashboard.sh, then verifies each of
# their ports actually came free — killing the recorded PID is not the same
# as the port being released (Bug 2: a stale wrapper PID being killed while
# the real listener kept the port held).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# PID_DIR has its own dedicated override (AF_DASHBOARD_PID_DIR), independent
# of AUTONOMOUS_TEAM_STATE_DIR/delegated mode -- delegated mode changes
# state-dir/project-identity resolution some suites test directly (D#1635),
# so PID-file isolation for tests (D#2267) uses its own narrow lever instead
# of overloading that one. Unset in production -- default unchanged.
PID_DIR="${AF_DASHBOARD_PID_DIR:-$REPO_ROOT/.autonomous-team}"
# Prefer the state-dir runtime file when delegated -- matches
# start-dashboard.sh's own priority, which skips the repo-side file
# entirely in that mode -- falling back to the repo-level file otherwise.
if [[ -n "${AUTONOMOUS_TEAM_STATE_DIR:-}" ]]; then
  RUNTIME_FILE="$AUTONOMOUS_TEAM_STATE_DIR/dashboard-runtime.json"
else
  RUNTIME_FILE="$REPO_ROOT/.autonomous-team/dashboard-runtime.json"
fi

log() { echo "[stop-dashboard] $*"; }

# Address-family agnostic port-bound check (same `ss` filter as
# start-dashboard.sh's probe — matches 127.0.0.1, [::1], 0.0.0.0, wildcard).
_port_bound() {
  local port="$1"
  [[ -z "$port" || "$port" == "null" ]] && return 1
  ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN
}

stop_pid() {
  local name="$1" pid="$2"
  if [[ -z "$pid" || "$pid" == "null" ]]; then
    log "No PID for $name — skipping"
    return
  fi
  if kill -0 "$pid" 2>/dev/null; then
    log "Stopping $name (PID $pid)"
    kill "$pid" 2>/dev/null || true
    # Give it a moment, then force-kill if still alive
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
      log "$name (PID $pid) force-killed"
    else
      log "$name (PID $pid) stopped"
    fi
  else
    log "$name (PID $pid) was already stopped"
  fi
}

# Read PIDs (and ports, for the post-stop free-port check below) from runtime
# JSON if available.
if [[ -f "$RUNTIME_FILE" ]]; then
  API_PID=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('pids',{}).get('api',''))" 2>/dev/null || true)
  SERVER_PID=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('pids',{}).get('server',''))" 2>/dev/null || true)
  SSE_PID=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('pids',{}).get('sse',''))" 2>/dev/null || true)
  VITE_PID=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('pids',{}).get('vite',''))" 2>/dev/null || true)
  API_PORT=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('ports',{}).get('api',''))" 2>/dev/null || true)
  RPC_PORT=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('ports',{}).get('rpc',''))" 2>/dev/null || true)
  SSE_PORT=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('ports',{}).get('sse',''))" 2>/dev/null || true)
  VITE_PORT=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('ports',{}).get('vite',''))" 2>/dev/null || true)
else
  # Fallback: read from individual PID files. No port info recorded here, so
  # fall back to the same env-override-then-default resolution start-dashboard.sh
  # uses, to keep the post-stop port check meaningful even without a runtime file.
  API_PID=$(cat "$PID_DIR/dashboard-api.pid" 2>/dev/null || true)
  SERVER_PID=$(cat "$PID_DIR/dashboard-server.pid" 2>/dev/null || true)
  SSE_PID=$(cat "$PID_DIR/dashboard-sse.pid" 2>/dev/null || true)
  VITE_PID=$(cat "$PID_DIR/dashboard-vite.pid" 2>/dev/null || true)
  API_PORT=""
  RPC_PORT=""
  SSE_PORT=""
  VITE_PORT=""
fi
API_PORT="${AF_API_PORT:-${API_PORT:-18099}}"
RPC_PORT="${AF_RPC_PORT:-${RPC_PORT:-8765}}"
SSE_PORT="${AF_SSE_PORT:-${SSE_PORT:-8420}}"
VITE_PORT="${AF_VITE_PORT:-${VITE_PORT:-5173}}"

stop_pid "backend/api.py" "$API_PID"
stop_pid "backend/server.py" "$SERVER_PID"
stop_pid "dashboard/server.py" "$SSE_PID"
stop_pid "Vite dev server" "$VITE_PID"

# Stop live-analyst daemon (non-fatal — may not be running if gate was off)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$SCRIPT_DIR/stop-live-analyst.sh" 2>/dev/null || true

# Clean up PID files
rm -f "$PID_DIR/dashboard-api.pid" \
      "$PID_DIR/dashboard-server.pid" \
      "$PID_DIR/dashboard-sse.pid" \
      "$PID_DIR/dashboard-vite.pid"

# Remove runtime file (stale config)
rm -f "$RUNTIME_FILE"

# ---- Verify ports are actually free -----------------------------------------
# Bug 2's consumer-side fix: killing the PID we recorded is not the same as
# the port being free (that PID was the bash wrapper, not the node process
# holding port 5173). Report failure loudly rather than declaring success
# while a port stays held.
STILL_BOUND=()
for _entry in "backend/api.py:$API_PORT" "backend/server.py:$RPC_PORT" "dashboard/server.py:$SSE_PORT" "Vite dev server:$VITE_PORT"; do
  _svc_name="${_entry%%:*}"
  _svc_port="${_entry##*:}"
  # Give a just-released socket a brief moment to clear.
  for _attempt in 1 2 3; do
    _port_bound "$_svc_port" || break
    sleep 1
  done
  if _port_bound "$_svc_port"; then
    STILL_BOUND+=("$_svc_name:$_svc_port")
  fi
done
unset _entry _svc_name _svc_port _attempt

if [[ ${#STILL_BOUND[@]} -gt 0 ]]; then
  log "ERROR: the following ports are still bound after stop: ${STILL_BOUND[*]}"
  log "       stop-dashboard.sh did not fully free these ports."
  exit 1
fi

log "All dashboard services stopped."
