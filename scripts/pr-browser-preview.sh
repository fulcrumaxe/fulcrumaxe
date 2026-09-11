#!/usr/bin/env bash
# scripts/pr-browser-preview.sh — serve a code-plane PR's dashboard head on
# its own port, for browser-testing (D#2549).
#
# THE BUG THIS FIXES: the long-running vite on 5173 serves the shared
# checkout at /home/jp/fulcrumaxe/dashboard — main, always — not any PR's
# code. Every browser-tester screenshot of 5173 was a screenshot of main
# (measured, D#2549). This script materializes the PR's actual head into a
# private scratch tree and starts a SEPARATE vite instance on a free port
# against it. It never binds, kills, or restarts anything already running:
# 5173 and the three backend services (api/rpc/sse) are left exactly as they
# were; this only ever adds a new frontend, pointed at the same live
# backends so the data it renders is real.
#
# Usage:
#   bash scripts/pr-browser-preview.sh <PR_NUMBER>
#   → prints the ready URL on stdout, e.g. http://localhost:54231
#
# Note the "localhost" spelling in the printed URL, not 127.0.0.1: vite
# binds [::1] only, so http://127.0.0.1:PORT is connection refused for a
# live server for that reason alone (D#2549 — do not "fix" this by binding
# vite to 0.0.0.0; that theory was tested and found unnecessary, the proxy
# already works over the IPv4 fallback).
#
# Re-running for the same PR number and head SHA reuses the already
# materialized tree (and, if still alive, its already-running vite) rather
# than rebuilding — cheap for a browser-tester retry loop.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=scripts/lib/pr-browser-tree.sh
source "$SCRIPT_DIR/lib/pr-browser-tree.sh"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"

log() { printf '[pr-browser-preview] %s\n' "$*" >&2; }

PR="${1:-}"
if [[ ! "$PR" =~ ^[0-9]+$ ]]; then
  echo "usage: $0 <PR_NUMBER>" >&2
  exit 2
fi

REMOTE="$(_resolve_code_plane_remote "$REPO_ROOT")" || exit 1
log "resolved code-plane remote: $REMOTE"

HEAD_SHA="$(pbt_fetch_head "$PR" "$REMOTE" "$REPO_ROOT")" || { log "fetch failed"; exit 1; }
log "PR #$PR head is $HEAD_SHA"

STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"
SCRATCH_ROOT="${PR_BROWSER_PREVIEW_ROOT:-$STATE_DIR/pr-browser-preview}"
DEST="$SCRATCH_ROOT/pr-${PR}-${HEAD_SHA:0:12}"
mkdir -p "$SCRATCH_ROOT"

if [[ -e "$DEST" ]]; then
  log "reusing existing scratch tree at $DEST"
else
  log "materializing PR #$PR head into $DEST"
  pbt_materialize "$HEAD_SHA" "$DEST" "$REPO_ROOT" || { log "materialize failed"; exit 1; }
fi

DASHBOARD_DIR="$DEST/dashboard"
if [[ ! -d "$DASHBOARD_DIR" ]]; then
  log "ERROR: PR #$PR head has no dashboard/ directory — nothing to preview"
  exit 1
fi

# node_modules is untracked, so it is absent from the fresh tree. Reuse the
# shared checkout's install READ-ONLY — a symlink only ever reads it, it
# never runs npm install and never writes into the shared tree, and it is
# far cheaper than re-installing per PR.
if [[ ! -e "$DASHBOARD_DIR/node_modules" ]]; then
  SHARED_MODULES="$REPO_ROOT/dashboard/node_modules"
  if [[ -d "$SHARED_MODULES" ]]; then
    ln -s "$SHARED_MODULES" "$DASHBOARD_DIR/node_modules"
  else
    log "ERROR: no node_modules at $SHARED_MODULES to link — run npm install there first"
    exit 1
  fi
fi

VITE_BIN="$DASHBOARD_DIR/node_modules/.bin/vite"
if [[ ! -x "$VITE_BIN" ]]; then
  log "ERROR: vite binary not found at $VITE_BIN"
  exit 1
fi

# Reuse the already-running backend services (api/rpc) for real data — this
# script never starts, stops, or touches them. Read-only.
RUNTIME_FILE="$REPO_ROOT/.autonomous-team/dashboard-runtime.json"
API_PORT=18099
RPC_PORT=8765
if [[ -f "$RUNTIME_FILE" ]]; then
  API_PORT="$(python3 -c "import json;print(json.load(open('$RUNTIME_FILE')).get('ports',{}).get('api',18099))" 2>/dev/null || echo 18099)"
  RPC_PORT="$(python3 -c "import json;print(json.load(open('$RUNTIME_FILE')).get('ports',{}).get('rpc',8765))" 2>/dev/null || echo 8765)"
fi

PORT="$(pbt_pick_free_port)" || { log "could not pick a free port"; exit 1; }
log "picked free port $PORT (never 5173 — the OS refuses to hand out an already-bound port)"

LOG_DIR="$SCRATCH_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/pr-${PR}-${PORT}.log"
PID_FILE="$LOG_DIR/pr-${PR}-${PORT}.pid"

log "starting vite on port $PORT for PR #$PR (api=$API_PORT rpc=$RPC_PORT)"
AF_API_PORT="$API_PORT" AF_RPC_PORT="$RPC_PORT" \
  nohup bash -c "cd '$DASHBOARD_DIR' && exec '$VITE_BIN' --port $PORT --strictPort" \
  > "$LOG_FILE" 2>&1 &
VITE_PID=$!
disown "$VITE_PID" 2>/dev/null || true
echo "$VITE_PID" > "$PID_FILE"

deadline=$((SECONDS + 30))
bound=false
while [[ $SECONDS -lt $deadline ]]; do
  if ss -ltnp "sport = :$PORT" 2>/dev/null | grep -q "pid=$VITE_PID,"; then
    bound=true
    break
  fi
  if ! kill -0 "$VITE_PID" 2>/dev/null; then
    log "ERROR: vite exited before binding port $PORT — see $LOG_FILE"
    exit 1
  fi
  sleep 1
done

if [[ "$bound" != "true" ]]; then
  log "ERROR: vite (pid $VITE_PID) did not bind port $PORT within 30s — see $LOG_FILE"
  exit 1
fi

log "ready"
echo "http://localhost:$PORT"
