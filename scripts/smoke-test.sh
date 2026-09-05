#!/usr/bin/env bash
# smoke-test.sh — full-stack smoke test for autonomous-forever
#
# Verifies every Python backend module imports, every CLI entry point responds,
# the API server starts and serves /health, the TUI builds and typechecks, and
# (if Rust toolchain is present) the saas-service compiles.
#
# Exit codes:
#   0 — all checks passed
#   1 — one or more checks failed (first failure exits unless --continue-on-error)
#
# Usage:
#   ./scripts/smoke-test.sh
#   ./scripts/smoke-test.sh --continue-on-error   # run all checks, report at end
#
# Run from the repository root.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONTINUE_ON_ERROR=0
if [[ "${1:-}" == "--continue-on-error" ]]; then
  CONTINUE_ON_ERROR=1
fi

PASS=0
FAIL=0
FAILURES=()

_pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
_fail() {
  echo "  FAIL: $1"
  echo "        $2"
  FAIL=$((FAIL + 1))
  FAILURES+=("$1: $2")
  if [[ $CONTINUE_ON_ERROR -eq 0 ]]; then
    echo ""
    echo "Smoke test aborted at first failure. Run with --continue-on-error to see all failures."
    exit 1
  fi
}

echo "============================================================"
echo " autonomous-forever smoke test"
echo " $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "============================================================"
echo ""

# ---------------------------------------------------------------------------
# 1. Python backend — import every non-test module
# ---------------------------------------------------------------------------
echo "[1/5] Python backend imports"
BACKEND_MODULES=$(find backend/ -maxdepth 1 -name "*.py" \
  ! -name "__*" \
  ! -name "test_*" \
  | sort \
  | sed 's|backend/||; s|\.py$||')

for mod in $BACKEND_MODULES; do
  result=$(python3 -c "import backend.${mod}" 2>&1)
  if [[ $? -eq 0 ]]; then
    _pass "backend.${mod}"
  else
    msg=$(echo "$result" | grep -E "^(ModuleNotFoundError|ImportError|SyntaxError)" | head -1 || echo "$result" | tail -1)
    _fail "backend.${mod}" "$msg"
  fi
done

# ---------------------------------------------------------------------------
# 2. CLI entry points — modules with __main__ respond to --help
# ---------------------------------------------------------------------------
echo ""
echo "[2/5] CLI entry points (--help)"

CLI_MODULES=(api registry cli spawn_queue workflow_runner)
for mod in "${CLI_MODULES[@]}"; do
  result=$(timeout --kill-after=5s 5 python3 "backend/${mod}.py" --help 2>&1)
  status=$?
  if [[ $status -eq 124 ]]; then
    _fail "$mod --help" "timed out after 5s"
  elif echo "$result" | grep -qi "usage\|options\|command"; then
    _pass "$mod --help"
  else
    msg=$(echo "$result" | head -1)
    _fail "$mod --help" "$msg"
  fi
done

# ---------------------------------------------------------------------------
# 3. API server — start, curl /health, stop
# ---------------------------------------------------------------------------
echo ""
echo "[3/5] API server /health"

SMOKE_PORT=18099
# Kill any stale process on the port
fuser -k "${SMOKE_PORT}/tcp" 2>/dev/null || true

python3 backend/api.py --port "$SMOKE_PORT" &
API_PID=$!
sleep 2

if kill -0 "$API_PID" 2>/dev/null; then
  HEALTH=$(curl -s --max-time 3 "http://localhost:${SMOKE_PORT}/health" 2>&1)
  kill "$API_PID" 2>/dev/null
  wait "$API_PID" 2>/dev/null || true

  if echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)" 2>/dev/null; then
    _pass "api.py GET /health → 200 ok"
  else
    _fail "api.py GET /health" "unexpected response: $HEALTH"
  fi
else
  _fail "api.py startup" "process exited before curl"
fi

# ---------------------------------------------------------------------------
# 4. TUI — build and typecheck
# ---------------------------------------------------------------------------
echo ""
echo "[4/5] TUI (npm run build + typecheck + lint)"
cd tui

# Install dependencies silently
npm install --silent 2>/dev/null

BUILD_OUT=$(npm run build 2>&1)
if [[ $? -eq 0 ]]; then
  _pass "tui npm run build"
else
  _fail "tui npm run build" "$(echo "$BUILD_OUT" | grep -E "error TS" | head -1 || echo "build failed")"
fi

TYPECHECK_OUT=$(npm run typecheck 2>&1)
if [[ $? -eq 0 ]]; then
  _pass "tui npm run typecheck"
else
  _fail "tui npm run typecheck" "$(echo "$TYPECHECK_OUT" | grep -E "error TS" | head -1 || echo "typecheck failed")"
fi

LINT_OUT=$(npm run lint 2>&1)
if [[ $? -eq 0 ]]; then
  _pass "tui npm run lint"
else
  _fail "tui npm run lint" "$(echo "$LINT_OUT" | head -1)"
fi

cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 5. Rust (saas-service) — only if cargo is available
# ---------------------------------------------------------------------------
echo ""
echo "[5/5] Rust saas-service (cargo check)"
if command -v cargo &>/dev/null; then
  cd saas-service
  CARGO_OUT=$(cargo check 2>&1)
  if [[ $? -eq 0 ]]; then
    _pass "saas-service cargo check"
  else
    _fail "saas-service cargo check" "$(echo "$CARGO_OUT" | grep "^error" | head -1)"
  fi
  cd "$REPO_ROOT"
else
  echo "  SKIP: cargo not found — Rust check skipped"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Results: ${PASS} passed, ${FAIL} failed"
echo "============================================================"

if [[ ${FAIL} -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for f in "${FAILURES[@]}"; do
    echo "  - $f"
  done
  exit 1
fi

echo ""
echo "All checks passed."
exit 0
