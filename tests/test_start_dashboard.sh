#!/usr/bin/env bash
# tests/test_start_dashboard.sh — Integration test for start-dashboard.sh
#
# Boots all four services via start-dashboard.sh (CI mode), verifies each
# endpoint, then stops cleanly. Total runtime target: < 60 seconds.
#
# HARD RULE: This test MUST NOT invoke `claude`, `claude -p`, `_start_loop_run`,
# or any /loop trigger. Services are tested by curling HTTP endpoints only.
#
# Usage:
#   bash tests/test_start_dashboard.sh
#   AF_DASHBOARD_CI=1 bash tests/test_start_dashboard.sh  # already default in tests

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AF_DASHBOARD_CI=1

# start-dashboard.sh honors AUTONOMOUS_TEAM_STATE_DIR (delegated mode, already
# existing production behavior) to redirect dashboard-runtime.json entirely
# out of the checked-out .autonomous-team/ tree, and AF_DASHBOARD_PID_DIR
# (D#2267) to redirect PID files the same way -- stop-dashboard.sh honors
# both too, so it can still find what start-dashboard.sh wrote. This suite
# has no assertion on the "State dir:" identity line (unlike
# test_dashboard_lifecycle.sh's DL-2 / D#1635), so full delegated mode is
# safe to use here.
export AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)"
export AF_DASHBOARD_PID_DIR="$(mktemp -d)"

PASS=0
FAIL=0

pass() { echo "  PASS: $*"; ((PASS++)) || true; }
fail() { echo "  FAIL: $*"; ((FAIL++)) || true; }

cleanup() {
  rm -rf "$AUTONOMOUS_TEAM_STATE_DIR" "$AF_DASHBOARD_PID_DIR"
}
trap cleanup EXIT

echo ""
echo "=== test_start_dashboard.sh ==="
echo ""

# ---- Cleanup from any previous test run ------------------------------------

echo "[setup] Stopping any stale dashboard processes..."
bash "$REPO_ROOT/scripts/stop-dashboard.sh" 2>/dev/null || true
sleep 1

# ---- Start dashboard -------------------------------------------------------

echo "[start] Running start-dashboard.sh..."
START_OUTPUT=$(bash "$REPO_ROOT/scripts/start-dashboard.sh" 2>&1)
echo "$START_OUTPUT" | tail -5

if echo "$START_OUTPUT" | grep -q "Dashboard ready:"; then
  pass "start-dashboard.sh printed 'Dashboard ready:'"
else
  fail "start-dashboard.sh did not print 'Dashboard ready:'"
fi

# ---- Read runtime config ---------------------------------------------------
# Delegated mode (AUTONOMOUS_TEAM_STATE_DIR, set above) makes start-dashboard.sh
# write dashboard-runtime.json only to the state dir, never the checked-out
# .autonomous-team/ tree (D#2267) -- backend/api.py's own /api/config handler
# already checks the state-dir file first, so this doesn't change what the
# services being tested actually see.

RUNTIME_FILE="$AUTONOMOUS_TEAM_STATE_DIR/dashboard-runtime.json"
if [[ -f "$RUNTIME_FILE" ]]; then
  pass "dashboard-runtime.json was created"
  RPC_TOKEN=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('rpcToken',''))" 2>/dev/null || true)
  RPC_PORT=$(python3 -c "import json; d=json.load(open('$RUNTIME_FILE')); print(d.get('ports',{}).get('rpc',8765))" 2>/dev/null || echo "8765")
else
  fail "dashboard-runtime.json was NOT created"
  RPC_TOKEN=""
  RPC_PORT="8765"
fi

# ---- Test /api/config (backend/api.py:18099) --------------------------------

