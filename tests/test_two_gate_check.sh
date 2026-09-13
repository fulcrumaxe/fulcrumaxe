#!/usr/bin/env bash
# tests/test_two_gate_check.sh — Unit tests for scripts/lib/two-gate-check.sh
#
# Run: bash tests/test_two_gate_check.sh
# Expects: all assertions pass, exit 0
#
# Uses TWO_GATE_PR_BODY_<PR> env vars to supply fixture bodies without
# making real GitHub API calls.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TWO_GATE_LIB="$REAL_REPO_ROOT/scripts/lib/two-gate-check.sh"

PASS=0
FAIL=0

# -----------------------------------------------------------------------
# Test harness
# -----------------------------------------------------------------------
assert_exit_0() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then
    echo "  PASS: $label (exit 0)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected exit 0, got $rc)"
    FAIL=$((FAIL + 1))
  fi
}

assert_exit_1() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 1 ]; then
    echo "  PASS: $label (exit 1)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected exit 1, got $rc)"
    FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local label="$1" expected_substr="$2" actual="$3"
  if echo "$actual" | grep -qF "$expected_substr"; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label"
    echo "        expected to contain: $expected_substr"
    echo "        actual: $actual"
    FAIL=$((FAIL + 1))
  fi
}

# -----------------------------------------------------------------------
# Load the library in a subshell per test to avoid state leakage.
# Usage: _run_check <pr_num> <body_literal>
#   Returns exit code of check_two_gate_markers.
#   Prints TWO_GATE_FAIL_REASON to stdout (empty on success).
# -----------------------------------------------------------------------
_run_check() {
  local pr="$1"
  local body="$2"
  (
    source "$TWO_GATE_LIB"
    export "TWO_GATE_PR_BODY_${pr}=${body}"
    if check_two_gate_markers "$pr" "test-owner/test-repo"; then
      echo "REASON:"
      exit 0
    else
      echo "REASON:${TWO_GATE_FAIL_REASON:-}"
      exit 1
    fi
  )
  return $?
}

# -----------------------------------------------------------------------
# D#2566 PR-2: the marker checks above are no longer the whole story —
# check_two_gate_markers now also calls gate1_receipt_check, which needs a
# receipt. _fixture_receipt exports GATE1_RECEIPT_HEAD_SHA_<PR> and
# GATE1_RECEIPT_JSON_<PR> (both read by gate1-receipt-check.sh's own
# test-mode override, mirroring TWO_GATE_PR_BODY_<PR> above) so every
# fixture below that expects check_two_gate_markers to ACCEPT still can,
# without a real gh call or a real receipt file on disk. routing/tests_run
# are left empty on purpose: this file tests the PROSE-marker logic, not
# the receipt's own authorization rules (that is
# tests/test_gate1_receipt_check.sh's job) — an empty routing list can
# never trip the unrouted-vs-N/A checks either way.
_fixture_receipt() {
  local pr="$1"
  local sha
  sha="$(printf 'dead%036x' "$pr")"
  export "GATE1_RECEIPT_HEAD_SHA_${pr}=${sha}"
  export "GATE1_RECEIPT_JSON_${pr}=$(python3 -c "
import json
print(json.dumps({
    'schema': 1,
    'caller': {
        'pr': int('$pr'), 'repo': 'test-owner/test-repo',
        'pr_head_sha': '$sha', 'tree_root': '/tmp/pr-head-tree-$pr',
        'gate1_runner_copy': '/tmp/operator-checkout/scripts/run-pr-tests.sh',
        'gate1_containment': 'NONE (same-uid)',
        'containment_probes': {
            'gh-credential': 'NOT-DENIED', 'state-dir': 'NOT-DENIED',
            'operator-checkout-write': 'NOT-DENIED', 'network': 'NOT-DENIED',
        },
        'containment_verdict': 'UNCONTAINED',
        'env': {
            'AUTONOMOUS_TEAM_REPO': 'test-owner/test-repo',
            'AUTONOMOUS_TEAM_STATE_DIR': '/tmp/x',
            'seed_files': {'.autonomous-team/config.json': False, '.autonomous-team/project.json': False},
        },
        'written_at': '2026-01-01T00:00:00Z', 'receipt_path': '/tmp/fixture-receipt-$pr.json',
    },
    'head_reported': {'routing': [], 'tests_run': [], 'partial': False, 'measured_tree': {}},
}))
")"
}

