#!/usr/bin/env bash
# e2e-verify.sh — Master E2E verification orchestrator
#
# Starts all services (Python backend API, Rust saas-service via docker compose,
# dashboard dev/static server), runs all verification phases in order, captures
# visual proof, and tears down on exit.
#
# Usage:
#   ./scripts/e2e-verify.sh [--skip-docker] [--skip-dashboard] [--no-teardown]
#
# Exit codes:
#   0 — gate passed (all checks green, no critical/high bugs)
#   1 — gate failed
#
# Run from the repository root.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
PROOF_DIR="$REPO_ROOT/verification-report/proof/$TIMESTAMP"
SKIP_DOCKER=false
SKIP_DASHBOARD=false
NO_TEARDOWN=false

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-docker)    SKIP_DOCKER=true; shift ;;
    --skip-dashboard) SKIP_DASHBOARD=true; shift ;;
    --no-teardown)    NO_TEARDOWN=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
_log()  { echo "[$(date +%H:%M:%S)] $*"; }
_pass() { echo "  PASS: $*"; }
_fail() { echo "  FAIL: $*"; }
_info() { echo "  INFO: $*"; }

# ---------------------------------------------------------------------------
# Service PIDs for cleanup
# ---------------------------------------------------------------------------
API_PID=""
DASHBOARD_PID=""

cleanup() {
  _log "Tearing down services..."
  [[ -n "$API_PID" ]] && kill "$API_PID" 2>/dev/null || true
  [[ -n "$DASHBOARD_PID" ]] && kill "$DASHBOARD_PID" 2>/dev/null || true
  if [[ "$SKIP_DOCKER" == "false" ]]; then
    docker compose -f "$REPO_ROOT/saas-service/docker-compose.yml" down 2>/dev/null || true
  fi
  _log "Teardown complete."
}

if [[ "$NO_TEARDOWN" == "false" ]]; then
  trap cleanup EXIT
fi

# ---------------------------------------------------------------------------
# Create proof directory
# ---------------------------------------------------------------------------
mkdir -p "$PROOF_DIR/annotated"
_log "Proof directory: $PROOF_DIR"

# ---------------------------------------------------------------------------
# Phase 1: Start Python backend API
# ---------------------------------------------------------------------------
_log "Phase 1: Starting Python backend API on port 18099..."
BACKEND_PY="$REPO_ROOT/backend/api.py"
if [[ ! -f "$BACKEND_PY" ]]; then
  _fail "backend/api.py not found — skipping API startup"
else
  cd "$REPO_ROOT"
  python3 "$BACKEND_PY" --port 18099 &
  API_PID=$!
  _info "API PID: $API_PID"

  # Wait for /health to respond (30s timeout)
  _log "Waiting for API /health..."
  for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:18099/health" >/dev/null 2>&1; then
      _pass "API is up (attempt $i)"
      break
    fi
    if [[ $i -eq 30 ]]; then
      _fail "API did not start within 30s"
      exit 1
    fi
    sleep 1
  done
fi

# ---------------------------------------------------------------------------
# Phase 2: Start Rust saas-service via docker compose
# ---------------------------------------------------------------------------
if [[ "$SKIP_DOCKER" == "false" ]]; then
  _log "Phase 2: Starting saas-service via docker compose..."
  COMPOSE_FILE="$REPO_ROOT/saas-service/docker-compose.yml"
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    _fail "saas-service/docker-compose.yml not found — skipping"
    SKIP_DOCKER=true
  else
    docker compose -f "$COMPOSE_FILE" up -d 2>/dev/null
    _log "Waiting for saas-service /health (port 3000)..."
    for i in $(seq 1 60); do
      if curl -sf "http://127.0.0.1:3000/health" >/dev/null 2>&1; then
        _pass "saas-service is up (attempt $i)"
        break
      fi
      if [[ $i -eq 60 ]]; then
        _fail "saas-service did not start within 60s"
        SKIP_DOCKER=true
      fi
      sleep 1
    done
  fi
else
  _info "Skipping docker (--skip-docker)"
fi

# ---------------------------------------------------------------------------
# Phase 3: Start dashboard
# ---------------------------------------------------------------------------
if [[ "$SKIP_DASHBOARD" == "false" ]]; then
  _log "Phase 3: Starting dashboard..."
  DASHBOARD_DIR="$REPO_ROOT/dashboard"
  if [[ -d "$DASHBOARD_DIR" ]] && [[ -f "$DASHBOARD_DIR/package.json" ]]; then
    # Try to use existing build or build static
    if [[ -d "$DASHBOARD_DIR/dist" ]]; then
      _info "Dashboard dist exists — serving statically on port 5173"
      python3 -m http.server 5173 --directory "$DASHBOARD_DIR/dist" &
      DASHBOARD_PID=$!
    else
      _info "No dist found — serving dashboard source directory"
      python3 -m http.server 5173 --directory "$DASHBOARD_DIR" &
      DASHBOARD_PID=$!
    fi
    sleep 2
    if curl -sf "http://127.0.0.1:5173/" >/dev/null 2>&1; then
      _pass "Dashboard is up"
    else
      _fail "Dashboard did not start"
    fi
  else
    _info "No dashboard directory found — skipping"
    SKIP_DASHBOARD=true
  fi
fi

