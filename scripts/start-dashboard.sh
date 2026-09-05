#!/usr/bin/env bash
# scripts/start-dashboard.sh — single-command dashboard launcher
#
# Boots all four required services:
#   1. backend/api.py      → REST API
#   2. backend/server.py   → JSON-RPC / HTTP adapter
#   3. dashboard/server.py → SSE bridge
#   4. Vite dev server     → React frontend
#
# Port resolution order (highest to lowest priority):
#   1. AF_API_PORT / AF_RPC_PORT / AF_SSE_PORT / AF_VITE_PORT env vars
#   2. "ports" block in .autonomous-team/project.json
#   3. Derived from dashboard_port in project.json (vite=base, api=+100, rpc=+200, sse=+300)
#   4. Hardcoded autonomous-forever defaults (18099/8765/8420/5173)
#
# Pre-start port occupancy check:
#   Before starting any service, checks whether its target port is already bound. If so,
#   refuses to proceed and names the offending PID and command line. Never kills a process
#   it did not spawn — run stop-dashboard.sh first if a previous run is still up.
#
# Usage:
#   bash scripts/start-dashboard.sh
#   AF_DASHBOARD_CI=1 bash scripts/start-dashboard.sh   # CI mode (no browser open)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# PID_DIR has its own dedicated override (AF_DASHBOARD_PID_DIR), independent
# of AUTONOMOUS_TEAM_STATE_DIR/delegated mode used below for
# dashboard-runtime.json -- delegated mode changes state-dir/project-identity
# resolution some suites test directly (D#1635), so PID-file isolation for
# tests (D#2267) uses its own narrow lever instead of overloading that one.
# Unset in production -- default unchanged.
PID_DIR="${AF_DASHBOARD_PID_DIR:-$REPO_ROOT/.autonomous-team}"
LOG_DIR="$REPO_ROOT/.autonomous-team/dashboard-logs"

BIND_TIMEOUT=30

mkdir -p "$LOG_DIR"

# ---- Helpers ----------------------------------------------------------------

log() { echo "[start-dashboard] $*" >&2; }

# ---- Resolve project identity -----------------------------------------------

PROJECT_JSON="$REPO_ROOT/.autonomous-team/project.json"

# Read project name (used for state-dir derivation and the runtime JSON)
THIS_PROJECT=""
if [[ -f "$PROJECT_JSON" ]]; then
  THIS_PROJECT="$(python3 -c "
import json, sys
try:
    d = json.load(open('$PROJECT_JSON'))
    print(d.get('project_name', ''))
except Exception:
    sys.exit(0)
" 2>/dev/null || true)"
fi

# When invoked as a delegated script for another project (AUTONOMOUS_TEAM_STATE_DIR
# is set by the wrapper), derive the project name from the state dir path if we
# couldn't read it from project.json.  e.g. <home>/.projectb-state → "projectb"
if [[ -z "$THIS_PROJECT" && -n "${AUTONOMOUS_TEAM_STATE_DIR:-}" ]]; then
  _dir_name="$(basename "$AUTONOMOUS_TEAM_STATE_DIR")"
  if [[ "$_dir_name" == .*-state ]]; then
    THIS_PROJECT="${_dir_name:1:-6}"  # strip leading "." and trailing "-state"
  fi
fi
THIS_PROJECT="${THIS_PROJECT:-autonomous-forever}"

# ---- Determine state dir for runtime JSON -----------------------------------
# Write dashboard-runtime.json to the state dir (outside repo) so fleet
# discovery can scan ~/.*-state/ for all running dashboards.
#
# Precedence (highest to lowest):
#   1. AUTONOMOUS_TEAM_STATE_DIR env var (set by per-project wrapper scripts)
#   2. state_dir field in project.json
#   3. Derived from project name: ~/.{project}-state

_STATE_DIR_SOURCE="default"
STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-}"
if [[ -n "$STATE_DIR" ]]; then
  _STATE_DIR_SOURCE="env"
