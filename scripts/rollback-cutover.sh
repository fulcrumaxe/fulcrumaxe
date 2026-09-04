#!/usr/bin/env bash
# scripts/rollback-cutover.sh — one-command cutover rollback
#
# Reverts .autonomous-team/dashboard-runtime.json to the pre-cutover backup
# (pointing back at legacy :18099/:8765) and stops the FastAPI uvicorn
# process on :18100 (or AF_ASGI_PORT).
#
# Idempotent: safe to run even if a cutover was never performed or already
# rolled back.  Will never affect the legacy :18099/:8765 services.
#
# Usage:
#   bash scripts/rollback-cutover.sh
#   AF_ASGI_PORT=18100 bash scripts/rollback-cutover.sh
#   AF_RUNTIME_FILE=/tmp/test-runtime.json bash scripts/rollback-cutover.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$REPO_ROOT/.autonomous-team"

ASGI_PORT="${AF_ASGI_PORT:-18100}"
RUNTIME_FILE="${AF_RUNTIME_FILE:-$REPO_ROOT/.autonomous-team/dashboard-runtime.json}"
ASGI_PID_FILE="$PID_DIR/dashboard-asgi.pid"
ASGI_BACKUP_FILE="${RUNTIME_FILE}.pre-cutover-backup"

log() { echo "[rollback-cutover] $*" >&2; }

# ---------------------------------------------------------------------------
# Step 1: Stop the FastAPI process on :ASGI_PORT
# ---------------------------------------------------------------------------

_stopped=false

# Try PID file first
if [[ -f "$ASGI_PID_FILE" ]]; then
  ASGI_PID=$(cat "$ASGI_PID_FILE" 2>/dev/null || true)
  if [[ -n "$ASGI_PID" ]] && kill -0 "$ASGI_PID" 2>/dev/null; then
    log "Stopping FastAPI on port $ASGI_PORT (PID $ASGI_PID)"
    kill "$ASGI_PID" 2>/dev/null || true
    sleep 1
    if kill -0 "$ASGI_PID" 2>/dev/null; then
      kill -9 "$ASGI_PID" 2>/dev/null || true
      log "FastAPI (PID $ASGI_PID) force-killed"
    else
      log "FastAPI (PID $ASGI_PID) stopped"
    fi
    _stopped=true
  else
    log "FastAPI PID $ASGI_PID not found in process table (already stopped?)"
  fi
  rm -f "$ASGI_PID_FILE"
fi

# Belt-and-suspenders: kill by port if still running
PORT_PID=$(lsof -ti tcp:"$ASGI_PORT" 2>/dev/null || true)
if [[ -n "$PORT_PID" ]]; then
  log "Found process on port $ASGI_PORT (PID $PORT_PID) — killing"
  kill "$PORT_PID" 2>/dev/null || true
  sleep 1
  if kill -0 "$PORT_PID" 2>/dev/null; then
    kill -9 "$PORT_PID" 2>/dev/null || true
  fi
  _stopped=true
fi

if [[ "$_stopped" != "true" ]]; then
  log "No FastAPI process found on port $ASGI_PORT — nothing to stop"
fi

# ---------------------------------------------------------------------------
# Step 2: Restore runtime.json from pre-cutover backup
# ---------------------------------------------------------------------------

if [[ -f "$ASGI_BACKUP_FILE" ]]; then
  log "Restoring $RUNTIME_FILE from backup..."
  cp "$ASGI_BACKUP_FILE" "$RUNTIME_FILE"
  rm -f "$ASGI_BACKUP_FILE"
  log "Restored. Runtime now points at legacy ports."
  log ""
  log "Restored config:"
  python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('$RUNTIME_FILE').read_text())
print(f'  rpcBaseUrl = {d.get(\"rpcBaseUrl\", \"?\")}')
ports = d.get('ports', {})
print(f'  ports.api  = {ports.get(\"api\", \"?\")}')
print(f'  ports.rpc  = {ports.get(\"rpc\", \"?\")}')
print(f'  ports.sse  = {ports.get(\"sse\", \"?\")}')
" >&2 || true
else
  log "No pre-cutover backup found at $ASGI_BACKUP_FILE"
  if [[ -f "$RUNTIME_FILE" ]]; then
    log "Reverting runtime.json manually to legacy defaults..."
    python3 - "$RUNTIME_FILE" <<'PYEOF'
import json, sys, pathlib

runtime_path = sys.argv[1]
d = json.loads(pathlib.Path(runtime_path).read_text()) if pathlib.Path(runtime_path).exists() else {}

# Revert to legacy defaults
d["rpcBaseUrl"] = "http://localhost:8765"

ports = d.get("ports", {})
ports["api"] = 18099
ports["rpc"] = 8765
# sse may still be running on 8420
if "sse" in ports:
    ports["sse"] = 8420
d["ports"] = ports

# Remove cutover markers
d.pop("cutover_at", None)
d.pop("cutover_port", None)
d.pop("legacy_api_port", None)
d.pop("legacy_rpc_port", None)
pids = d.get("pids", {})
pids.pop("asgi", None)
d["pids"] = pids

pathlib.Path(runtime_path).write_text(json.dumps(d, indent=2) + "\n")
print("[rollback-cutover] runtime.json reverted to legacy defaults")
PYEOF
  else
    log "No runtime.json found — nothing to revert"
  fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "ROLLED BACK"
echo ""
echo "  FastAPI on :$ASGI_PORT: STOPPED"
echo "  Runtime file reverted to legacy :18099/:8765"
echo ""
echo "Verify legacy is still up:"
echo "  curl http://127.0.0.1:18099/health"