# ---------------------------------------------------------------------------
# Phase 4: Run checklist verification
# ---------------------------------------------------------------------------
_log "Phase 4: Running programmatic checklist..."
CHECKLIST_RESULTS="$PROOF_DIR/checklist-results.json"
bash "$REPO_ROOT/scripts/run-checklist.sh" \
  --checklist "$REPO_ROOT/verification-report/checklist.json" \
  --output "$CHECKLIST_RESULTS" \
  --proof-dir "$PROOF_DIR" \
  --timestamp "$TIMESTAMP" \
  --api-port 18099 \
  ${SKIP_DOCKER:+--skip-saas} 2>&1 | tee "$PROOF_DIR/checklist-run.log"

# ---------------------------------------------------------------------------
# Phase 5: Bug sweep
# ---------------------------------------------------------------------------
_log "Phase 5: Running bug sweep..."
BUG_MATRIX="$PROOF_DIR/bug-matrix-results.json"
bash "$REPO_ROOT/scripts/sweep-bugs.sh" \
  --output "$BUG_MATRIX" \
  --proof-dir "$PROOF_DIR" 2>&1 | tee "$PROOF_DIR/sweep-bugs.log"

# ---------------------------------------------------------------------------
# Phase 5a: Data completeness check — API data quality assertions
# ---------------------------------------------------------------------------
_log "Phase 5a: Running data completeness check..."
DATA_CHECK_EXIT=0
API_PORT=18099 RUST_PORT=3000 bash "$REPO_ROOT/scripts/data-completeness-check.sh" \
  2>&1 | tee "$PROOF_DIR/data-completeness.log" || DATA_CHECK_EXIT=$?
if [[ $DATA_CHECK_EXIT -ne 0 ]]; then
  _fail "data-completeness-check: $DATA_CHECK_EXIT failure(s) — see $PROOF_DIR/data-completeness.log"
else
  _pass "data-completeness-check: all assertions passed"
fi

# ---------------------------------------------------------------------------
# Phase 5b: Dashboard rendering assertions — Puppeteer DOM checks
# ---------------------------------------------------------------------------
_log "Phase 5b: Running dashboard rendering assertions..."
RENDER_CHECK_EXIT=0
RENDER_SCRIPT="$REPO_ROOT/scripts/verify-dashboard-rendering.js"
if [[ ! -f "$RENDER_SCRIPT" ]]; then
  _info "verify-dashboard-rendering.js not found (archived) — skipping Phase 5b" >&2
elif command -v node >/dev/null 2>&1; then
  API_PORT=18099 SAAS_PORT=5173 PROOF_DIR="$PROOF_DIR" \
    node "$RENDER_SCRIPT" \
    2>&1 | tee "$PROOF_DIR/rendering-check.log" || RENDER_CHECK_EXIT=$?
  if [[ $RENDER_CHECK_EXIT -ne 0 ]]; then
    _fail "verify-dashboard-rendering: rendering assertion(s) failed — see $PROOF_DIR/rendering-check.log"
  else
    _pass "verify-dashboard-rendering: all DOM assertions passed"
  fi
else
  _info "node not found — skipping dashboard rendering assertions (Phase 5b)"
fi

# ---------------------------------------------------------------------------
# Phase 6: Capture visual proof
# ---------------------------------------------------------------------------
_log "Phase 6: Capturing visual proof..."
bash "$REPO_ROOT/scripts/capture-proof.sh" \
  --proof-dir "$PROOF_DIR" \
  --timestamp "$TIMESTAMP" \
  --api-port 18099 \
  ${SKIP_DASHBOARD:+--skip-dashboard} 2>&1 | tee "$PROOF_DIR/capture-proof.log"

# ---------------------------------------------------------------------------
# Phase 7: Build HTML proof report
# ---------------------------------------------------------------------------
_log "Phase 7: Building proof report..."
bash "$REPO_ROOT/scripts/build-proof-report.sh" \
  --proof-dir "$PROOF_DIR" \
  --checklist "$CHECKLIST_RESULTS" \
  --bug-matrix "$BUG_MATRIX" \
  --timestamp "$TIMESTAMP" 2>&1 | tee "$PROOF_DIR/build-report.log"

# ---------------------------------------------------------------------------
# Phase 8: Gate check
# ---------------------------------------------------------------------------
_log "Phase 8: Production readiness gate check..."
bash "$REPO_ROOT/scripts/gate-check.sh" \
  --proof-dir "$PROOF_DIR" \
  --checklist "$CHECKLIST_RESULTS" \
  --bug-matrix "$BUG_MATRIX"
GATE_EXIT=$?

# Incorporate data completeness and rendering failures into gate result
if [[ $DATA_CHECK_EXIT -ne 0 ]] || [[ $RENDER_CHECK_EXIT -ne 0 ]]; then
  _fail "One or more new verification phases failed (data-completeness=$DATA_CHECK_EXIT, rendering=$RENDER_CHECK_EXIT)"
  GATE_EXIT=1
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " E2E Verification Complete"
echo " Proof: $PROOF_DIR"
echo " Report: $PROOF_DIR/proof-report.html"
echo " Gate: $([ $GATE_EXIT -eq 0 ] && echo 'PASS' || echo 'FAIL')"
echo "============================================================"

exit $GATE_EXIT
