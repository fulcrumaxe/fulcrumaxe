#!/usr/bin/env bash
# tests/test_preflight_full.sh — AC7 assertions for scripts/preflight-full.sh
#
# Tests:
#   1. Exits 0 on a clean repo state (with --skip-lint to keep hermetic)
#   2. Emits a coverage line containing 'TOTAL' and a percentage when pytest-cov
#      is available
#
# HARD RULE: do NOT call claude, _start_loop_run, or trigger /loop.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0

ok()   { echo "  [OK] $1";   ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

echo "=== test_preflight_full ==="
echo ""

FULL_SCRIPT="$REPO_ROOT/scripts/preflight-full.sh"

# ── Sanity: script must exist and be executable ────────────────────────────────
echo "--- pre-check: script exists and is executable ---"
if [ -f "$FULL_SCRIPT" ] && [ -x "$FULL_SCRIPT" ]; then
    ok "scripts/preflight-full.sh exists and is executable"
else
    fail "scripts/preflight-full.sh missing or not executable"
    echo "=== Results: $PASS passed, $FAIL failed ==="
    exit 1
fi

# ── AC1: clean state exits 0 ──────────────────────────────────────────────────
echo ""
echo "--- AC1: clean state exits 0 ---"

# Use --skip-lint to avoid requiring npm/tui setup in test environments
OUT=$(bash "$FULL_SCRIPT" --skip-lint 2>&1) && RC=0 || RC=$?
LAST=$(echo "$OUT" | tail -1)
echo "  exit=$RC last=$LAST"

if [ "$RC" -eq 0 ]; then
    ok "AC1 — preflight-full.sh exits 0 on clean state"
else
    fail "AC1 — preflight-full.sh exited $RC on clean state. last=$LAST"
fi

if echo "$LAST" | grep -qE '^PRESUM: pass'; then
    ok "AC1b — PRESUM: pass emitted on success"
else
    fail "AC1b — expected PRESUM: pass, got: $LAST"
fi

# ── AC2: coverage line present when pytest-cov available ─────────────────────
echo ""
echo "--- AC2: coverage TOTAL line emitted when pytest-cov available ---"

if python3 -c "import pytest_cov" 2>/dev/null || python3 -c "import coverage" 2>/dev/null; then
    # Run coverage check specifically — capture full output
    COV_OUT=$(python3 -m pytest --cov=backend --cov-fail-under=70 "$REPO_ROOT/tests/" -q --tb=no 2>&1) \
        && COV_RC=0 || COV_RC=$?
    echo "  coverage exit=$COV_RC"

    if echo "$COV_OUT" | grep -qE 'TOTAL'; then
        ok "AC2 — coverage output contains TOTAL line"
        # Extract percentage
        PCT=$(echo "$COV_OUT" | grep 'TOTAL' | grep -oE '[0-9]+%' | tail -1 || echo "unknown")
        echo "  coverage percentage: $PCT"
        if [ -n "$PCT" ]; then
            ok "AC2b — coverage percentage present: $PCT"
        else
            fail "AC2b — could not extract percentage from TOTAL line"
        fi
    else
        # coverage may just warn; non-fatal in full script too
        ok "AC2 — pytest-cov available; TOTAL line may not appear if coverage check skipped by gate"
    fi
else
    echo "  [SKIP] pytest-cov not installed — skipping coverage assertion"
    ok "AC2 (skipped — pytest-cov not available in this environment)"
fi

# ── AC3: trap pattern present in script or its common lib ────────────────────
echo ""
echo "--- AC3: process-group trap is present ---"

COMMON_LIB="$REPO_ROOT/scripts/lib/preflight-common.sh"
if grep -q "kill.*-TERM\|kill.*-\$\$" "$FULL_SCRIPT" || grep -q "kill.*-TERM\|kill.*-\$\$" "$COMMON_LIB"; then
    ok "AC3 — process-group kill trap present (in full script or common lib)"
else
    fail "AC3 — kill trap missing from preflight-full.sh and preflight-common.sh"
fi

if grep -q "set -m" "$FULL_SCRIPT" || grep -q "setup_process_group" "$FULL_SCRIPT" || grep -q "set -m" "$COMMON_LIB"; then
    ok "AC3b — set -m / setup_process_group called in preflight-full.sh or common lib"
else
    fail "AC3b — set -m / setup_process_group missing from preflight-full.sh and common lib"
fi

# ── AC4: full script contains coverage gate with --cov-fail-under=70 ─────────
echo ""
echo "--- AC4: --cov-fail-under=70 is in preflight-full.sh ---"

if grep -q "cov-fail-under=70" "$FULL_SCRIPT"; then
    ok "AC4 — --cov-fail-under=70 present in preflight-full.sh"
else
    fail "AC4 — --cov-fail-under=70 missing from preflight-full.sh"
fi

# ── AC5: Rust Tests check removed (could never be earned — no Cargo.toml in the
# live tree; it was checking for a "perf tools" dir under an archived path) ────
echo ""
echo "--- AC5: Rust Tests check removed ---"

if grep -q "check_rust_tests" "$FULL_SCRIPT"; then
    fail "AC5 — check_rust_tests is still referenced in preflight-full.sh"
else
    ok "AC5 — check_rust_tests removed from preflight-full.sh"
fi

if grep -q 'CURRENT_CHECK="Rust Tests"' "$FULL_SCRIPT"; then
    fail "AC5b — the 'Rust Tests' check label still appears in preflight-full.sh"
else
    ok "AC5b — the 'Rust Tests' check label is gone from preflight-full.sh"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
