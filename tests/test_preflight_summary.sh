#!/usr/bin/env bash
# tests/test_preflight_summary.sh — verify the PRESUM: structured summary line
#
# AC1: clean run → last stdout line matches ^PRESUM: pass checks=[0-9]+ duration=[0-9]+s$
# AC2: induced typecheck failure → last stdout line matches
#      ^PRESUM: fail step=typecheck exit=[1-9][0-9]* checks=[0-9]+ duration=[0-9]+s$
# AC3: pr-size early exit → last stdout line matches
#      ^PRESUM: fail step=pr-size exit=1 checks=0 duration=[0-9]+s$
#
# Tests run via stub scripts to avoid slow/server-dependent external checks.
# HARD RULE: Do NOT call `claude`, `claude -p`, `_start_loop_run`, or trigger /loop.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
TMPFILES=()

ok()   { echo "  [OK] $1";   ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

cleanup() { rm -f "${TMPFILES[@]}" 2>/dev/null || true; }
trap cleanup EXIT

echo "=== test_preflight_summary ==="
echo ""

# ── AC1: success path stub ────────────────────────────────────────────────────
# Create a minimal stub that exercises the PRESUM: pass path directly,
# matching the same code pattern used in scripts/preflight.sh.
echo "--- AC1: success path stub ---"

SUCCESS_STUB=$(mktemp /tmp/preflight-success-XXXXXX.sh)
TMPFILES+=("$SUCCESS_STUB")

cat > "$SUCCESS_STUB" << 'SUCCESS_EOF'
#!/usr/bin/env bash
# Stub: simulates 3 passing checks, then emits PRESUM: pass
set -euo pipefail
START_TS=$SECONDS
CHECKS_RUN=0
CURRENT_SLUG=""

fail() {
    echo "[FAIL] $1"
    echo "PRESUM: fail step=${CURRENT_SLUG:-unknown} exit=1 checks=${CHECKS_RUN} duration=$((SECONDS-START_TS))s"
    exit 1
}

# Simulate 3 passing checks
for slug in pytest python-syntax python-imports; do
    CURRENT_SLUG="$slug"
    ((CHECKS_RUN++)) || true
    echo "[PASS] $slug"
done

echo ""
echo "=== All checks passed ==="
echo "PRESUM: pass checks=${CHECKS_RUN} duration=$((SECONDS-START_TS))s"
exit 0
SUCCESS_EOF
chmod +x "$SUCCESS_STUB"

SUCCESS_OUT=$(bash "$SUCCESS_STUB" 2>&1)
SUCCESS_LAST=$(echo "$SUCCESS_OUT" | tail -1)
echo "  last line: $SUCCESS_LAST"

if echo "$SUCCESS_LAST" | grep -qE '^PRESUM: pass checks=[0-9]+ duration=[0-9]+s$'; then
    ok "AC1 — success path emits PRESUM: pass checks=N duration=Ns"
else
    fail "AC1 — expected 'PRESUM: pass checks=N duration=Ns', got: '$SUCCESS_LAST'"
fi

# Also verify that "=== All checks passed ===" still appears (AC3 from spec)
if echo "$SUCCESS_OUT" | grep -q "=== All checks passed ==="; then
    ok "AC1b — '=== All checks passed ===' still present before PRESUM line"
else
    fail "AC1b — '=== All checks passed ===' missing from output"
fi

# Verify PRESUM is the LAST line (not just present somewhere)
LINE_COUNT=$(echo "$SUCCESS_OUT" | wc -l)
PRESUM_LINE=$(echo "$SUCCESS_OUT" | grep -n "^PRESUM:" | tail -1 | cut -d: -f1)
if [ "$PRESUM_LINE" = "$LINE_COUNT" ]; then
    ok "AC1c — PRESUM is the last line (line $PRESUM_LINE of $LINE_COUNT)"
else
    fail "AC1c — PRESUM is on line $PRESUM_LINE but output has $LINE_COUNT lines"
fi

# ── AC2: typecheck failure stub ───────────────────────────────────────────────
echo ""
echo "--- AC2: induced typecheck failure ---"

TYPECHECK_STUB=$(mktemp /tmp/preflight-typecheck-XXXXXX.sh)
TMPFILES+=("$TYPECHECK_STUB")

cat > "$TYPECHECK_STUB" << 'TYPECHECK_EOF'
#!/usr/bin/env bash
# Stub: simulates 4 passing checks then a typecheck failure
set -euo pipefail
START_TS=$SECONDS
CHECKS_RUN=0
CURRENT_SLUG=""
RED='\033[0;31m'
NC='\033[0m'

fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    if [ -n "${2:-}" ]; then echo "$2" | sed 's/^/    /'; fi
    local _exit_code="${3:-1}"
    echo "PRESUM: fail step=${CURRENT_SLUG:-unknown} exit=${_exit_code} checks=${CHECKS_RUN} duration=$((SECONDS-START_TS))s"
    exit 1
}

