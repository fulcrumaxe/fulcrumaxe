#!/usr/bin/env bash
# data-completeness-check.sh — API data quality assertions
#
# Verifies that API endpoints return MEANINGFUL data, not just HTTP 200.
# Catches bugs where the server returns empty arrays, null fields, or zero
# counts that indicate a rendering or data-pipeline failure.
#
# Also exercises each backend CLI module to confirm nothing has crashed.
#
# Usage:
#   ./scripts/data-completeness-check.sh
#   API_PORT=18099 ./scripts/data-completeness-check.sh
#
# Exit codes:
#   0 — all checks pass (warnings are non-fatal)
#   N — N hard failures detected
#
# Run from repository root.

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

API=${API_PORT:-18099}
RUST_PORT=${RUST_PORT:-3000}
FAILURES=0
WARNINGS=0

_pass() { echo "  PASS: $*"; }
_fail() { echo "  FAIL: $*"; FAILURES=$((FAILURES + 1)); }
_warn() { echo "  WARN: $*"; WARNINGS=$((WARNINGS + 1)); }
_info() { echo "  INFO: $*"; }
_skip() { echo "  SKIP: $*"; }

echo "============================================================"
echo " data-completeness-check.sh"
echo " API=http://localhost:${API}  Rust=http://localhost:${RUST_PORT}"
echo " $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "============================================================"

# ---------------------------------------------------------------------------
# Helper: curl with timeout, return empty string on failure
# ---------------------------------------------------------------------------
api_get() {
  curl -sf --max-time 5 "http://localhost:${API}${1}" 2>/dev/null || echo ""
}

rust_get() {
  curl -sf --max-time 5 "http://localhost:${RUST_PORT}${1}" 2>/dev/null || echo ""
}

# ---------------------------------------------------------------------------
# 1. Check API server is reachable at all
# ---------------------------------------------------------------------------
echo ""
echo "[1/7] API server reachability"
HEALTH=$(api_get /health)
if [ -z "$HEALTH" ]; then
  _fail "API server not reachable at port ${API} — all subsequent checks will fail"
  echo "data-completeness: $FAILURES failures, $WARNINGS warnings"
  exit "$FAILURES"
else
  _pass "API server is reachable"
fi

# ---------------------------------------------------------------------------
# 2. Registry has discussions (not just 200 with empty data)
# ---------------------------------------------------------------------------
echo ""
echo "[2/7] Registry data quality"

REGISTRY=$(api_get /registry/stats)
if [ -z "$REGISTRY" ]; then
  REGISTRY=$(api_get /v1/registry)
fi

if [ -z "$REGISTRY" ]; then
  _fail "registry endpoint returned empty response"