# -----------------------------------------------------------------------
# TG-1: Both gates PASS — should accept
# -----------------------------------------------------------------------
echo ""
echo "=== TG-1: both gates PASS — accept ==="
BODY_TG1="## Verification\nGate 1: PASS — all unit tests green\nGate 2: PASS — smoke test ran clean"
_fixture_receipt 10001
OUTPUT_TG1=$(_run_check 10001 "$BODY_TG1" 2>&1)
RC_TG1=$?
assert_exit_0 "TG-1: both gates PASS accepted" "$RC_TG1"

# -----------------------------------------------------------------------
# TG-2: Both gates N/A with justification — should accept
# -----------------------------------------------------------------------
echo ""
echo "=== TG-2: both gates N/A with justification — accept ==="
BODY_TG2="## Verification\nGate 1: N/A — doc-only change\nGate 2: N/A — wiki update, no binary"
_fixture_receipt 10002
OUTPUT_TG2=$(_run_check 10002 "$BODY_TG2" 2>&1)
RC_TG2=$?
assert_exit_0 "TG-2: both gates N/A with justification accepted" "$RC_TG2"

# -----------------------------------------------------------------------
# TG-3: Gate 2 missing entirely — should reject
# -----------------------------------------------------------------------
echo ""
echo "=== TG-3: Gate 2 missing — reject ==="
BODY_TG3="## Verification\nGate 1: PASS — unit tests pass\nThis PR does something useful."
OUTPUT_TG3=$(_run_check 10003 "$BODY_TG3" 2>&1)
RC_TG3=$?
assert_exit_1 "TG-3: missing Gate 2 rejected" "$RC_TG3"
assert_contains "TG-3: fail reason mentions Gate 2" "Gate 2" "$OUTPUT_TG3"

# -----------------------------------------------------------------------
# TG-4: Gate 2 bare N/A without justification — should reject
# -----------------------------------------------------------------------
echo ""
echo "=== TG-4: Gate 2 bare N/A (no justification) — reject ==="
BODY_TG4="## Verification\nGate 1: PASS — tests pass\nGate 2: N/A"
OUTPUT_TG4=$(_run_check 10004 "$BODY_TG4" 2>&1)
RC_TG4=$?
assert_exit_1 "TG-4: bare Gate 2 N/A (no justification) rejected" "$RC_TG4"
assert_contains "TG-4: fail reason mentions justification" "justification" "$OUTPUT_TG4"

# -----------------------------------------------------------------------
# TG-5: Malformed gate strings (just the word Gate, no number/status) — reject
# -----------------------------------------------------------------------
echo ""
echo "=== TG-5: malformed gate strings — reject ==="
BODY_TG5="## Verification\nGate: some text\nVerification was done manually."
OUTPUT_TG5=$(_run_check 10005 "$BODY_TG5" 2>&1)
RC_TG5=$?
assert_exit_1 "TG-5: malformed gates rejected" "$RC_TG5"
assert_contains "TG-5: fail reason mentions Gate 1" "Gate 1" "$OUTPUT_TG5"

# -----------------------------------------------------------------------
# TG-6: Case-insensitive — lowercase gate should work
# -----------------------------------------------------------------------
echo ""
echo "=== TG-6: case-insensitive matching — accept ==="
BODY_TG6="## Verification\ngate 1: pass\ngate 2: pass"
_fixture_receipt 10006
OUTPUT_TG6=$(_run_check 10006 "$BODY_TG6" 2>&1)
RC_TG6=$?
assert_exit_0 "TG-6: lowercase gate markers accepted" "$RC_TG6"

# -----------------------------------------------------------------------
# TG-7: Gate 1 missing entirely — should reject
# -----------------------------------------------------------------------
echo ""
echo "=== TG-7: Gate 1 missing — reject ==="
BODY_TG7="## Verification\nGate 2: PASS — smoke test ok"
OUTPUT_TG7=$(_run_check 10007 "$BODY_TG7" 2>&1)
RC_TG7=$?
assert_exit_1 "TG-7: missing Gate 1 rejected" "$RC_TG7"
assert_contains "TG-7: fail reason mentions Gate 1" "Gate 1" "$OUTPUT_TG7"

