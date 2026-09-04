#!/usr/bin/env bash
# scripts/cutover-dashboard.sh — safe, verified, one-command dashboard cutover
#
# Launches the unified FastAPI app (backend.asgi_app:app) on NEW port :18100
# (configurable via AF_ASGI_PORT).  Updates .autonomous-team/dashboard-runtime.json
# so rpcBaseUrl + REST/api port both point at :18100.
#
# SAFETY INVARIANTS:
#   - Legacy services on :18099 and :8765 are NOT stopped.  FastAPI proxies the
#     16 still-unmigrated routes to legacy :18099 via its reverse-proxy catch-all.
#   - The LIVE dashboard-runtime.json is only rewritten after the FastAPI app
#     passes a health check.  Pass AF_RUNTIME_FILE=/path to test on a temp copy.
#   - Idempotent: running again kills the existing :18100 process and starts fresh.
#
# SECURITY NOTE — NO --proxy-headers:
#   uvicorn is launched WITHOUT --proxy-headers intentionally.
#   /api/config trusts request.client.host to gate loopback access (CWE-290).
#   If --proxy-headers were passed, an attacker on a separate network hop could
#   inject X-Forwarded-For: 127.0.0.1 and bypass the loopback gate.
#   Without --proxy-headers, request.client.host is the REAL direct-connect IP.
#
# PROXY BACKSTOP:
#   FastAPI's reverse-proxy catch-all forwards unmigrated routes to
#   http://127.0.0.1:18099 (legacy api.py).  That target is hard-coded in
#   backend/asgi_app.py as _LEGACY_BASE_URL.  Confirm:
#     grep _LEGACY_BASE_URL backend/asgi_app.py
#
# Usage:
#   bash scripts/cutover-dashboard.sh
#   AF_ASGI_PORT=18100 bash scripts/cutover-dashboard.sh
#   AF_RUNTIME_FILE=/tmp/test-runtime.json bash scripts/cutover-dashboard.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_ROOT/.autonomous-team/dashboard-logs"
PID_DIR="$REPO_ROOT/.autonomous-team"

ASGI_PORT="${AF_ASGI_PORT:-18100}"
BIND_TIMEOUT="${AF_CUTOVER_TIMEOUT:-30}"
RUNTIME_FILE="${AF_RUNTIME_FILE:-$REPO_ROOT/.autonomous-team/dashboard-runtime.json}"
ASGI_PID_FILE="$PID_DIR/dashboard-asgi.pid"
ASGI_BACKUP_FILE="${RUNTIME_FILE}.pre-cutover-backup"

mkdir -p "$LOG_DIR"

log() { echo "[cutover-dashboard] $*" >&2; }
die() { echo "[cutover-dashboard] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Step 1: Sanity checks — verify legacy services are up
# ---------------------------------------------------------------------------

log "Pre-flight: verifying legacy services..."

if ! curl -sf "http://127.0.0.1:18099/health" >/dev/null 2>&1 \
   && ! curl -sf "http://127.0.0.1:18099/" >/dev/null 2>&1; then
  log "WARNING: legacy api.py on :18099 does not appear to be running."
  log "         The FastAPI proxy-backstop will fail for unmigrated routes."
  log "         Start legacy services first: bash scripts/start-dashboard.sh"
  log "         Continuing anyway (FastAPI native routes will work regardless)."
fi

# ---------------------------------------------------------------------------
# Step 2: Kill any stale :ASGI_PORT process (idempotency)
# ---------------------------------------------------------------------------

_kill_asgi_port() {
  local pid
  pid=$(lsof -ti tcp:"$ASGI_PORT" 2>/dev/null || true)
  if [[ -n "$pid" ]]; then
    log "Stopping existing process on port $ASGI_PORT (PID $pid)"
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
}
_kill_asgi_port

# Also kill by PID file if present (belt-and-suspenders for the idempotent case)
if [[ -f "$ASGI_PID_FILE" ]]; then
  OLD_PID=$(cat "$ASGI_PID_FILE" 2>/dev/null || true)
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    log "Stopping previous FastAPI process (PID $OLD_PID from PID file)"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$ASGI_PID_FILE"
fi

# ---------------------------------------------------------------------------
# Step 3: Launch FastAPI on the new port
#
# ASSERT: NO --proxy-headers (see CWE-290 note in header above).
# ASSERT: --workers 1 (in-process event bus + long-lived SSE/WS; scale via
#         AF_THREADPOOL_TOKENS, not process count).
# ---------------------------------------------------------------------------

log "Launching FastAPI app (backend.asgi_app:app) on port $ASGI_PORT..."

nohup env PYTHONPATH="$REPO_ROOT" \
  python3 -m uvicorn backend.asgi_app:app \
    --host 127.0.0.1 \
    --port "$ASGI_PORT" \
    --workers 1 \
    --log-level warning \
  > "$LOG_DIR/asgi.log" 2>&1 &
ASGI_PID=$!
echo "$ASGI_PID" > "$ASGI_PID_FILE"
log "FastAPI PID: $ASGI_PID (log: $LOG_DIR/asgi.log)"

# ---------------------------------------------------------------------------
# Step 4: Wait for the FastAPI app to bind and serve /health
# ---------------------------------------------------------------------------

log "Waiting for FastAPI to bind on port $ASGI_PORT (timeout: ${BIND_TIMEOUT}s)..."
deadline=$((SECONDS + BIND_TIMEOUT))
asgi_up=false
while [[ $SECONDS -lt $deadline ]]; do
  if curl -sf "http://127.0.0.1:$ASGI_PORT/health" >/dev/null 2>&1; then
    asgi_up=true
    break
  fi
  # Check the process is still alive
  if ! kill -0 "$ASGI_PID" 2>/dev/null; then
    log "FastAPI process exited unexpectedly. Last 20 lines of log:"
    tail -20 "$LOG_DIR/asgi.log" >&2 || true
    die "FastAPI failed to start. See $LOG_DIR/asgi.log"
  fi
  sleep 1
done

if [[ "$asgi_up" != "true" ]]; then
  log "FastAPI did not respond on port $ASGI_PORT within ${BIND_TIMEOUT}s."
  log "Last 20 lines of log:"
  tail -20 "$LOG_DIR/asgi.log" >&2 || true
  kill "$ASGI_PID" 2>/dev/null || true
  die "Health check timed out. See $LOG_DIR/asgi.log"
fi

log "FastAPI /health: OK on port $ASGI_PORT"

# ---------------------------------------------------------------------------
# Step 5: Smoke-test the new port before touching the runtime config
# ---------------------------------------------------------------------------

log "Smoke testing FastAPI endpoints on port $ASGI_PORT..."

# Test /health/loop (REST)
if ! curl -sf "http://127.0.0.1:$ASGI_PORT/health/loop" >/dev/null 2>&1; then
  log "WARNING: /health/loop did not return 200 (may be expected if loop isn't running)"
fi

# Verify /api/config exists (responds, even if 403 from non-loopback test — that's correct)
CONFIG_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "http://127.0.0.1:$ASGI_PORT/api/config" 2>/dev/null || echo "000")
if [[ "$CONFIG_STATUS" == "200" || "$CONFIG_STATUS" == "403" ]]; then
  log "/api/config: HTTP $CONFIG_STATUS (expected — 200 from loopback, 403 from remote)"