elif [[ -f "$PROJECT_JSON" ]]; then
  STATE_DIR="$(python3 -c "
import json, sys
try:
    d = json.load(open('$PROJECT_JSON'))
    v = d.get('state_dir', '')
    if v:
        print(v)
except Exception:
    pass
" 2>/dev/null || true)"
  if [[ -n "$STATE_DIR" ]]; then
    _STATE_DIR_SOURCE="project.json"
  fi
fi

# Fallback: derive state dir from project name
if [[ -z "$STATE_DIR" ]]; then
  STATE_DIR="$HOME/.${THIS_PROJECT}-state"
fi

log "state-dir source: $_STATE_DIR_SOURCE"

# Repo-side runtime file (for backward compatibility — /api/config reads this)
RUNTIME_FILE="$REPO_ROOT/.autonomous-team/dashboard-runtime.json"
# State-dir runtime file (for fleet discovery — scanned by backend/fleet/runtime.py).
# AF_DASHBOARD_STATE_RUNTIME_FILE is a dedicated override for WHERE this copy
# lands, independent of STATE_DIR's own value (D#2267). STATE_DIR itself must
# stay untouched here -- it feeds the "State dir:" log line
# test_dashboard_lifecycle.sh's DL-2 asserts on (D#1635) and the "state_dir"
# field embedded in the runtime JSON's own content -- so a test that must not
# write the operator's real ~/.autonomous-forever-state/dashboard-runtime.json
# (the default STATE_DIR fallback when AUTONOMOUS_TEAM_STATE_DIR is
# deliberately left unset, as DL-2 requires) redirects only the write target,
# not STATE_DIR. Unset in production -- default path unchanged.
STATE_RUNTIME_FILE="${AF_DASHBOARD_STATE_RUNTIME_FILE:-$STATE_DIR/dashboard-runtime.json}"

# ---- Port resolution --------------------------------------------------------
# Autonomous-forever keeps its existing ports explicitly in project.json.
# New projects get ports derived from dashboard_port.

_read_ports_from_project() {
  if [[ ! -f "$PROJECT_JSON" ]]; then return; fi
  python3 - <<PYEOF
import json, sys
try:
    d = json.load(open('$PROJECT_JSON'))
    ports = d.get('ports', {})
    dp = d.get('dashboard_port')

    # If explicit ports block present, use it
    if ports.get('vite') and ports.get('api') and ports.get('rpc') and ports.get('sse'):
        print(f"vite={ports['vite']}")
        print(f"api={ports['api']}")
        print(f"rpc={ports['rpc']}")
        print(f"sse={ports['sse']}")
    elif isinstance(dp, int):
        # Derive from dashboard_port
        print(f"vite={dp}")
        print(f"api={dp + 100}")
        print(f"rpc={dp + 200}")
        print(f"sse={dp + 300}")
except Exception:
    pass
PYEOF
}

# Default ports (autonomous-forever hardcoded values — preserved for backward compatibility)
API_PORT=18099
RPC_PORT=8765
SSE_PORT=8420
VITE_PORT=5173

# Apply project.json ports (overrides defaults)
while IFS='=' read -r key val; do
  [[ -z "$key" ]] && continue
  case "$key" in
    api)  API_PORT="$val" ;;
    rpc)  RPC_PORT="$val" ;;
    sse)  SSE_PORT="$val" ;;
    vite) VITE_PORT="$val" ;;
  esac
done < <(_read_ports_from_project)

# Apply explicit env overrides (highest priority)
API_PORT="${AF_API_PORT:-$API_PORT}"
RPC_PORT="${AF_RPC_PORT:-$RPC_PORT}"
SSE_PORT="${AF_SSE_PORT:-$SSE_PORT}"
VITE_PORT="${AF_VITE_PORT:-$VITE_PORT}"

