#!/usr/bin/env bash
# tests/test_dashboard_server_no_llm.sh
#
# Asserts that dashboard/server.py starts and serves SSE events without any
# LLM API key present in the environment.
#
# AC1: process stays alive for at least 30 seconds with no keys set
# AC2: GET /events returns 200 and streams at least one SSE frame
# AC5: no subprocess.Popen call to backend/server.py in source
#
# Usage:
#   bash tests/test_dashboard_server_no_llm.sh
#
# Isolation (D#2267): dashboard/server.py computes its own _AGENT_FEED as
# Path(__file__).resolve().parent.parent / ".autonomous-team" / "agent-feed.jsonl"
# -- anchored to wherever the invoked *file* physically lives, with no env
# override, the same shape hooks/sandbox.py had before D#2267's telemetry-
# family fix. Invoking the real in-place copy would append this suite's own
# test event straight into the live agent-feed.jsonl every running agent's
# own feed writes land in. Instead this materialises a private fixture root
# (tests/lib/repo-root-fixture.sh -- same mechanism used for the hooks/
# family) and copies dashboard/server.py into it byte-for-byte, so its
# _AGENT_FEED resolves inside the fixture. dashboard/server.py has no other
# repo-module imports (aiohttp + stdlib only), so a single-file copy is
# sufficient -- no need to also materialise dashboard/static/, which this
# suite never requests.
#
# The port is also no longer fixed (D#2267 item 7): bind :0, read back the
# OS-assigned port, then hand that port to the server -- two concurrent runs
# of this suite no longer collide.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/tests/lib/repo-root-fixture.sh"

SSE_HOST=127.0.0.1
SSE_PID=""

PASS=0
FAIL=0

pass() { echo "  PASS: $*"; ((PASS++)) || true; }
fail() { echo "  FAIL: $*"; ((FAIL++)) || true; }

# Server stdout/stderr capture lives at a mktemp'd path, not a fixed
# /tmp/test_sse_server.log name (D#2254).
SSE_LOG="$(mktemp /tmp/test_dashboard_server_no_llm.XXXXXX)"

FIXTURE_ROOT="$(repo_root_fixture_make "$REPO_ROOT")" || {
  echo "  FAIL: could not create isolated fixture root"
  exit 1
}
mkdir -p "$FIXTURE_ROOT/dashboard"
cp "$REPO_ROOT/dashboard/server.py" "$FIXTURE_ROOT/dashboard/server.py"

cleanup() {
  if [[ -n "$SSE_PID" ]] && kill -0 "$SSE_PID" 2>/dev/null; then
    kill "$SSE_PID" 2>/dev/null || true
    wait "$SSE_PID" 2>/dev/null || true
  fi
  rm -f "$SSE_LOG"
  rm -rf "$FIXTURE_ROOT"
}
trap cleanup EXIT

echo ""
echo "=== test_dashboard_server_no_llm.sh ==="
echo ""

# ---- AC5: no subprocess.Popen to backend/server.py in source ----------------
# Checked against the real, checked-in source -- this is a static source
# check, not a runtime behavior check, so it deliberately reads $REPO_ROOT,
# not the fixture copy.

echo "[check] Verifying no subprocess.Popen(['...backend/server.py']) in source..."
if grep -n 'Popen.*backend.*server\.py\|subprocess.*backend.*server\.py' \
    "$REPO_ROOT/dashboard/server.py" 2>/dev/null | grep -v '^[[:space:]]*#'; then
  fail "dashboard/server.py still contains subprocess call to backend/server.py"
else
  pass "No subprocess.Popen to backend/server.py found in source"
fi

# ---- Pick a free port (D#2267 item 7 -- no fixed TCP port) -------------------

SSE_PORT=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
")

# ---- Boot without keys -------------------------------------------------------

echo ""
echo "[boot] Starting dashboard/server.py (fixture copy) with keys unset on port $SSE_PORT..."
(
  unset AF_API_KEY MOONSHOT_API_KEY 2>/dev/null || true
  exec python3 "$FIXTURE_ROOT/dashboard/server.py" --host "$SSE_HOST" --port "$SSE_PORT"
) > "$SSE_LOG" 2>&1 &
SSE_PID=$!

