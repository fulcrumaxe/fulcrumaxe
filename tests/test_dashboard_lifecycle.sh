#!/usr/bin/env bash
# tests/test_dashboard_lifecycle.sh — start/stop dashboard lifecycle regression
# test (D#1817).
#
# Boots real services via scripts/start-dashboard.sh / stop-dashboard.sh —
# Bugs 1-3 fixed here are socket-level (address family) and PID-ownership
# checks, which fixture env vars can't stand in for the way
# tests/test_ci_status_check.sh's fixtures do for its pure-logic lib. This
# test follows that file's plain-bash pass/fail-counter convention for
# structure and output, but exercises the real scripts end to end instead.
#
# Uses AF_API_PORT / AF_RPC_PORT / AF_SSE_PORT / AF_VITE_PORT (which both
# scripts already honor) so this can run without colliding with a dashboard
# instance already up on the project defaults (18099/8765/8420/5173) —
# override them if the alternate ports below are also taken.
#
# Run: bash tests/test_dashboard_lifecycle.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
START="$REPO_ROOT/scripts/start-dashboard.sh"
STOP="$REPO_ROOT/scripts/stop-dashboard.sh"

export AF_DASHBOARD_CI=1
export AF_API_PORT="${AF_API_PORT:-18199}"
export AF_RPC_PORT="${AF_RPC_PORT:-8865}"
export AF_SSE_PORT="${AF_SSE_PORT:-8520}"
export AF_VITE_PORT="${AF_VITE_PORT:-5273}"

# start-dashboard.sh / stop-dashboard.sh now honor AF_DASHBOARD_PID_DIR for
# PID files (D#2267) -- pointing it at a scratch dir keeps this suite's PID
# files out of the checked-out .autonomous-team/ tree, the same way the
# AF_*_PORT overrides above already keep it off the project's default ports.
# (Deliberately not AUTONOMOUS_TEAM_STATE_DIR: that also switches
# start-dashboard.sh into delegated mode, which changes the "State dir:"
# line DL-2 below asserts on -- D#1635 -- so PID-file isolation uses its own
# narrow lever instead.)
export AF_DASHBOARD_PID_DIR="$(mktemp -d)"

# start-dashboard.sh ALSO writes a second copy of dashboard-runtime.json to
# $STATE_DIR (for fleet discovery) even in this suite's deliberately
# non-delegated mode -- and with AUTONOMOUS_TEAM_STATE_DIR unset (required
# for DL-2 above), STATE_DIR resolves to the operator's real
# ~/.autonomous-forever-state, so without this override this suite would
# overwrite that real file with dead test PIDs on every run. Same
# independent-of-STATE_DIR override shape as AF_DASHBOARD_PID_DIR: STATE_DIR
# itself, and everything DL-2 reads, stays exactly as it resolves today.
export AF_DASHBOARD_STATE_RUNTIME_FILE="$(mktemp -d)/dashboard-runtime.json"

PASS=0
FAIL=0
pass() { echo "  PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $*"; FAIL=$((FAIL + 1)); }

_port_bound() {
  ss -ltn "sport = :$1" 2>/dev/null | grep -q LISTEN
}

DECOY_PID=""
cleanup() {
  if [[ -n "$DECOY_PID" ]]; then
    kill "$DECOY_PID" 2>/dev/null || true
    wait "$DECOY_PID" 2>/dev/null || true
  fi
  bash "$STOP" >/dev/null 2>&1 || true
  rm -rf "$AF_DASHBOARD_PID_DIR" "$(dirname "$AF_DASHBOARD_STATE_RUNTIME_FILE")"
}
trap cleanup EXIT

echo ""
echo "=== test_dashboard_lifecycle.sh ==="
echo "Ports under test: vite=$AF_VITE_PORT api=$AF_API_PORT rpc=$AF_RPC_PORT sse=$AF_SSE_PORT"
echo ""

# Baseline: make sure nothing from a previous run is left over.
bash "$STOP" >/dev/null 2>&1 || true
sleep 1

# -----------------------------------------------------------------------
# DL-1 (AC-1, AC-2, AC-3): clean start — exit 0, no ERROR, all four ports up
# -----------------------------------------------------------------------
echo "=== DL-1: clean start ==="
START_OUT="$(bash "$START" 2>&1)"
START_RC=$?
echo "$START_OUT" | tail -8

if [[ $START_RC -eq 0 ]]; then pass "DL-1: start-dashboard.sh exited 0"; else fail "DL-1: start-dashboard.sh exited $START_RC"; fi
if echo "$START_OUT" | grep -q "ERROR"; then fail "DL-1: stdout contains ERROR"; else pass "DL-1: no ERROR in stdout"; fi
if echo "$START_OUT" | grep -q "did not respond"; then fail "DL-1: stdout contains 'did not respond'"; else pass "DL-1: no 'did not respond' in stdout"; fi

ALL_UP=true
for port in "$AF_VITE_PORT" "$AF_API_PORT" "$AF_RPC_PORT" "$AF_SSE_PORT"; do
  _port_bound "$port" || ALL_UP=false
done
if [[ "$ALL_UP" == "true" ]]; then pass "DL-1: all four ports listening"; else fail "DL-1: one or more ports not listening"; fi