echo ""
echo "[test] /api/config endpoint..."
CONFIG_RESPONSE=$(curl -sf http://localhost:18099/api/config 2>/dev/null || true)
if echo "$CONFIG_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('rpcBaseUrl') and d.get('rpcToken'), 'missing fields'" 2>/dev/null; then
  pass "/api/config returns rpcBaseUrl and rpcToken"
else
  fail "/api/config did not return expected fields (response: $CONFIG_RESPONSE)"
fi

# /api/config must refuse non-localhost Origin
CORS_RESPONSE=$(curl -sf -H "Origin: https://evil.example.com" http://localhost:18099/api/config 2>/dev/null || echo "rejected")
if echo "$CORS_RESPONSE" | grep -q "forbidden\|rejected\|403"; then
  pass "/api/config rejects non-localhost Origin"
else
  fail "/api/config did NOT reject non-localhost Origin (response: $CORS_RESPONSE)"
fi

# ---- Test /rpc (backend/server.py:8765) ------------------------------------

echo ""
echo "[test] JSON-RPC /rpc endpoint..."
# No repo-level dashboard-token fallback here (D#2267): backend/server.py
# always writes that file at the checked-out $REPO_ROOT/.autonomous-team/
# path with no state-dir override, so reading it would defeat the isolation
# above. It's also unreachable in practice -- RUNTIME_FILE's own rpcToken
# field is populated from that same file by start-dashboard.sh before the
# runtime JSON is written, so $RPC_TOKEN is already set whenever RUNTIME_FILE
# resolved successfully above. If it didn't, the earlier
# "dashboard-runtime.json was NOT created" failure already reported that,
# and this /rpc call fails informatively on its own instead.

RPC_RESPONSE=$(curl -sf -X POST "http://localhost:${RPC_PORT}/rpc" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RPC_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"discussions.list","params":{}}' 2>/dev/null || true)

if echo "$RPC_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'result' in d or 'error' in d, 'not a JSON-RPC response'" 2>/dev/null; then
  pass "/rpc responds with valid JSON-RPC envelope"
else
  fail "/rpc did not return valid JSON-RPC response (response: $RPC_RESPONSE)"
fi

# ---- Test /api/projects KPI endpoint (backend/api.py:18099) ----------------

echo ""
echo "[test] /api/projects/autonomous-forever/kpi endpoint..."
KPI_RESPONSE=$(curl -sf http://localhost:18099/api/projects/autonomous-forever/kpi 2>/dev/null || true)
if [[ -n "$KPI_RESPONSE" ]]; then
  pass "/api/projects/autonomous-forever/kpi responded ($(echo "$KPI_RESPONSE" | wc -c) bytes)"
else
  fail "/api/projects/autonomous-forever/kpi returned empty response"
fi

# ---- Test health endpoint --------------------------------------------------

echo ""
echo "[test] /health endpoint..."
HEALTH_RESPONSE=$(curl -sf http://localhost:18099/health 2>/dev/null || true)
if echo "$HEALTH_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('ok'), 'ok not true'" 2>/dev/null; then
  pass "/health returns {ok: true}"
else
  fail "/health did not return {ok: true} (response: $HEALTH_RESPONSE)"
fi

# ---- Test Vite dev server --------------------------------------------------

echo ""
echo "[test] Vite dev server on port 5173..."
VITE_RESPONSE=$(curl -sf http://localhost:5173 2>/dev/null | head -c 500 || true)
if echo "$VITE_RESPONSE" | grep -qi "html\|vite\|react"; then
  pass "Vite dev server responds with HTML"
else
  fail "Vite dev server did not respond with HTML"
fi

# ---- Stop dashboard --------------------------------------------------------

echo ""
echo "[stop] Running stop-dashboard.sh..."
bash "$REPO_ROOT/scripts/stop-dashboard.sh"
sleep 2

# ---- Verify all ports are free ---------------------------------------------

echo ""
echo "[verify] Checking all ports are released..."
PORTS_FREE=true
for port in 18099 8765 8420 5173; do
  if lsof -ti tcp:"$port" >/dev/null 2>&1; then
    fail "Port $port is still bound after stop"
    PORTS_FREE=false
  fi
done
if [[ "$PORTS_FREE" == "true" ]]; then
  pass "All four ports (18099, 8765, 8420, 5173) released cleanly"
fi

# ---- Summary ---------------------------------------------------------------

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
echo ""

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