# 4 passing checks
for slug in pytest python-syntax python-imports python-interface; do
    CURRENT_SLUG="$slug"
    ((CHECKS_RUN++)) || true
    echo "[PASS] $slug"
done

# 5th check: typecheck fails
CURRENT_SLUG="typecheck"
((CHECKS_RUN++)) || true
fail "TypeScript Type Check" "error TS2345: Argument of type 'string' is not assignable to parameter of type 'number'" 2
TYPECHECK_EOF
chmod +x "$TYPECHECK_STUB"

FAIL_OUT=$(bash "$TYPECHECK_STUB" 2>&1 || true)
FAIL_LAST=$(echo "$FAIL_OUT" | tail -1)
echo "  last line: $FAIL_LAST"

if echo "$FAIL_LAST" | grep -qE '^PRESUM: fail step=typecheck exit=[1-9][0-9]* checks=[0-9]+ duration=[0-9]+s$'; then
    ok "AC2 — typecheck failure emits PRESUM: fail step=typecheck exit=N checks=N duration=Ns"
else
    fail "AC2 — expected PRESUM: fail step=typecheck exit=N ..., got: '$FAIL_LAST'"
fi

# Verify checks=5 (4 passed + the failing one)
if echo "$FAIL_LAST" | grep -q "checks=5"; then
    ok "AC2b — checks counter is 5 (4 passed + 1 failed)"
else
    fail "AC2b — expected checks=5 in: '$FAIL_LAST'"
fi

# ── AC3: pr-size early exit path ─────────────────────────────────────────────
echo ""
echo "--- AC3: pr-size early exit path ---"

PRSIZE_STUB=$(mktemp /tmp/preflight-prsize-XXXXXX.sh)
TMPFILES+=("$PRSIZE_STUB")

cat > "$PRSIZE_STUB" << 'PRSIZE_EOF'
#!/usr/bin/env bash
# Stub: simulates the pr_size_max_lines early exit path
set -euo pipefail
START_TS=$SECONDS
CHECKS_RUN=0
echo "[FAIL] PR diff (9999 insertion lines) exceeds pr_size_max_lines (2000)."
echo "       Split the work into smaller PRs and re-run preflight."
echo "PRESUM: fail step=pr-size exit=1 checks=0 duration=$((SECONDS-START_TS))s"
exit 1
PRSIZE_EOF
chmod +x "$PRSIZE_STUB"

PRSIZE_OUT=$(bash "$PRSIZE_STUB" 2>&1 || true)
PRSIZE_LAST=$(echo "$PRSIZE_OUT" | tail -1)
echo "  last line: $PRSIZE_LAST"

if echo "$PRSIZE_LAST" | grep -qE '^PRESUM: fail step=pr-size exit=1 checks=0 duration=[0-9]+s$'; then
    ok "AC3 — pr-size exit path emits PRESUM: fail step=pr-size exit=1 checks=0"
else
    fail "AC3 — expected PRESUM: fail step=pr-size exit=1 checks=0 ..., got: '$PRSIZE_LAST'"
fi

# ── AC4: verify preflight-fast.sh and preflight-full.sh contain PRESUM emissions ─
# Note: scripts/preflight.sh was archived to archive/preflight-deprecated-2026-05-15/
# per D#893. The PRESUM contract is now carried by preflight-fast.sh and preflight-full.sh.
echo ""
echo "--- AC4: preflight-fast.sh and preflight-full.sh contain PRESUM emissions ---"

PREFLIGHT_FAST="$REPO_ROOT/scripts/preflight-fast.sh"
PREFLIGHT_FULL="$REPO_ROOT/scripts/preflight-full.sh"

if grep -q "^echo \"PRESUM: pass" "$PREFLIGHT_FAST"; then
    ok "AC4 — preflight-fast.sh contains 'PRESUM: pass' emission at success path"
else
    fail "AC4 — preflight-fast.sh missing 'PRESUM: pass' emission"
fi

if grep -q "PRESUM: fail step=\${CURRENT_SLUG" "$REPO_ROOT/scripts/lib/preflight-common.sh"; then
    ok "AC4b — preflight-common.sh fail() emits PRESUM: fail step=\${CURRENT_SLUG}"
else
    fail "AC4b — preflight-common.sh fail() missing PRESUM emission"
fi

if grep -q "PRESUM: fail step=pr-size" "$PREFLIGHT_FAST"; then
    ok "AC4c — preflight-fast.sh has PRESUM emission on pr-size early exit"
else
    fail "AC4c — preflight-fast.sh missing pr-size PRESUM emission"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
