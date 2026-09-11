#!/usr/bin/env bash
# scripts/pr-browser-preview.sh — serve a code-plane PR's dashboard head on
# its own port, for browser-testing (D#2549).
#
# THE BUG THIS FIXES: the long-running vite on 5173 serves the shared
# checkout's dashboard/ directory — main, always — not any PR's code. Every
# browser-tester screenshot of 5173 was a screenshot of main (measured,
# D#2549). This script materializes the PR's actual head into a
# private scratch tree and starts a SEPARATE vite instance on a free port
# against it. It never binds, kills, or restarts anything already running:
# 5173 and the three backend services (api/rpc/sse) are left exactly as they
# were; this only ever adds a new frontend, pointed at the same live
# backends so the data it renders is real.
#
# WHAT THIS ACTUALLY EXECUTES, DISCLOSED: starting vite below runs
# `dashboard/vite.config.ts` (and anything it imports — postcss/tailwind
# config included) FROM THE PR HEAD, in Node, as the user invoking this
# script, with the full inherited environment. That is inherent to
# browser-testing a PR's real frontend build, not a bug — but it is real
# code execution, not a passive preview. `pbt_materialize` below reuses
# `verify_tree_build` (scripts/lib/verify-tree.sh) for integrity, not
# confinement: that mechanism detects the materialized tree drifting under
# this process mid-run, it does not sandbox what the tree's own code does
# once vite executes it. Treat invocation of this script the same as running
# arbitrary code from the PR head, because that is what it does.
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
# materialized tree AND, if it is still alive and still bound to its
# recorded port, the already-running vite process for it — implemented
# below via a small `.pr-browser-preview.info` marker inside the scratch
# tree, not merely claimed. Materialized scratch trees under
# $SCRATCH_ROOT are NOT deleted automatically once their vite process ends
# — this is deliberate (the tree is what a *later* run reuses), but it does
# mean stale trees accumulate over time; there is no automatic reaping yet.

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

# If a still-alive vite from a previous run of this same script is already
# serving this exact DEST, reuse it instead of starting another one — the
# header above claims this; this is what actually implements it. A stale
# marker (process dead, or a different process now holding that port) is
# never trusted on its own — both the pid AND the port binding are checked
# live before reuse, so a leftover marker file just falls through to
# starting a fresh vite below.
INFO_FILE="$DEST/.pr-browser-preview.info"
if [[ -f "$INFO_FILE" ]]; then
  EXISTING_PID="" EXISTING_PORT=""
  read -r EXISTING_PID EXISTING_PORT < "$INFO_FILE" 2>/dev/null || true
  if [[ -n "$EXISTING_PID" && -n "$EXISTING_PORT" ]] \
     && kill -0 "$EXISTING_PID" 2>/dev/null \
     && ss -ltnp "sport = :$EXISTING_PORT" 2>/dev/null | grep -q "pid=$EXISTING_PID,"; then
    log "reusing already-running vite (pid $EXISTING_PID) on port $EXISTING_PORT for PR #$PR"
    echo "http://localhost:$EXISTING_PORT"
    exit 0
  fi
  log "stale preview marker at $INFO_FILE (process no longer bound) — starting a fresh vite"
fi

# Refuse outright when the PR head commits its own dashboard/node_modules.
# Checked against the TREE OBJECT at $HEAD_SHA (git ls-tree), never a
# filesystem `-e` test against the materialized tree: git preserves the
# 100755 mode bit on a committed file, so a head-supplied binary would pass
# an executable check, and a committed node_modules SYMLINK (mode 120000)
# sits outside both verify-tree.sh's write-protection chmod and its
# manifest hashing (see that file's own header). A head that ships either
# must not be able to supply what vite treats as its own dependency tree or
# its own binary.
if git ls-tree -r --name-only "$HEAD_SHA" -- dashboard/node_modules 2>/dev/null | grep -q .; then
  log "ERROR: PR #$PR head commits dashboard/node_modules — refusing to preview it. Remove the committed node_modules from the PR."
  exit 1
fi

# node_modules is untracked, so it is absent from the fresh tree (the guard
# above rejects a head that commits one anyway). Reuse the shared checkout's
# install READ-ONLY — a symlink only ever reads it, it never runs npm
# install and never writes into the shared tree, and it is far cheaper than
# re-installing per PR.
if [[ ! -e "$DASHBOARD_DIR/node_modules" ]]; then
  SHARED_MODULES="$REPO_ROOT/dashboard/node_modules"
  if [[ -d "$SHARED_MODULES" ]]; then
    if ! ln -s "$SHARED_MODULES" "$DASHBOARD_DIR/node_modules"; then
      log "ERROR: failed to symlink node_modules into $DASHBOARD_DIR"
      exit 1
    fi
  else
    log "ERROR: no node_modules at $SHARED_MODULES to link — run npm install there first"
    exit 1
  fi
fi

# Resolve vite from the TRUSTED repo root's own install, never through the
# scratch tree — belt-and-braces alongside the node_modules guard above
# rather than relying on that guard alone: even if some future code path
# creates $DASHBOARD_DIR/node_modules some other way, the binary this script
# actually execs still only ever comes from $REPO_ROOT.
VITE_BIN="$REPO_ROOT/dashboard/node_modules/.bin/vite"
if [[ ! -x "$VITE_BIN" ]]; then
  log "ERROR: vite binary not found at $VITE_BIN"
  exit 1
fi

# Reuse the already-running backend services (api/rpc) for real data — this
# script never starts, stops, or touches them. Read-only. $RUNTIME_FILE is
# passed via argv, not interpolated into the program text, so its contents
# can never be read as part of the python source.
RUNTIME_FILE="$REPO_ROOT/.autonomous-team/dashboard-runtime.json"
API_PORT=18099
RPC_PORT=8765
if [[ -f "$RUNTIME_FILE" ]]; then
  if PORTS_OUT="$(python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    data = {}
ports = data.get("ports", {})
print(ports.get("api", 18099), ports.get("rpc", 8765))
' "$RUNTIME_FILE" 2>/dev/null)"; then
    read -r API_PORT RPC_PORT <<< "$PORTS_OUT"
  fi
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

echo "$VITE_PID $PORT" > "$INFO_FILE"
log "ready"
echo "http://localhost:$PORT"