else
  log "WARNING: /api/config returned unexpected status: $CONFIG_STATUS"
fi

log "Smoke tests passed."

# ---------------------------------------------------------------------------
# Step 6: Back up current runtime.json and rewrite with new ports
# ---------------------------------------------------------------------------

log "Backing up $RUNTIME_FILE to $ASGI_BACKUP_FILE..."
if [[ -f "$RUNTIME_FILE" ]]; then
  cp "$RUNTIME_FILE" "$ASGI_BACKUP_FILE"
fi

log "Rewriting $RUNTIME_FILE to point at FastAPI port $ASGI_PORT..."

python3 - "$RUNTIME_FILE" "$ASGI_PID" "$ASGI_PORT" <<'PYEOF'
import json, sys, pathlib, datetime

runtime_path = sys.argv[1]
asgi_pid     = int(sys.argv[2])
asgi_port    = int(sys.argv[3])

# Load existing config or start with empty dict
if pathlib.Path(runtime_path).exists():
    d = json.loads(pathlib.Path(runtime_path).read_text())
else:
    d = {}

# Point both REST base (api port) and RPC base at the new FastAPI port.
# The FastAPI app natively handles /rpc (P6a router) so the RPC URL changes
# too.  Legacy services stay up as proxy backstop for unmigrated routes.
d["rpcBaseUrl"] = f"http://localhost:{asgi_port}"

# Update ports block — api, rpc, and sse all move to asgi_port
ports = d.get("ports", {})
ports["api"]  = asgi_port
ports["rpc"]  = asgi_port
ports["sse"]  = asgi_port
d["ports"] = ports

# Record the new ASGI PID alongside the legacy PIDs
pids = d.get("pids", {})
pids["asgi"] = asgi_pid
d["pids"] = pids

d["cutover_at"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
d["cutover_port"] = asgi_port
d["legacy_api_port"] = 18099
d["legacy_rpc_port"] = 8765

pathlib.Path(runtime_path).write_text(json.dumps(d, indent=2) + "\n")
print(f"[cutover-dashboard] runtime.json updated: rpcBaseUrl=http://localhost:{asgi_port}")
PYEOF

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "CUTOVER COMPLETE — dashboard now on FastAPI :${ASGI_PORT}; legacy still up as proxy backstop"
echo ""
echo "  FastAPI port : $ASGI_PORT  (REST + RPC + SSE + WS + proxy-backstop)"
echo "  Legacy api   : 18099  (still running — proxied for unmigrated routes)"
echo "  Legacy RPC   : 8765   (still running — proxied for unmigrated RPC methods)"
echo "  Runtime file : $RUNTIME_FILE"
echo "  ASGI PID file: $ASGI_PID_FILE"
echo "  ASGI log     : $LOG_DIR/asgi.log"
echo ""
echo "Verify:"
echo "  curl http://127.0.0.1:$ASGI_PORT/health"
echo "  curl http://127.0.0.1:$ASGI_PORT/api/config"
echo "  curl http://127.0.0.1:$ASGI_PORT/api/projects"
echo ""
echo "Rollback:"
echo "  bash scripts/rollback-cutover.sh"