log "Project: $(basename "$REPO_ROOT")"
log "Ports: vite=$VITE_PORT api=$API_PORT rpc=$RPC_PORT sse=$SSE_PORT"
log "State dir: $STATE_DIR"

# ---- Shared port-readiness / ownership probe --------------------------------
# One probe function used at all four call sites below rather than four
# near-duplicate copies of the both-address-families-plus-ownership logic.
#
# ss's `sport = :$port` filter matches a listening socket on that port
# regardless of which address it's bound to (127.0.0.1, [::1], 0.0.0.0,
# wildcard) — this is what makes the probe address-family agnostic (Bug 1:
# Vite binds [::1] only; the old probe curled 127.0.0.1 literally and timed
# out on a server that was serving fine).

# Print the PID(s) currently holding the listening socket for port $1.
# `|| true`: under `set -e -o pipefail`, grep finding no match (the common,
# non-error case of "nothing is listening on this port") makes the pipeline
# exit non-zero, which would otherwise silently kill the whole script at
# every call site that captures this via `x="$(_port_owner_pids ...)"`.
_port_owner_pids() {
  local port="$1"
  ss -ltnp "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u || true
}

# True if PID $1 is PID $2, or a descendant of it (walks the ppid chain).
# Handles a wrapper-forks-child process tree without assuming a fixed depth.
_pid_is_or_descends_from() {
  local pid="$1" target="$2" hops=0
  while [[ -n "$pid" && "$pid" != "0" && "$pid" != "1" && $hops -lt 50 ]]; do
    [[ "$pid" == "$target" ]] && return 0
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    hops=$((hops + 1))
  done
  return 1
}

# ---- Pre-start port occupancy check (Bug 3) ---------------------------------
# Refuse to proceed if any target port is already held by a process this
# script did not spawn. Name it and stop — do not kill it. This also covers
# "start-dashboard.sh run twice without an intervening stop": the second run
# finds its own still-alive first-run services occupying every port and
# refuses the same way, rather than silently restarting on top of them.
for _pair in "backend/api.py:$API_PORT" "backend/server.py:$RPC_PORT" "dashboard/server.py:$SSE_PORT" "Vite dev server:$VITE_PORT"; do
  _svc_name="${_pair%%:*}"
  _svc_port="${_pair##*:}"
  _pids="$(_port_owner_pids "$_svc_port")"
  if [[ -n "$_pids" ]]; then
    _first_pid="$(echo "$_pids" | head -1)"
    _cmd="$(ps -o cmd= -p "$_first_pid" 2>/dev/null || echo "<pid $_first_pid already gone>")"
    log "ERROR: port $_svc_port ($_svc_name) is already bound by PID $_first_pid: $_cmd"
    log "       Not starting — this script never kills a process it did not spawn."
    log "       Run 'bash scripts/stop-dashboard.sh' if this is a previous dashboard run, or stop the process directly."
    exit 1
  fi
done
unset _pair _svc_name _svc_port _pids _first_pid _cmd

# ---- Start backend/api.py (REST API) ----------------------------------------

log "Starting backend/api.py on port $API_PORT..."
nohup python3 "$REPO_ROOT/backend/api.py" --host 127.0.0.1 --port "$API_PORT" \
  > "$LOG_DIR/api.log" 2>&1 &
API_PID=$!
echo $API_PID > "$PID_DIR/dashboard-api.pid"

# Wait for $2 to be listening AND owned by PID $3 (or a descendant of it).
# Address-family agnostic (Bug 1) and ownership-checked (Bug 3) — a stale,
# unrelated process answering the same port must not count as ready.
wait_for_port_owned() {
  local name="$1" port="$2" spawned_pid="$3" deadline=$((SECONDS + BIND_TIMEOUT))
  while [[ $SECONDS -lt $deadline ]]; do
    local pids p
    pids="$(_port_owner_pids "$port")"
    if [[ -n "$pids" ]]; then
      for p in $pids; do
        if _pid_is_or_descends_from "$p" "$spawned_pid"; then
          return 0
        fi
      done
      log "ERROR: port $port is answering, but the owning PID(s) [$pids] are not $spawned_pid (the process this script spawned) or a descendant of it"
      return 1
    fi
    sleep 1
  done
  log "ERROR: $name did not respond on port $port within ${BIND_TIMEOUT}s"
  return 1
}

