#!/usr/bin/env bash
# api-integration-test.sh — Integration tests for backend/api.py
#
# Usage:
#   ./scripts/api-integration-test.sh [--port PORT] [--host HOST] [--auth-key KEY] [--no-start]
#
# By default, this script starts api.py on a free port, runs all tests, and
# kills the server on exit. Pass --no-start to test against an already-running server.
#
# Exit code 0 = all tests passed. Non-zero = one or more failures.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
HOST="127.0.0.1"
PORT=18999
AUTH_KEY=""
NO_START=false
API_PID=""

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)     PORT="$2"; shift 2 ;;
    --host)     HOST="$2"; shift 2 ;;
    --auth-key) AUTH_KEY="$2"; shift 2 ;;
    --no-start) NO_START=true; shift ;;
    *)          echo "Unknown arg: $1"; exit 1 ;;
  esac
done

BASE="http://${HOST}:${PORT}"
PASS_COUNT=0
FAIL_COUNT=0
FAILURES=()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
cleanup() {
  if [[ -n "$API_PID" ]]; then
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

auth_header() {
  if [[ -n "$AUTH_KEY" ]]; then
    echo "-H Authorization: Bearer $AUTH_KEY"
  fi
}

test_endpoint() {
  local method="$1"
  local path="$2"
  local expect="$3"
  local body="${4:-}"
  local description="${5:-$method $path}"

  local curl_args=(-s -w "%{http_code}" -o /dev/null -X "$method")
  if [[ -n "$AUTH_KEY" ]]; then
    curl_args+=(-H "Authorization: Bearer $AUTH_KEY")
  fi
  if [[ "$method" == "POST" && -n "$body" ]]; then
    curl_args+=(-H "Content-Type: application/json" -d "$body")
  fi
  curl_args+=("${BASE}${path}")

  local code
  code=$(curl "${curl_args[@]}" 2>/dev/null)

  if [[ "$code" == "$expect" ]]; then
    printf "  PASS  %-55s expect=%s got=%s\n" "$description" "$expect" "$code"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    printf "  FAIL  %-55s expect=%s got=%s\n" "$description" "$expect" "$code"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAILURES+=("$description (expected $expect, got $code)")
  fi
}

test_sse() {
  local path="$1"
  local description="${2:-SSE $path}"

  # Write HTTP status to a temp file to avoid mixing with curl exit-code fallback.
  local tmpfile
  tmpfile=$(mktemp)
  curl -s -o /dev/null -w "%{http_code}" --max-time 1 "${BASE}${path}" > "$tmpfile" 2>/dev/null || true
  local code
  code=$(cat "$tmpfile")
  rm -f "$tmpfile"

  # SSE endpoints return 200 and stream continuously. curl exits with code 28
  # (timeout) after --max-time, so the HTTP status code is still 200.
  if [[ "$code" == "200" ]]; then
    printf "  PASS  %-55s (SSE connected, 200)\n" "$description"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    printf "  FAIL  %-55s (expected 200 SSE, got '%s')\n" "$description" "$code"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAILURES+=("$description (SSE not connected, got $code)")
  fi
}

test_ws() {
  local path="$1"
  local description="${2:-WebSocket $path}"

  local result
  result=$(python3 - <<PYEOF 2>&1
import socket, hashlib, base64
host, port = "$HOST", $PORT
key = "dGhlIHNhbXBsZSBub25jZQ=="
sock = socket.socket()
sock.settimeout(5)
try:
    sock.connect((host, port))
    req = (
        "GET ${path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = sock.recv(1024)
        if not c: break
        buf += c
    if b"101 Switching Protocols" in buf:
        print("PASS")
    else:
        print("FAIL: " + buf.decode(errors="replace")[:200])
except Exception as e:
    print(f"ERROR: {e}")
finally:
    sock.close()
PYEOF
)

  if [[ "$result" == "PASS" ]]; then
    printf "  PASS  %-55s (101 Switching Protocols)\n" "$description"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    printf "  FAIL  %-55s (%s)\n" "$description" "$result"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAILURES+=("$description ($result)")
  fi
}

# ---------------------------------------------------------------------------
# Start server
# ---------------------------------------------------------------------------
if [[ "$NO_START" == "false" ]]; then
  REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  echo "Starting api.py on ${HOST}:${PORT} ..."
  if [[ -n "$AUTH_KEY" ]]; then
    AF_API_AUTH_KEY="$AUTH_KEY" python3 "$REPO_ROOT/backend/api.py" \
      --host "$HOST" --port "$PORT" --no-rate-limit > /tmp/api-integration-test.log 2>&1 &
  else
    python3 "$REPO_ROOT/backend/api.py" \
      --host "$HOST" --port "$PORT" --no-rate-limit > /tmp/api-integration-test.log 2>&1 &
  fi
  API_PID=$!
  sleep 3

  # Verify it started
  if ! curl -s --max-time 3 "${BASE}/health" > /dev/null 2>&1; then
    echo "ERROR: Server failed to start. Check /tmp/api-integration-test.log"
    cat /tmp/api-integration-test.log
    exit 1
  fi
  echo "Server started (PID $API_PID)"
fi

echo ""
echo "================================================================"
echo " API Integration Tests — ${BASE}"
echo "================================================================"
echo ""

# ---------------------------------------------------------------------------
# Section 1: Health / Status (no auth required)
# ---------------------------------------------------------------------------
echo "--- Health & Metrics (auth-exempt) ---"
test_endpoint GET "/health"         "200" "" "GET /health"
test_endpoint GET "/health/loop"    "200" "" "GET /health/loop"
test_endpoint GET "/health/modules" "200" "" "GET /health/modules"
test_endpoint GET "/metrics"        "200" "" "GET /metrics (Prometheus)"

# ---------------------------------------------------------------------------
# Section 2: Budget & Cost
# ---------------------------------------------------------------------------
echo ""
echo "--- Budget & Cost ---"
test_endpoint GET  "/budget/status"      "200" "" "GET /budget/status"
test_endpoint POST "/budget/init"        "200" '{}'                   "POST /budget/init (empty)"
test_endpoint POST "/budget/init"        "200" '{"ceiling":1000}'    "POST /budget/init (ceiling)"
test_endpoint GET  "/cost"               "200" "" "GET /cost"
test_endpoint GET  "/cost/summary"       "200" "" "GET /cost/summary"

# ---------------------------------------------------------------------------
# Section 3: Registry & Control Plane
# ---------------------------------------------------------------------------
echo ""
echo "--- Registry & Control ---"
test_endpoint GET  "/registry"           "200" "" "GET /registry"
test_endpoint GET  "/registry/stats"     "200" "" "GET /registry/stats"
test_endpoint GET  "/control"            "200" "" "GET /control"
test_endpoint GET  "/control/gates"      "200" "" "GET /control/gates"
test_endpoint GET  "/control/audit"      "200" "" "GET /control/audit"
test_endpoint POST "/control/set"        "200" '{"key":"test.integration","value":true}' "POST /control/set"
test_endpoint POST "/control/set"        "400" '{"value":true}'                          "POST /control/set (missing key → 400)"
test_endpoint POST "/control/set"        "400" '{"key":"test.integration"}'              "POST /control/set (missing value → 400)"

# ---------------------------------------------------------------------------
# Section 4: Audit Trail
# ---------------------------------------------------------------------------
echo ""
echo "--- Audit Trail ---"
test_endpoint GET "/audit"               "200" "" "GET /audit"
test_endpoint GET "/audit?limit=5"       "200" "" "GET /audit?limit=5"
test_endpoint GET "/audit/stats"         "200" "" "GET /audit/stats"

# ---------------------------------------------------------------------------
# Section 5: Agents & Profiles
# ---------------------------------------------------------------------------
echo ""
echo "--- Agents & Profiles ---"
test_endpoint GET "/agents"                          "200" "" "GET /agents"
test_endpoint GET "/agents/executor"                 "200" "" "GET /agents/executor"
test_endpoint GET "/agents/nonexistent"              "404" "" "GET /agents/nonexistent → 404"
test_endpoint GET "/agents/profiles"                 "200" "" "GET /agents/profiles"
test_endpoint GET "/agents/profiles/summary"         "200" "" "GET /agents/profiles/summary"
test_endpoint GET "/agents/profiles/executor"        "200" "" "GET /agents/profiles/executor"
test_endpoint GET "/agents/profiles/nonexistent"     "404" "" "GET /agents/profiles/nonexistent → 404"

# ---------------------------------------------------------------------------
# Section 6: RBAC
# ---------------------------------------------------------------------------
echo ""
echo "--- RBAC ---"
test_endpoint GET "/rbac/whoami" "200" "" "GET /rbac/whoami"

# ---------------------------------------------------------------------------
# Section 7: KPI
# ---------------------------------------------------------------------------
echo ""
echo "--- KPI ---"
test_endpoint GET "/kpi"            "200" "" "GET /kpi"
test_endpoint GET "/kpi/velocity"   "200" "" "GET /kpi/velocity"
test_endpoint GET "/kpi/cycle-time" "200" "" "GET /kpi/cycle-time"

# ---------------------------------------------------------------------------
# Section 8: Dependencies
# ---------------------------------------------------------------------------
echo ""
echo "--- Dependency Graph ---"
test_endpoint GET "/deps"              "200" "" "GET /deps"
test_endpoint GET "/deps?format=dot"   "200" "" "GET /deps?format=dot"
test_endpoint GET "/deps?format=ascii" "200" "" "GET /deps?format=ascii"

# ---------------------------------------------------------------------------
# Section 9: Validation
# ---------------------------------------------------------------------------
echo ""
echo "--- Schema Validation ---"
test_endpoint GET "/validate" "200" "" "GET /validate"

# ---------------------------------------------------------------------------
# Section 10: Spawn Queue
# ---------------------------------------------------------------------------
echo ""
echo "--- Spawn Queue ---"
test_endpoint GET  "/spawn-queue"                   "200" "" "GET /spawn-queue"
test_endpoint GET  "/spawn-queue/pending"           "200" "" "GET /spawn-queue/pending"
test_endpoint GET  "/spawn-queue/active"            "200" "" "GET /spawn-queue/active"
test_endpoint POST "/spawn-queue/enqueue"           "200" '{"role":"executor","prompt_context":"test"}' "POST /spawn-queue/enqueue"
test_endpoint POST "/spawn-queue/enqueue"           "400" '{"prompt_context":"test"}'                   "POST /spawn-queue/enqueue (no role → 400)"

# ---------------------------------------------------------------------------
# Section 11: Sessions
# ---------------------------------------------------------------------------
echo ""
echo "--- Sessions ---"
test_endpoint GET  "/sessions"          "200" "" "GET /sessions"
test_endpoint GET  "/sessions/current"  "404" "" "GET /sessions/current (none active → 404)"
test_endpoint GET  "/sessions/compare"  "400" "" "GET /sessions/compare (no params → 400)"
test_endpoint GET  "/sessions/bogus"    "404" "" "GET /sessions/bogus → 404"
test_endpoint POST "/sessions/start"    "200" '{}' "POST /sessions/start"
test_endpoint POST "/sessions/close"    "200" '{}' "POST /sessions/close"

# ---------------------------------------------------------------------------
# Section 12: Replays
# ---------------------------------------------------------------------------
echo ""
echo "--- Replays ---"
test_endpoint GET  "/replays"                   "200" "" "GET /replays"
test_endpoint GET  "/replays/status"            "200" "" "GET /replays/status"
test_endpoint GET  "/replays/bogus"             "404" "" "GET /replays/bogus → 404"
test_endpoint GET  "/replays/bogus/summary"     "404" "" "GET /replays/bogus/summary → 404"
test_endpoint POST "/replays/pause"             "409" '{}' "POST /replays/pause (none active → 409)"
test_endpoint POST "/replays/resume"            "409" '{}' "POST /replays/resume (none active → 409)"
test_endpoint POST "/replays/stop"              "200" '{}' "POST /replays/stop (none active → ok:true)"
test_endpoint POST "/replays/seek"              "409" '{"event_number":0}' "POST /replays/seek (none active → 409)"

# ---------------------------------------------------------------------------
# Section 13: Backup
# ---------------------------------------------------------------------------
echo ""
echo "--- Backup ---"
test_endpoint GET  "/backups"                   "200" "" "GET /backups"
test_endpoint POST "/backup"                    "200" '{}' "POST /backup"
test_endpoint POST "/backup/restore"            "400" '{}' "POST /backup/restore (no filename → 400)"
test_endpoint POST "/backup/restore"            "404" '{"filename":"no-such-file.tar.gz"}' "POST /backup/restore (bad filename → 404)"

# ---------------------------------------------------------------------------
# Section 14: Notifications
# ---------------------------------------------------------------------------
echo ""
echo "--- Notifications ---"
test_endpoint GET  "/notifications/history" "200" "" "GET /notifications/history"
test_endpoint POST "/notifications/test"    "200" '{}' "POST /notifications/test"

# ---------------------------------------------------------------------------
# Section 15: Plugins
# ---------------------------------------------------------------------------
echo ""
echo "--- Plugins ---"
test_endpoint GET "/plugins"           "200" "" "GET /plugins"
test_endpoint GET "/plugins/bogus"     "404" "" "GET /plugins/bogus → 404"

# ---------------------------------------------------------------------------
# Section 16: Quality Scores
# ---------------------------------------------------------------------------
echo ""
echo "--- Quality Scores ---"
test_endpoint GET "/quality"             "200" "" "GET /quality"
test_endpoint GET "/quality/stats"       "200" "" "GET /quality/stats"
test_endpoint GET "/quality/99999"       "404" "" "GET /quality/99999 → 404"
test_endpoint GET "/quality/notanumber" "400" "" "GET /quality/notanumber → 400"

# ---------------------------------------------------------------------------
# Section 17: Agent Memory
# ---------------------------------------------------------------------------
echo ""
echo "--- Agent Memory ---"
test_endpoint GET "/memory/lessons"              "200" "" "GET /memory/lessons"
test_endpoint GET "/memory/stats"                "200" "" "GET /memory/stats"
test_endpoint GET "/memory/context"              "400" "" "GET /memory/context (no files → 400)"
test_endpoint GET "/memory/context?files=api.py" "200" "" "GET /memory/context?files=api.py"

# ---------------------------------------------------------------------------
# Section 18: Benchmarks
# ---------------------------------------------------------------------------
echo ""
echo "--- Benchmarks ---"
test_endpoint GET "/benchmarks"         "200" "" "GET /benchmarks"
test_endpoint GET "/benchmarks/history" "200" "" "GET /benchmarks/history"
test_endpoint GET "/benchmarks/http"    "200" "" "GET /benchmarks/http"

# ---------------------------------------------------------------------------
# Section 19: Distributed Traces
# ---------------------------------------------------------------------------
echo ""
echo "--- Distributed Traces ---"
test_endpoint GET "/traces"           "200" "" "GET /traces"
test_endpoint GET "/traces/stats"     "200" "" "GET /traces/stats"
test_endpoint GET "/traces/bogus"     "404" "" "GET /traces/bogus → 404"

# ---------------------------------------------------------------------------
# Section 20: GraphQL
# ---------------------------------------------------------------------------
echo ""
echo "--- GraphQL ---"
test_endpoint GET  "/graphql?query={__typename}" "200" ""                       "GET /graphql?query={__typename}"
test_endpoint GET  "/graphql"                    "400" ""                       "GET /graphql (no query → 400)"
test_endpoint POST "/graphql"                    "200" '{"query":"{__typename}"}' "POST /graphql"
test_endpoint POST "/graphql"                    "400" '{}'                       "POST /graphql (no query → 400)"

# ---------------------------------------------------------------------------
# Section 21: Dashboard & Docs
# ---------------------------------------------------------------------------
echo ""
echo "--- Dashboard & Docs ---"
test_endpoint GET "/dashboard"    "200" "" "GET /dashboard"
test_endpoint GET "/openapi.json" "200" "" "GET /openapi.json"
test_endpoint GET "/docs"         "200" "" "GET /docs"

# ---------------------------------------------------------------------------
# Section 22: API Versioning
# ---------------------------------------------------------------------------
echo ""
echo "--- API Versioning ---"
test_endpoint GET "/v1/health"        "200" "" "GET /v1/health (valid version)"
test_endpoint GET "/v1/budget/status" "200" "" "GET /v1/budget/status"
test_endpoint GET "/v2/health"        "400" "" "GET /v2/health (unsupported → 400)"
test_endpoint GET "/v2/budget/status" "400" "" "GET /v2/budget/status (unsupported → 400)"

# ---------------------------------------------------------------------------
# Section 23: Error cases
# ---------------------------------------------------------------------------
echo ""
echo "--- Error Cases ---"
test_endpoint GET  "/nonexistent"    "404" "" "GET /nonexistent → 404"
test_endpoint POST "/nonexistent"    "404" '{}' "POST /nonexistent → 404"

# ---------------------------------------------------------------------------
# Section 24: SSE Streaming
# ---------------------------------------------------------------------------
echo ""
echo "--- SSE Streaming ---"
test_sse "/stream/feed"   "GET /stream/feed"
test_sse "/stream/status" "GET /stream/status"
test_sse "/stream/events" "GET /stream/events"

# ---------------------------------------------------------------------------
# Section 25: WebSocket
# ---------------------------------------------------------------------------
echo ""
echo "--- WebSocket ---"
# /ws without upgrade headers must return 400
test_endpoint GET "/ws" "400" "" "GET /ws (no upgrade headers → 400)"
# /ws with proper WebSocket upgrade
test_ws "/ws" "GET /ws (WebSocket handshake)"

# ---------------------------------------------------------------------------
# Auth tests — only run if an auth key was provided
# ---------------------------------------------------------------------------
if [[ -n "$AUTH_KEY" ]]; then
  echo ""
  echo "--- Auth (AF_API_AUTH_KEY active) ---"
  # Temporarily test without the key
  SAVED_KEY="$AUTH_KEY"
  AUTH_KEY=""
  test_endpoint GET "/budget/status" "401" "" "GET /budget/status (no token → 401)"
  AUTH_KEY="wrongkey"
  test_endpoint GET "/budget/status" "403" "" "GET /budget/status (bad token → 403)"
  AUTH_KEY="$SAVED_KEY"
  test_endpoint GET "/budget/status" "200" "" "GET /budget/status (valid token → 200)"
  # Health/metrics always exempt
  AUTH_KEY=""
  test_endpoint GET "/health"  "200" "" "GET /health (always exempt from auth)"
  test_endpoint GET "/metrics" "200" "" "GET /metrics (always exempt from auth)"
  AUTH_KEY="$SAVED_KEY"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "================================================================"
TOTAL=$((PASS_COUNT + FAIL_COUNT))
echo " Results: ${PASS_COUNT}/${TOTAL} passed"
if [[ $FAIL_COUNT -gt 0 ]]; then
  echo ""
  echo " FAILURES:"
  for f in "${FAILURES[@]}"; do
    echo "   - $f"
  done
  echo ""
  echo "================================================================"
  exit 1
else
  echo " All tests passed."
  echo "================================================================"
  exit 0
fi