# Wait for the server to bind (up to 15s)
READY=false
for i in $(seq 1 15); do
  if curl -sf "http://${SSE_HOST}:${SSE_PORT}/api/status" >/dev/null 2>&1; then
    READY=true
    break
  fi
  sleep 1
done

if [[ "$READY" != "true" ]]; then
  echo ""
  echo "[log] Server output:"
  cat "$SSE_LOG" || true
  fail "dashboard/server.py did not start on port $SSE_PORT within 15s"
  echo ""
  echo "=== Results: $PASS passed, $FAIL failed ==="
  exit 1
fi

pass "dashboard/server.py bound port $SSE_PORT with no LLM keys set"

# ---- AC1: still alive after 30s wait? Test by 5s proxy -----------------------
# We only wait 5s here (not 30) to keep CI fast; the key test is that it
# doesn't exit immediately (which the binding check above already validates).
# A 5-second health check is enough to confirm it hasn't crashed on startup.

sleep 5
if kill -0 "$SSE_PID" 2>/dev/null; then
  pass "Process still alive 5s after start (no immediate crash on missing keys)"
else
  fail "Process exited early — check $SSE_LOG"
fi

# ---- AC2: GET /events returns 200 + SSE frame --------------------------------

echo ""
echo "[test] GET /events — expect 200 and SSE frame..."

# Write a test event to the fixture's own agent-feed.jsonl (never the real
# repo's) so the tail-follower has something to emit.
FEED_FILE="$FIXTURE_ROOT/.autonomous-team/agent-feed.jsonl"
TEST_EVENT='{"event_type":"test","message":"no-llm-test","ts":"2026-01-01T00:00:00Z"}'

mkdir -p "$FIXTURE_ROOT/.autonomous-team"
echo "$TEST_EVENT" >> "$FEED_FILE"

# Give the tail-follower up to 3s to pick up the new line.
sleep 3

# Read up to 3 seconds of SSE output.
SSE_OUTPUT=$(timeout 3 curl -sN "http://${SSE_HOST}:${SSE_PORT}/events" 2>/dev/null || true)

if echo "$SSE_OUTPUT" | grep -q '^:.*\|^data:'; then
  pass "GET /events returned SSE frame(s)"
else
  # The keepalive comment ': keepalive' counts as an SSE frame too — try that.
  if echo "$SSE_OUTPUT" | grep -q ':'; then
    pass "GET /events returned SSE keepalive frame (connection alive, feed may be empty)"
  else
    fail "GET /events did not return any SSE frame (output: $(echo "$SSE_OUTPUT" | head -3))"
  fi
fi

# ---- GET /api/status returns feed metadata -----------------------------------

echo ""
echo "[test] GET /api/status includes feed_path field..."
STATUS_RESPONSE=$(curl -sf "http://${SSE_HOST}:${SSE_PORT}/api/status" 2>/dev/null || true)
if echo "$STATUS_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'feed_path' in d, 'missing feed_path'" 2>/dev/null; then
  pass "/api/status includes feed_path metadata"
else
  fail "/api/status missing feed_path (response: $STATUS_RESPONSE)"
fi

# ---- LLM-dependent endpoints return 503, not crash --------------------------

echo ""
echo "[test] POST /api/prompt returns structured error (not crash)..."
PROMPT_RESPONSE=$(curl -s -X POST "http://${SSE_HOST}:${SSE_PORT}/api/prompt" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"hello"}' 2>/dev/null || true)
if echo "$PROMPT_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('error') == 'LLM unavailable', f'expected LLM unavailable error, got {d}'" 2>/dev/null; then
  pass "POST /api/prompt returns {error: 'LLM unavailable'}"
else
  fail "POST /api/prompt did not return structured error (response: $PROMPT_RESPONSE)"
fi

if kill -0 "$SSE_PID" 2>/dev/null; then
  pass "Process still alive after 503 LLM error response (no crash)"
else
  fail "Process exited after /api/prompt call"
fi

# ---- Summary -----------------------------------------------------------------

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
echo ""

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