# Wait for REST API to be up
wait_for_port_owned "backend/api.py" $API_PORT $API_PID || {
  log "backend/api.py failed to start. Check $LOG_DIR/api.log"
  exit 1
}
log "backend/api.py ready (PID $API_PID)"

# ---- Start backend/server.py (JSON-RPC HTTP adapter) -----------------------

log "Starting backend/server.py --http $RPC_PORT..."
nohup env PYTHONPATH="$REPO_ROOT" python3 -m backend.server --http "$RPC_PORT" \
  > "$LOG_DIR/server.log" 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > "$PID_DIR/dashboard-server.pid"

# Wait for RPC server to bind its port. wait_for_port_owned checks via `ss`
# (port-bind + ownership), not curl — the RPC server requires auth, and a
# curl probe would see a 401, which curl -sf treats as failure.
wait_for_port_owned "backend/server.py" $RPC_PORT $SERVER_PID || {
  log "backend/server.py failed to start. Check $LOG_DIR/server.log"
  exit 1
}
log "backend/server.py ready (PID $SERVER_PID)"

# ---- Start dashboard/server.py (SSE bridge) ---------------------------------

log "Starting dashboard/server.py on port $SSE_PORT..."
nohup env AF_SSE_PORT="$SSE_PORT" python3 "$REPO_ROOT/dashboard/server.py" --port "$SSE_PORT" \
  > "$LOG_DIR/sse.log" 2>&1 &
SSE_PID=$!
echo $SSE_PID > "$PID_DIR/dashboard-sse.pid"

wait_for_port_owned "dashboard/server.py" $SSE_PORT $SSE_PID || {
  log "dashboard/server.py failed to start. Check $LOG_DIR/sse.log"
  exit 1
}
log "dashboard/server.py ready (PID $SSE_PID)"

# ---- Write runtime JSON (source of truth for /api/config) ------------------

# Read the RPC token that server.py wrote to disk
TOKEN_PATH="$REPO_ROOT/.autonomous-team/dashboard-token"
RPC_TOKEN=""
if [[ -f "$TOKEN_PATH" ]]; then
  RPC_TOKEN=$(cat "$TOKEN_PATH")
fi

DASHBOARD_VERSION="0.1.0"

# Get project repo from project.json
PROJECT_REPO=""
if [[ -f "$PROJECT_JSON" ]]; then
  PROJECT_REPO="$(python3 -c "
import json, sys
try:
    d = json.load(open('$PROJECT_JSON'))
    print(d.get('repo', ''))
except Exception:
    pass
" 2>/dev/null || true)"
fi

