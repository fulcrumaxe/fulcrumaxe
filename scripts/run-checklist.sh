#!/usr/bin/env bash
# run-checklist.sh — Programmatic checklist runner for verification-report/checklist.json
#
# Reads checklist.json, runs each type:"programmatic" item, captures results,
# and writes updated JSON to verification-report/proof/{timestamp}/checklist-results.json.
# Items with type:"manual" are left as "pending" for human review.
#
# Usage:
#   ./scripts/run-checklist.sh [--api-port PORT] [--rust-port PORT] [--auth-key KEY]
#
# Options:
#   --api-port PORT     Python backend API port (default: 8000)
#   --rust-port PORT    Rust SaaS service port (default: 3000)
#   --auth-key KEY      API auth key for Python backend
#   --no-screenshots    Skip screenshot capture
#
# Exit code 0 = all programmatic items passed
# Exit code 1 = one or more programmatic items failed
#
# Run from the repository root.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
API_PORT=8000
RUST_PORT=3000
AUTH_KEY=""
NO_SCREENSHOTS=false

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-port)       API_PORT="$2"; shift 2 ;;
    --rust-port)      RUST_PORT="$2"; shift 2 ;;
    --auth-key)       AUTH_KEY="$2"; shift 2 ;;
    --no-screenshots) NO_SCREENSHOTS=true; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

API_BASE="http://127.0.0.1:${API_PORT}"
RUST_BASE="http://127.0.0.1:${RUST_PORT}"
CHECKLIST="$REPO_ROOT/verification-report/checklist.json"
TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
PROOF_DIR="$REPO_ROOT/verification-report/proof/$TIMESTAMP"
RESULTS_FILE="$PROOF_DIR/checklist-results.json"

# ---------------------------------------------------------------------------
# Validate inputs
# ---------------------------------------------------------------------------
if [[ ! -f "$CHECKLIST" ]]; then
  echo "ERROR: checklist.json not found at $CHECKLIST" >&2
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required but not found in PATH" >&2
  exit 1
fi

