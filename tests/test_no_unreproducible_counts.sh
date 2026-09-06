#!/usr/bin/env bash
# tests/test_no_unreproducible_counts.sh — contract test for
# scripts/ci/verify-no-unreproducible-counts.py (D#1900 PR 1).
#
# Run: bash tests/test_no_unreproducible_counts.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$REPO_ROOT/scripts/ci/verify-no-unreproducible-counts.py"
FIXTURE="$REPO_ROOT/tests/fixtures/ci_workflow/unreproducible-count.yml"
CI_YML="$REPO_ROOT/.github/workflows/ci.yml"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_exit() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" -eq "$expected" ]; then
    pass "$label (exit $actual)"
  else
    fail "$label (expected exit $expected, got $actual)"
  fi
}

# ── Negative case, watched to fail: the fixture carries today's pre-fix
#    line 246 text ("~151 failures...") verbatim, at the same line number. ──
echo "-- fixture carrying the pre-fix '~151 failures' line exits non-zero and names line 246 --"
FIXTURE_OUT=$(python3 "$CHECK" "$FIXTURE" 2>&1)
FIXTURE_RC=$?
if [ "$FIXTURE_RC" -ne 0 ]; then
  pass "check exits non-zero against the pre-fix fixture"
else
  fail "check exits non-zero against the pre-fix fixture (got 0)"
fi
case "$FIXTURE_OUT" in
  *"line 246"*) pass "output names line 246" ;;
  *) fail "output names line 246 — got: $FIXTURE_OUT" ;;
esac

# ── Positive case: the real, fixed ci.yml has no such count today. ─────────
echo "-- real ci.yml (post-fix) exits 0 --"
python3 "$CHECK" "$CI_YML" >/dev/null 2>&1
assert_exit "check exits 0 against .github/workflows/ci.yml" 0 "$?"

echo ""
echo "no-unreproducible-counts contract tests: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
  exit 0
else
  exit 1
fi