# -----------------------------------------------------------------------
# DL-2 (AC-9, AC-10): project identity line, and state-dir line unchanged
# -----------------------------------------------------------------------
echo ""
echo "=== DL-2: project identity + state dir ==="
if echo "$START_OUT" | grep '^\[start-dashboard\] Project:' | grep -q "autonomous-forever"; then
  fail "DL-2: Project: line still says autonomous-forever"
else
  pass "DL-2: Project: line does not say autonomous-forever"
fi
if echo "$START_OUT" | grep -q "State dir:.*\.autonomous-forever-state"; then
  pass "DL-2: State dir unchanged (still .autonomous-forever-state — D#1635)"
else
  fail "DL-2: State dir line missing or changed"
fi

# -----------------------------------------------------------------------
# DL-3 (AC-5): vite PID file records the real listener, not a wrapper
# -----------------------------------------------------------------------
echo ""
echo "=== DL-3: vite PID ownership ==="
VITE_PID_FILE="$AF_DASHBOARD_PID_DIR/dashboard-vite.pid"
if [[ -f "$VITE_PID_FILE" ]]; then
  VITE_PID="$(cat "$VITE_PID_FILE")"
  COMM="$(ps -p "$VITE_PID" -o comm= 2>/dev/null || echo "<gone>")"
  if [[ "$COMM" == "node" ]]; then
    pass "DL-3: dashboard-vite.pid (PID $VITE_PID) is 'node', not a bash wrapper"
  else
    fail "DL-3: dashboard-vite.pid (PID $VITE_PID) comm='$COMM', expected 'node'"
  fi
else
  fail "DL-3: dashboard-vite.pid not found"
fi

# -----------------------------------------------------------------------
# DL-4 (AC-4): stop frees all four ports, vite port specifically
# -----------------------------------------------------------------------
echo ""
echo "=== DL-4: stop frees all ports ==="
STOP_OUT="$(bash "$STOP" 2>&1)"
STOP_RC=$?
echo "$STOP_OUT" | tail -8
if [[ $STOP_RC -eq 0 ]]; then pass "DL-4: stop-dashboard.sh exited 0"; else fail "DL-4: stop-dashboard.sh exited $STOP_RC"; fi

ALL_FREE=true
for port in "$AF_VITE_PORT" "$AF_API_PORT" "$AF_RPC_PORT" "$AF_SSE_PORT"; do
  _port_bound "$port" && ALL_FREE=false
done
if [[ "$ALL_FREE" == "true" ]]; then pass "DL-4: all four ports free after stop"; else fail "DL-4: one or more ports still bound after stop"; fi

if _port_bound "$AF_VITE_PORT"; then
  fail "DL-4: vite port ($AF_VITE_PORT) specifically still bound (Bug 2 regression)"
else
  pass "DL-4: vite port ($AF_VITE_PORT) specifically free (Bug 2 regression check)"
fi

# -----------------------------------------------------------------------
# DL-5 (AC-6): pre-bound foreign port -> loud refusal, no false-ready
# -----------------------------------------------------------------------
echo ""
echo "=== DL-5: pre-bound port refusal ==="
python3 -m http.server "$AF_API_PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
DECOY_PID=$!
sleep 1
if _port_bound "$AF_API_PORT"; then
  DECOY_OUT="$(bash "$START" 2>&1)"
  DECOY_RC=$?
  echo "$DECOY_OUT" | grep -i "error\|already bound" | head -5

  if [[ $DECOY_RC -ne 0 ]]; then pass "DL-5: start-dashboard.sh exited non-zero over a decoy-bound port"; else fail "DL-5: start-dashboard.sh exited 0 with a decoy on the port"; fi
  if echo "$DECOY_OUT" | grep -q "PID $DECOY_PID"; then pass "DL-5: decoy PID named in output"; else fail "DL-5: decoy PID not named in output"; fi
  if echo "$DECOY_OUT" | grep -q "backend/api.py ready"; then fail "DL-5: falsely reported backend/api.py ready"; else pass "DL-5: did not report backend/api.py ready over the decoy"; fi
else
  fail "DL-5: decoy process did not bind $AF_API_PORT — cannot exercise refusal"
fi

kill "$DECOY_PID" 2>/dev/null || true
wait "$DECOY_PID" 2>/dev/null || true
DECOY_PID=""
sleep 1

# -----------------------------------------------------------------------
# DL-6 (AC-12): start twice in a row without an intervening stop
# -----------------------------------------------------------------------
echo ""
echo "=== DL-6: start twice without stop ==="
bash "$STOP" >/dev/null 2>&1 || true
sleep 1
bash "$START" >/dev/null 2>&1
FIRST_RC=$?
SECOND_OUT="$(bash "$START" 2>&1)"
SECOND_RC=$?
if [[ $FIRST_RC -eq 0 && $SECOND_RC -ne 0 ]]; then
  pass "DL-6: second start (no stop between) exits non-zero"
else
  fail "DL-6: expected first_rc=0 second_rc!=0, got first=$FIRST_RC second=$SECOND_RC"
fi
if echo "$SECOND_OUT" | grep -qE "ready \(PID"; then
  fail "DL-6: second start printed a 'ready' line for a service it did not start"
else
  pass "DL-6: second start did not print any false 'ready' line"
fi

# -----------------------------------------------------------------------
# Cleanup + summary
# -----------------------------------------------------------------------
bash "$STOP" >/dev/null 2>&1 || true

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