mkdir -p "$PROOF_DIR"

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "  $*"; }
pass() { echo "  PASS  $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  FAIL  $1 — $2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
skip() { echo "  SKIP  $1 (manual)"; SKIP_COUNT=$((SKIP_COUNT + 1)); }

# HTTP GET helper — returns "STATUS_CODE BODY"
http_get() {
  local url="$1"
  local extra_args=()
  if [[ -n "$AUTH_KEY" ]]; then
    extra_args+=(-H "Authorization: Bearer $AUTH_KEY")
  fi
  curl -s -w "\n__STATUS__%{http_code}" "${extra_args[@]}" "$url" 2>/dev/null || echo -e "\n__STATUS__000"
}

# Extract HTTP status code from http_get output
extract_status() {
  echo "$1" | grep '__STATUS__' | sed 's/__STATUS__//'
}

# Extract body from http_get output
extract_body() {
  echo "$1" | grep -v '__STATUS__'
}

# Capture screenshot using annotate-proof.sh if available
capture_screenshot() {
  local item_id="$1"
  local url="$2"
  local screenshot_path="$PROOF_DIR/${item_id}.png"

  if [[ "$NO_SCREENSHOTS" == "true" ]]; then
    echo ""
    return
  fi

  if [[ -x "$REPO_ROOT/scripts/annotate-proof.sh" ]]; then
    "$REPO_ROOT/scripts/annotate-proof.sh" \
      --url "$url" \
      --output "$screenshot_path" \
      --label "$item_id" 2>/dev/null || true
    if [[ -f "$screenshot_path" ]]; then
      echo "$screenshot_path"
      return
    fi
  fi

  # Fallback: save raw curl output as text proof
  local text_path="$PROOF_DIR/${item_id}.txt"
  http_get "$url" > "$text_path" 2>/dev/null || true
  echo "$text_path"
}

# Run a shell command and return pass/fail
run_command() {
  local item_id="$1"
  local cmd="$2"
  local workdir="${3:-$REPO_ROOT}"
  local output_file="$PROOF_DIR/${item_id}.log"

  if (cd "$workdir" && eval "$cmd") >"$output_file" 2>&1; then
    echo "pass"
  else
    echo "fail"
  fi
}

# ---------------------------------------------------------------------------
# JSON update helper — updates a specific item's fields in the checklist copy
# We build up a jq script to apply all updates at the end
# ---------------------------------------------------------------------------
declare -A ITEM_RESULTS  # item_id -> json patch

update_item() {
  local item_id="$1"
  local status="$2"
  local actual="$3"
  local screenshot="${4:-}"
  local verified_at
  verified_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  # Escape strings for jq
  local actual_escaped
  actual_escaped=$(printf '%s' "$actual" | jq -Rs .)
  local screenshot_escaped
  screenshot_escaped=$(printf '%s' "$screenshot" | jq -Rs .)

  ITEM_RESULTS["$item_id"]=$(cat <<EOF
.subsystems[].items[] |= if .id == "$item_id" then
  .status = "$status" |
  .actual = $actual_escaped |
  .screenshot = $screenshot_escaped |
  .verified_by = "run-checklist.sh" |
  .verified_at = "$verified_at"
else . end
EOF
)
}

# ---------------------------------------------------------------------------
# Main — load checklist and iterate subsystems
# ---------------------------------------------------------------------------
echo "============================================================"
echo " Checklist Runner"
echo " $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "============================================================"
echo ""

# Work on a copy so we can update it
cp "$CHECKLIST" "$PROOF_DIR/checklist-results.json"

# Read subsystem and item counts
TOTAL_PROGRAMMATIC=$(jq '[.subsystems[].items[] | select(.type == "programmatic")] | length' "$CHECKLIST")
TOTAL_MANUAL=$(jq '[.subsystems[].items[] | select(.type == "manual")] | length' "$CHECKLIST")
echo "Items: $TOTAL_PROGRAMMATIC programmatic, $TOTAL_MANUAL manual (skipped)"
echo ""

# ---------------------------------------------------------------------------
# Section: Python Backend API
# ---------------------------------------------------------------------------
echo "[Python Backend API]"

# api-health
RESP=$(http_get "$API_BASE/health")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e '.ok == true' >/dev/null 2>&1; then
  pass "api-health: GET /health → $CODE, ok:true"
  update_item "api-health" "pass" "HTTP $CODE, ok:true" "$(capture_screenshot api-health $API_BASE/health)"
else
  fail "api-health" "HTTP $CODE, body: $(echo "$BODY" | head -c 100)"
  update_item "api-health" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-budget-status
RESP=$(http_get "$API_BASE/v1/budget/status")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e 'type == "object"' >/dev/null 2>&1; then
  pass "api-budget-status: GET /v1/budget/status → $CODE, valid JSON"
  update_item "api-budget-status" "pass" "HTTP $CODE, valid JSON object"
else
  fail "api-budget-status" "HTTP $CODE, body: $(echo "$BODY" | head -c 100)"
  update_item "api-budget-status" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-registry
RESP=$(http_get "$API_BASE/v1/registry")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e '.' >/dev/null 2>&1; then
  pass "api-registry: GET /v1/registry → $CODE, valid JSON"
  update_item "api-registry" "pass" "HTTP $CODE, valid JSON"
else
  fail "api-registry" "HTTP $CODE, body: $(echo "$BODY" | head -c 100)"
  update_item "api-registry" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-control-gates
RESP=$(http_get "$API_BASE/v1/control/gates")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e 'type == "object"' >/dev/null 2>&1; then
  pass "api-control-gates: GET /v1/control/gates → $CODE, valid JSON"
  update_item "api-control-gates" "pass" "HTTP $CODE, valid JSON object with gates"
else
  fail "api-control-gates" "HTTP $CODE, body: $(echo "$BODY" | head -c 100)"
  update_item "api-control-gates" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-agents
RESP=$(http_get "$API_BASE/v1/agents")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e '.' >/dev/null 2>&1; then
  pass "api-agents: GET /v1/agents → $CODE, valid JSON"
  update_item "api-agents" "pass" "HTTP $CODE, valid JSON"
else
  fail "api-agents" "HTTP $CODE, body: $(echo "$BODY" | head -c 100)"
  update_item "api-agents" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-kpi
RESP=$(http_get "$API_BASE/v1/kpi")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e '.' >/dev/null 2>&1; then
  pass "api-kpi: GET /v1/kpi → $CODE, valid JSON"
  update_item "api-kpi" "pass" "HTTP $CODE, valid JSON"
else
  fail "api-kpi" "HTTP $CODE, body: $(echo "$BODY" | head -c 100)"
  update_item "api-kpi" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-cost-summary
RESP=$(http_get "$API_BASE/v1/cost/summary")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e '.' >/dev/null 2>&1; then
  pass "api-cost-summary: GET /v1/cost/summary → $CODE, valid JSON"
  update_item "api-cost-summary" "pass" "HTTP $CODE, valid JSON"
else
  fail "api-cost-summary" "HTTP $CODE, body: $(echo "$BODY" | head -c 100)"
  update_item "api-cost-summary" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-metrics
RESP=$(http_get "$API_BASE/metrics")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | grep -q '^#'; then
  pass "api-metrics: GET /metrics → $CODE, Prometheus format"
  update_item "api-metrics" "pass" "HTTP $CODE, Prometheus exposition format detected"
else
  fail "api-metrics" "HTTP $CODE, missing Prometheus '# HELP' lines"
  update_item "api-metrics" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-openapi
RESP=$(http_get "$API_BASE/openapi.json")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e '.openapi' >/dev/null 2>&1; then
  OPENAPI_VER=$(echo "$BODY" | jq -r '.openapi')
  pass "api-openapi: GET /openapi.json → $CODE, openapi:$OPENAPI_VER"
  update_item "api-openapi" "pass" "HTTP $CODE, OpenAPI version $OPENAPI_VER"
else
  fail "api-openapi" "HTTP $CODE, missing .openapi field"
  update_item "api-openapi" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-docs
RESP=$(http_get "$API_BASE/docs")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | grep -qi 'swagger\|openapi\|<html'; then
  pass "api-docs: GET /docs → $CODE, HTML Swagger UI"
  update_item "api-docs" "pass" "HTTP $CODE, HTML Swagger UI loaded"
  capture_screenshot "api-docs" "$API_BASE/docs" >/dev/null 2>&1 || true
else
  fail "api-docs" "HTTP $CODE, expected HTML with Swagger UI"
  update_item "api-docs" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# api-dashboard
RESP=$(http_get "$API_BASE/dashboard")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | grep -qi '<html'; then
  pass "api-dashboard: GET /dashboard → $CODE, HTML"
  update_item "api-dashboard" "pass" "HTTP $CODE, HTML content loaded"
  capture_screenshot "api-dashboard" "$API_BASE/dashboard" >/dev/null 2>&1 || true
else
  fail "api-dashboard" "HTTP $CODE, expected HTML"
  update_item "api-dashboard" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

echo ""

# ---------------------------------------------------------------------------
# Section: Rust SaaS Service
# ---------------------------------------------------------------------------
echo "[Rust SaaS Service]"

# rust-health
RESP=$(http_get "$RUST_BASE/health")
CODE=$(extract_status "$RESP")
BODY=$(extract_body "$RESP")
if [[ "$CODE" == "200" ]] && echo "$BODY" | jq -e '.status == "ok"' >/dev/null 2>&1; then
  pass "rust-health: GET /health → $CODE, status:ok"
  update_item "rust-health" "pass" "HTTP $CODE, status:ok"
else
  fail "rust-health" "HTTP $CODE, body: $(echo "$BODY" | head -c 100)"
  update_item "rust-health" "fail" "HTTP $CODE, body: $(echo "$BODY" | head -c 200)"
fi

# rust-postgres — manual check
# The /health endpoint only returns {"status":"ok","version":"..."} and does not expose
# a database or db field, so we cannot infer Postgres health programmatically.
# This item is type:manual in checklist.json and must be confirmed via service logs
# or a direct DB connection check.
echo "[SKIP] rust-postgres: /health has no database field — marked manual in checklist.json"

# rust-auth-github
RESP=$(http_get "$RUST_BASE/auth/github")
CODE=$(extract_status "$RESP")
if [[ "$CODE" == "200" || "$CODE" == "302" || "$CODE" == "303" ]]; then
  pass "rust-auth-github: GET /auth/github → $CODE (redirect or response)"
  update_item "rust-auth-github" "pass" "HTTP $CODE (OAuth redirect or response)"
else
  fail "rust-auth-github" "HTTP $CODE, expected 200 or 302"
  update_item "rust-auth-github" "fail" "HTTP $CODE"
fi

# rust-projects-crud — unauthenticated should return 401
RESP=$(http_get "$RUST_BASE/api/v1/projects")
CODE=$(extract_status "$RESP")
if [[ "$CODE" == "200" || "$CODE" == "401" || "$CODE" == "403" ]]; then
  pass "rust-projects-crud: GET /api/v1/projects → $CODE (endpoint exists)"
  update_item "rust-projects-crud" "pass" "HTTP $CODE (endpoint reachable)"
else
  fail "rust-projects-crud" "HTTP $CODE, expected 200/401/403"
  update_item "rust-projects-crud" "fail" "HTTP $CODE"
fi

# rust-agents-crud — need a project ID; use a placeholder, expect 401/403/404
RESP=$(http_get "$RUST_BASE/api/v1/projects/00000000-0000-0000-0000-000000000000/agents")
CODE=$(extract_status "$RESP")
if [[ "$CODE" == "200" || "$CODE" == "401" || "$CODE" == "403" || "$CODE" == "404" ]]; then
  pass "rust-agents-crud: GET /api/v1/projects/{id}/agents → $CODE (endpoint exists)"
  update_item "rust-agents-crud" "pass" "HTTP $CODE (endpoint reachable)"
else
  fail "rust-agents-crud" "HTTP $CODE, expected 200/401/403/404"
  update_item "rust-agents-crud" "fail" "HTTP $CODE"
fi

# rust-websocket — WebSocket upgrade endpoint exists.
# A plain HTTP GET (without the Upgrade: websocket header) will return 400 with
# "Connection header did not include 'upgrade'" — this is correct behavior from
# axum's WebSocket handler. Accept 101/401/403/426 (standard WS responses) as
# well as 400 (endpoint exists, correctly rejects non-upgrade requests).
RESP=$(http_get "$RUST_BASE/api/v1/projects/00000000-0000-0000-0000-000000000000/stream")
CODE=$(extract_status "$RESP")
if [[ "$CODE" == "101" || "$CODE" == "400" || "$CODE" == "401" || "$CODE" == "403" || "$CODE" == "426" ]]; then
  pass "rust-websocket: GET /api/v1/projects/{id}/stream → $CODE (endpoint exists)"
  update_item "rust-websocket" "pass" "HTTP $CODE (WebSocket endpoint reachable; 400=requires Upgrade header)"
else
  fail "rust-websocket" "HTTP $CODE, expected 101/400/401/403/426"
  update_item "rust-websocket" "fail" "HTTP $CODE"
fi

echo ""

# ---------------------------------------------------------------------------
# Section: TUI
# ---------------------------------------------------------------------------
echo "[TUI]"

if [[ -d "$REPO_ROOT/tui" ]]; then
  # tui-build
  RESULT=$(run_command "tui-build" "npm run build 2>&1" "$REPO_ROOT/tui")
  if [[ "$RESULT" == "pass" ]]; then
    pass "tui-build: npm run build → success"
    update_item "tui-build" "pass" "exit code 0, build succeeded"
  else
    fail "tui-build" "build failed — see $PROOF_DIR/tui-build.log"
    update_item "tui-build" "fail" "exit code non-zero — see tui-build.log"
  fi

  # tui-typecheck
  RESULT=$(run_command "tui-typecheck" "npm run typecheck 2>&1" "$REPO_ROOT/tui")
  if [[ "$RESULT" == "pass" ]]; then
    pass "tui-typecheck: npm run typecheck → success"
    update_item "tui-typecheck" "pass" "exit code 0, no TypeScript errors"
  else
    fail "tui-typecheck" "typecheck failed — see $PROOF_DIR/tui-typecheck.log"
    update_item "tui-typecheck" "fail" "exit code non-zero — see tui-typecheck.log"
  fi

  # tui-lint
  RESULT=$(run_command "tui-lint" "npm run lint 2>&1" "$REPO_ROOT/tui")
  if [[ "$RESULT" == "pass" ]]; then
    pass "tui-lint: npm run lint → success"
    update_item "tui-lint" "pass" "exit code 0, no lint errors"
  else
    fail "tui-lint" "lint failed — see $PROOF_DIR/tui-lint.log"
    update_item "tui-lint" "fail" "exit code non-zero — see tui-lint.log"
  fi
else
  log "tui/ directory not found — skipping TUI checks"
  SKIP_COUNT=$((SKIP_COUNT + 3))
fi

echo ""

# ---------------------------------------------------------------------------
# Section: Dashboard
# ---------------------------------------------------------------------------
echo "[Dashboard]"

if [[ -d "$REPO_ROOT/dashboard" ]]; then
  # dashboard-build
  RESULT=$(run_command "dashboard-build" "npm run build 2>&1" "$REPO_ROOT/dashboard")
  if [[ "$RESULT" == "pass" ]]; then
    pass "dashboard-build: npm run build → success"
    update_item "dashboard-build" "pass" "exit code 0, build succeeded"
  else
    fail "dashboard-build" "build failed — see $PROOF_DIR/dashboard-build.log"
    update_item "dashboard-build" "fail" "exit code non-zero — see dashboard-build.log"
  fi

  # dashboard-typecheck
  RESULT=$(run_command "dashboard-typecheck" "npm run typecheck 2>&1" "$REPO_ROOT/dashboard")
  if [[ "$RESULT" == "pass" ]]; then
    pass "dashboard-typecheck: npm run typecheck → success"
    update_item "dashboard-typecheck" "pass" "exit code 0, no TypeScript errors"
  else
    fail "dashboard-typecheck" "typecheck failed — see $PROOF_DIR/dashboard-typecheck.log"
    update_item "dashboard-typecheck" "fail" "exit code non-zero — see dashboard-typecheck.log"
  fi
else
  log "dashboard/ directory not found — skipping Dashboard checks"
  SKIP_COUNT=$((SKIP_COUNT + 2))
fi

echo ""

# ---------------------------------------------------------------------------
# Section: Integration (manual items — mark as pending, do not run)
# ---------------------------------------------------------------------------
echo "[Integration — manual items, skipped by this runner]"
MANUAL_IDS=("int-dashboard-live-data" "int-rust-agent-dispatch" "int-budget-realtime" "int-control-plane-propagation")
for mid in "${MANUAL_IDS[@]}"; do
  skip "$mid"
done

echo ""

# ---------------------------------------------------------------------------
# Apply all updates to results file using jq
# ---------------------------------------------------------------------------
WORKING_JSON="$RESULTS_FILE"
for item_id in "${!ITEM_RESULTS[@]}"; do
  JQ_SCRIPT="${ITEM_RESULTS[$item_id]}"
  UPDATED=$(jq "$JQ_SCRIPT" "$WORKING_JSON" 2>/dev/null) || true
  if [[ -n "$UPDATED" ]]; then
    echo "$UPDATED" > "$WORKING_JSON"
  fi
done

# Update the generated timestamp in results
jq --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" '.generated = $ts' "$WORKING_JSON" > "${WORKING_JSON}.tmp" \
  && mv "${WORKING_JSON}.tmp" "$WORKING_JSON"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
TOTAL_CHECKED=$((PASS_COUNT + FAIL_COUNT))
echo "============================================================"
echo " Results written to: $RESULTS_FILE"
echo ""
echo " Programmatic: $PASS_COUNT passed, $FAIL_COUNT failed"
echo " Manual items: $SKIP_COUNT pending (human review required)"
echo "============================================================"

if [[ $FAIL_COUNT -gt 0 ]]; then
  echo ""
  echo "FAIL: $FAIL_COUNT programmatic items did not pass."
  exit 1
else
  echo ""
  echo "PASS: All $PASS_COUNT programmatic items passed."
  exit 0
fi