_write_runtime() {
  local target="$1"
  python3 - <<PYEOF
import json, pathlib, datetime

p = pathlib.Path("$target")
p.parent.mkdir(parents=True, exist_ok=True)
d = {
    "project_name": "$THIS_PROJECT",
    "project_repo": "$PROJECT_REPO",
    "state_dir": "$STATE_DIR",
    "rpcBaseUrl": "http://localhost:$RPC_PORT",
    "rpcToken": "$RPC_TOKEN",
    "dashboardVersion": "$DASHBOARD_VERSION",
    "ports": {
        "vite": $VITE_PORT,
        "api": $API_PORT,
        "rpc": $RPC_PORT,
        "sse": $SSE_PORT,
    },
    "pids": {
        "api": $API_PID,
        "server": $SERVER_PID,
        "sse": $SSE_PID,
    },
    "started_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
p.write_text(json.dumps(d, indent=2) + "\n")
PYEOF
}

# When running as a delegated script for another project (AUTONOMOUS_TEAM_STATE_DIR
# is set), do NOT overwrite the AF repo-side runtime file — that would clobber AF's
# own dashboard data.  Write only to the project's state dir.
if [[ -n "${AUTONOMOUS_TEAM_STATE_DIR:-}" ]]; then
  _write_runtime "$STATE_RUNTIME_FILE"
  log "Runtime config written to $STATE_RUNTIME_FILE (delegated mode — skipping AF repo-side file)"
else
  _write_runtime "$RUNTIME_FILE"
  log "Runtime config written to $RUNTIME_FILE"

  # Also write to state dir so fleet discovery finds it
  if [[ "$STATE_DIR" != "$REPO_ROOT/.autonomous-team" ]]; then
    _write_runtime "$STATE_RUNTIME_FILE"
    log "Runtime config written to $STATE_RUNTIME_FILE"
  fi
fi

# ---- Start Vite dev server (React frontend) ---------------------------------

log "Starting Vite dev server on port $VITE_PORT..."
# Bug 2: `npm run dev` puts an npm process between the backgrounded PID and
# the actual node/vite listener, so the recorded PID was never the process
# holding the port. `npm run dev` here is just `vite` (see dashboard/package.json)
# with no other flags, so exec straight into the vite binary — the PID this
# script captures below IS the node process that binds the port, and
# stop-dashboard.sh's `kill` lands on the right target.
VITE_BIN="$REPO_ROOT/dashboard/node_modules/.bin/vite"
nohup bash -c "cd '$REPO_ROOT/dashboard' && exec '$VITE_BIN' --port $VITE_PORT" \
  > "$LOG_DIR/vite.log" 2>&1 &
VITE_PID=$!
echo $VITE_PID > "$PID_DIR/dashboard-vite.pid"

# Update runtime files with Vite PID
python3 - <<PYEOF
import json, os, pathlib

def update_vite_pid(path_str):
    p = pathlib.Path(path_str)
    if not p.exists():
        return
    d = json.loads(p.read_text())
    d["pids"]["vite"] = $VITE_PID
    p.write_text(json.dumps(d, indent=2) + "\n")

delegated = bool(os.environ.get("AUTONOMOUS_TEAM_STATE_DIR"))
if delegated:
    update_vite_pid("$STATE_RUNTIME_FILE")
else:
    update_vite_pid("$RUNTIME_FILE")
    if "$STATE_DIR" != "$REPO_ROOT/.autonomous-team":
        update_vite_pid("$STATE_RUNTIME_FILE")
PYEOF

wait_for_port_owned "Vite dev server" $VITE_PORT $VITE_PID || {
  log "Vite dev server failed to start. Check $LOG_DIR/vite.log"
  exit 1
}
log "Vite dev server ready (PID $VITE_PID)"

# ---- Open browser (unless CI mode) ----------------------------------------

if [[ "${AF_DASHBOARD_CI:-}" != "1" ]]; then
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:$VITE_PORT" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "http://localhost:$VITE_PORT" >/dev/null 2>&1 &
  fi
fi

# ---- Start live-analyst daemon (optional — gated) --------------------------
#
# Non-fatal: the script checks gates.live_run_analyst internally and exits 1
# when the gate is off (default), printing "ERROR: gates.live_run_analyst is
# not enabled." on stdout. Suppress both streams — not just stderr — so that
# disabled-gate message doesn't leak an "ERROR" line into every normal run's
# stdout (the exact false-failure shape this Discussion is about).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log "Starting live-analyst daemon (if gate enabled)..."
bash "$SCRIPT_DIR/start-live-analyst.sh" >/dev/null 2>&1 || true

# ---- Done ------------------------------------------------------------------

echo ""
echo "Dashboard ready: http://localhost:$VITE_PORT"
echo "Stop with: bash scripts/stop-dashboard.sh"