# -----------------------------------------------------------------------
# TG-8: Gate 2 N/A followed by justification on next line — accept
# -----------------------------------------------------------------------
echo ""
echo "=== TG-8: Gate 2 N/A with next-line justification — accept ==="
BODY_TG8="## Verification\nGate 1: PASS\nGate 2: N/A\nwiki-only change, no deployable artifact"
_fixture_receipt 10008
OUTPUT_TG8=$(_run_check 10008 "$BODY_TG8" 2>&1)
RC_TG8=$?
assert_exit_0 "TG-8: Gate 2 N/A with next-line justification accepted" "$RC_TG8"

# -----------------------------------------------------------------------
# TG-9: Empty PR body — should reject
# -----------------------------------------------------------------------
echo ""
echo "=== TG-9: empty body — reject ==="
BODY_TG9=" "
OUTPUT_TG9=$(_run_check 10009 "$BODY_TG9" 2>&1)
RC_TG9=$?
assert_exit_1 "TG-9: empty body rejected" "$RC_TG9"

# -----------------------------------------------------------------------
# TG-10: Both gates PASSED — should accept (new PASSED alias)
# -----------------------------------------------------------------------
echo ""
echo "=== TG-10: PASSED token — accept ==="
BODY_TG10="## Verification\nGate 1: PASSED — pytest: 12/12\nGate 2: PASSED — manual smoke ok"
_fixture_receipt 10010
OUTPUT_TG10=$(_run_check 10010 "$BODY_TG10" 2>&1)
RC_TG10=$?
assert_exit_0 "TG-10: PASSED token accepted" "$RC_TG10"

# -----------------------------------------------------------------------
# TG-11: Both gates ✓ — should accept (new checkmark alias)
# -----------------------------------------------------------------------
echo ""
echo "=== TG-11: ✓ token — accept ==="
BODY_TG11="## Verification\nGate 1: ✓ — tests green\nGate 2: ✓ — smoke passed"
_fixture_receipt 10011
OUTPUT_TG11=$(_run_check 10011 "$BODY_TG11" 2>&1)
RC_TG11=$?
assert_exit_0 "TG-11: ✓ token accepted" "$RC_TG11"

# -----------------------------------------------------------------------
# TG-12: Bare count "12 passed" — should still reject (no recognized token)
# -----------------------------------------------------------------------
echo ""
echo "=== TG-12: bare count '12 passed' — reject ==="
BODY_TG12="## Verification\nGate 1: 12 passed\nGate 2: 86%"
OUTPUT_TG12=$(_run_check 10012 "$BODY_TG12" 2>&1)
RC_TG12=$?
assert_exit_1 "TG-12: bare count rejected" "$RC_TG12"
assert_contains "TG-12: fail reason mentions Gate 1" "Gate 1" "$OUTPUT_TG12"

# -----------------------------------------------------------------------
# TG-13: Percentage "86%" — should still reject (no recognized token)
# -----------------------------------------------------------------------
echo ""
echo "=== TG-13: percentage '86%' — reject ==="
BODY_TG13="## Verification\nGate 1: PASS — tests ok\nGate 2: 86% coverage"
OUTPUT_TG13=$(_run_check 10013 "$BODY_TG13" 2>&1)
RC_TG13=$?
assert_exit_1 "TG-13: percentage alone rejected" "$RC_TG13"
assert_contains "TG-13: fail reason mentions Gate 2" "Gate 2" "$OUTPUT_TG13"

# -----------------------------------------------------------------------
# TG-14 (D#2566 PR-2): well-formed markers, no receipt fixture at all —
# must now reject. Before this PR, valid marker text alone was enough;
# this is the exact behavior change this Discussion exists to make.
# -----------------------------------------------------------------------
echo ""
echo "=== TG-14: markers fine, receipt absent — reject (D#2566) ==="
BODY_TG14="## Verification\nGate 1: PASS — tests ok\nGate 2: PASS — smoke ok"
export "GATE1_RECEIPT_HEAD_SHA_10014=$(printf 'dead%036x' 10014)"
unset "GATE1_RECEIPT_JSON_10014"
export GATE1_RECEIPT_STATE_DIR="$(mktemp -d)"
OUTPUT_TG14=$(_run_check 10014 "$BODY_TG14" 2>&1)
RC_TG14=$?
unset GATE1_RECEIPT_STATE_DIR
assert_exit_1 "TG-14: prose-valid PR rejected without a receipt" "$RC_TG14"
assert_contains "TG-14: fail reason mentions gate1-invoke.sh" "gate1-invoke.sh" "$OUTPUT_TG14"

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