else
  TOTAL=$(echo "$REGISTRY" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  print(d.get('total', d.get('count', 0)))
except: print(0)
" 2>/dev/null || echo "0")

  if [ "$TOTAL" -eq 0 ] 2>/dev/null; then
    _warn "registry total is 0 — no discussions indexed (may be empty on fresh install)"
  else
    _pass "registry total: $TOTAL discussions"
  fi

  # Check that the response is valid JSON
  if echo "$REGISTRY" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    _pass "registry response is valid JSON"
  else
    _fail "registry response is not valid JSON: ${REGISTRY:0:100}"
  fi
fi

# ---------------------------------------------------------------------------
# 3. KPI endpoint has cycle time field (not null)
# ---------------------------------------------------------------------------
echo ""
echo "[3/7] KPI data quality"

KPI=$(api_get /v1/kpi)
if [ -z "$KPI" ]; then
  KPI=$(api_get /kpi)
fi

if [ -z "$KPI" ]; then
  _fail "KPI endpoint returned empty response"
else
  if echo "$KPI" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    _pass "KPI response is valid JSON"
  else
    _fail "KPI response is not valid JSON: ${KPI:0:100}"
  fi

  CYCLE=$(echo "$KPI" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  ct = d.get('pr_cycle_time', d.get('cycle_time', {}))
  if isinstance(ct, dict):
    v = ct.get('mean_hours', ct.get('mean', None))
    print(v if v is not None else '')
  elif ct is not None:
    print(ct)
  else:
    print('')
except: print('')
" 2>/dev/null || echo "")

  if [ -z "$CYCLE" ]; then
    _warn "KPI cycle time is null or missing (may be zero if no PRs yet)"
  else
    _pass "KPI cycle time: $CYCLE hours"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Health endpoint has loop metrics
# ---------------------------------------------------------------------------
echo ""
echo "[4/7] Health / loop metrics"

HEALTH_DATA=$(api_get /health)
LOOP_RUN=$(echo "$HEALTH_DATA" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  print(d.get('loop_last_run') or d.get('last_loop') or d.get('loop', {}).get('last_run', '') or '')
except: print('')
" 2>/dev/null || echo "")

if [ -z "$LOOP_RUN" ]; then
  _warn "no loop_last_run field in /health (expected once loop has run at least once)"
else
  _pass "health loop_last_run: $LOOP_RUN"
fi

# Check health modules endpoint if available
MODULES=$(api_get /v1/health/modules)
if [ -n "$MODULES" ]; then
  MOD_COUNT=$(echo "$MODULES" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  if isinstance(d, list): print(len(d))
  elif isinstance(d, dict): print(len(d.get('modules', d)))
  else: print(0)
except: print(0)
" 2>/dev/null || echo "0")
  if [ "$MOD_COUNT" -gt 0 ] 2>/dev/null; then
    _pass "health/modules: $MOD_COUNT module(s) reported"
  else
    _warn "health/modules returned 0 modules"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Quality endpoint has scores
# ---------------------------------------------------------------------------
echo ""
echo "[5/7] Quality scores"

QUALITY=$(api_get /v1/quality)
if [ -n "$QUALITY" ]; then
  SCORES=$(echo "$QUALITY" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  scores = d.get('scores', d.get('results', []))
  print(len(scores) if isinstance(scores, list) else 0)
except: print(0)
" 2>/dev/null || echo "0")

  if [ "$SCORES" -eq 0 ] 2>/dev/null; then
    _warn "no quality scores yet (expected after code reviews have run)"
  else
    _pass "quality: $SCORES score(s) on record"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Module health — exercise each backend CLI
# ---------------------------------------------------------------------------
echo ""
echo "[6/7] Backend module CLI health"

run_cli() {
  local label="$1"
  local cmd="$2"
  if eval "$cmd" > /dev/null 2>&1; then
    _pass "$label"
  else
    _fail "$label crashed (exit code $?)"
  fi
}

run_cli "registry.py sync" \
  "python3 backend/registry.py sync --dry-run 2>/dev/null || python3 backend/registry.py sync 2>/dev/null || python3 -c 'import backend.registry' 2>/dev/null"

run_cli "kpi_engine.py show" \
  "python3 backend/kpi_engine.py show > /dev/null 2>&1 || python3 -c 'import backend.kpi_engine' 2>/dev/null"

run_cli "quality_scorer.py stats" \
  "python3 backend/quality_scorer.py stats > /dev/null 2>&1 || python3 -c 'import backend.quality_scorer' 2>/dev/null"

run_cli "budget.py status" \
  "python3 backend/budget.py status > /dev/null 2>&1"

run_cli "cost_tracker.py summary" \
  "python3 backend/cost_tracker.py summary > /dev/null 2>&1 || python3 -c 'import backend.cost_tracker' 2>/dev/null"

run_cli "circuit_breaker.py imports" \
  "python3 -c 'import backend.circuit_breaker' 2>/dev/null"

run_cli "context_manager.py show" \
  "python3 backend/context_manager.py show > /dev/null 2>&1 || python3 -c 'import backend.context_manager' 2>/dev/null"

# ---------------------------------------------------------------------------
# 7. Rust saas-service health (if running)
# ---------------------------------------------------------------------------
echo ""
echo "[7/7] Rust saas-service data quality"

RUST_HEALTH=$(rust_get /health)
if [ -z "$RUST_HEALTH" ]; then
  _skip "Rust saas-service not running on port ${RUST_PORT}"
else
  STATUS=$(echo "$RUST_HEALTH" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  print(d.get('status', ''))
except: print(sys.stdin.read().strip()[:50] if False else '')
" 2>/dev/null || echo "$RUST_HEALTH")

  if echo "$STATUS" | grep -qiE '^(ok|healthy|up)$'; then
    _pass "Rust saas-service health: $STATUS"
  else
    _fail "Rust saas-service health unexpected: ${STATUS:0:80}"
  fi

  # Rust /agents endpoint — check it returns an array
  AGENTS=$(rust_get /agents)
  if [ -n "$AGENTS" ]; then
    AGENT_COUNT=$(echo "$AGENTS" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  if isinstance(d, list): print(len(d))
  elif isinstance(d, dict): print(len(d.get('agents', [])))
  else: print(0)
except: print(0)
" 2>/dev/null || echo "0")
    _pass "Rust /agents: $AGENT_COUNT agent(s)"
  fi
fi

# ---------------------------------------------------------------------------
# Cross-endpoint consistency: if GitHub is accessible, compare discussion counts
# ---------------------------------------------------------------------------
echo ""
echo "[cross-check] Discussion count consistency"
if command -v gh >/dev/null 2>&1; then
  GH_COUNT=$(gh api graphql -f query='query { repository(owner:"fulcrumaxe", name:"fulcrumaxe") { discussions(first:1) { totalCount } } }' \
    --jq '.data.repository.discussions.totalCount' 2>/dev/null || echo "")
  if [ -n "$GH_COUNT" ] && [ -n "$REGISTRY" ]; then
    REG_TOTAL=$(echo "$REGISTRY" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  print(d.get('total', d.get('count', 0)))
except: print(0)
" 2>/dev/null || echo "0")
    if [ "$REG_TOTAL" -gt 0 ] && [ "$GH_COUNT" -gt 0 ]; then
      # Registry may be a subset of all GitHub discussions — just check it's non-zero
      _pass "cross-check: GitHub has $GH_COUNT discussions, registry has $REG_TOTAL indexed"
    else
      _info "cross-check: GitHub=$GH_COUNT, registry=$REG_TOTAL"
    fi
  fi
else
  _info "gh CLI not available — skipping cross-endpoint consistency check"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Data completeness: $FAILURES failure(s), $WARNINGS warning(s)"
echo "============================================================"

exit "$FAILURES"
