#!/usr/bin/env bash
# tests/test_run_analyst_sweep.sh
#
# Synthetic tests for scripts/lib/run-analyst-sweep.sh (D#1753).
#
# Verifies the sweep can tell a crashed analyzer apart from a clean run,
# captures the real exit code rather than inferring it from output, and
# stays non-fatal so the rest of start-the-day.sh's ritual still runs
# after the analyzer dies.
#
# Usage: bash tests/test_run_analyst_sweep.sh
# Exit 0 = all tests passed; non-zero = at least one failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0

ok() {
  echo "PASS: $1"
  PASS=$((PASS + 1))
}

fail_test() {
  echo "FAIL: $1"
  echo "      $2"
  FAIL=$((FAIL + 1))
}

assert_contains() {
  local name="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    ok "$name"
  else
    fail_test "$name" "expected to find: $needle"
  fi
}

assert_not_contains() {
  local name="$1" needle="$2" haystack="$3"
  if ! echo "$haystack" | grep -qF "$needle"; then
    ok "$name"
  else
    fail_test "$name" "expected NOT to find: $needle"
  fi
}

source "$REPO_ROOT/scripts/lib/run-analyst-sweep.sh"

# ── item 9: exit code is captured, not inferred from output ─────────────────

test_item9_nonzero_exit_produces_marker() {
  local out
  out=$(RUN_ANALYST_CMD='echo "line one"; echo "line two"; echo "line three"; exit 3' run_analyst_sweep)
  assert_contains "item9: non-zero exit (3) produces failure marker" "RUN-ANALYST FAILED" "$out"
}

test_item9_zero_exit_no_marker() {
  local out
  out=$(RUN_ANALYST_CMD='echo "line one"; echo "line two"; echo "line three"; exit 0' run_analyst_sweep)
  assert_not_contains "item9: zero exit does not produce failure marker" "RUN-ANALYST FAILED" "$out"
}

# ── item 10: marker names the exit code and says findings are not all-clear ─

test_item10_marker_has_literal_string_and_exit_code() {
  local out
  out=$(RUN_ANALYST_CMD='exit 7' run_analyst_sweep)
  assert_contains "item10: marker contains literal RUN-ANALYST FAILED" "RUN-ANALYST FAILED" "$out"
  assert_contains "item10: marker names the numeric exit code" "exit 7" "$out"
  assert_contains "item10: marker states absence of findings is not an all-clear" "not an all-clear" "$out"
}

test_item10_clean_run_marker_appears_nowhere() {
  local out
  out=$(RUN_ANALYST_CMD='echo "Findings: 0"; exit 0' run_analyst_sweep)
  assert_not_contains "item10: clean run has no RUN-ANALYST FAILED anywhere" "RUN-ANALYST FAILED" "$out"
}

# ── item 11: sweep stays non-fatal ───────────────────────────────────────────

test_item11_function_returns_0_on_failing_stub() {
  RUN_ANALYST_CMD='exit 3' run_analyst_sweep >/dev/null
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then
    ok "item11: run_analyst_sweep returns 0 even when the stub exits non-zero"
  else
    fail_test "item11: run_analyst_sweep return code" "expected 0, got $rc"
  fi
}

test_item11_ritual_continues_after_failing_stub() {
  # Simulate the shape of start-the-day.sh's sweep section: run the sweep
  # with a failing stub under `set -uo pipefail`, then confirm a later
  # section still prints -- i.e. the failure did not abort the script.
  local out
  out=$(
    set -uo pipefail
    source "$REPO_ROOT/scripts/lib/run-analyst-sweep.sh"
    echo "  Recent run-analyst findings (last 12h):"
    RUN_ANALYST_CMD='exit 9' run_analyst_sweep
    echo "  Stats freshness:"
    echo "    (later section ran)"
  )
  assert_contains "item11: later sweep section still runs after analyzer death" "(later section ran)" "$out"
}

# ── Run all tests ─────────────────────────────────────────────────────────────

echo "=== run-analyst-sweep.sh tests ==="
echo ""

test_item9_nonzero_exit_produces_marker
test_item9_zero_exit_no_marker
test_item10_marker_has_literal_string_and_exit_code
test_item10_clean_run_marker_appears_nowhere
test_item11_function_returns_0_on_failing_stub
test_item11_ritual_continues_after_failing_stub

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
