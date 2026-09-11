#!/usr/bin/env bash
# tests/test_coldstart_state_dir_guard.sh — the coldstart-state-dir-guard.py
# check itself must fail loudly, not report clean, when its NON_MUTATING_FLAGS
# pattern list is empty or compiles to a degenerate regex alternation (D#2493).
#
# NON_MUTATING_FLAGS is compiled into a "|"-joined regex alternation. With no
# members (or members that are all empty strings), the alternation degenerates
# to one that can match the empty string — which the surrounding boundary
# pattern turns into a match at most positions in a typical shell line, so
# every invocation gets misclassified as provably non-mutating and skipped.
# An empty input set must mean "could not establish", not "established, and
# it is fine" (same shape as the D#1928 fix to manifest.py verify) — refuse
# rather than silently report a clean scan.
#
# tests/test_coldstart_state_containment.sh (the closest existing suite) never
# invokes this guard at all — it exercises coldstart-project.sh and
# sweep-stale-state-dirs.sh directly — so this is a new file rather than an
# extension of that one.

set -uo pipefail

PASS=0
FAIL=0
_pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
_fail() { echo "FAIL: $1 — $2"; FAIL=$((FAIL + 1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GUARD="$REPO_ROOT/scripts/ci/coldstart-state-dir-guard.py"

WORK="$(mktemp -d /tmp/test-coldstart-guard-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# ── 1. The shipped guard is unchanged: exits 0, and its own embedded
#      self-test (which includes a positive "an offending line is flagged"
#      case) passes as part of that — this is also the evidence for item 4,
#      that the fix didn't achieve its refusal by refusing everything ───────
SHIPPED_OUT="$(python3 "$GUARD" 2>&1)"
SHIPPED_RC=$?
if [[ $SHIPPED_RC -eq 0 ]]; then
  _pass "shipped guard (real NON_MUTATING_FLAGS) exits 0"
else
  _fail "shipped guard exits 0" "exit=$SHIPPED_RC:
$SHIPPED_OUT"
fi
if grep -q "^FAIL " <<<"$SHIPPED_OUT"; then
  _fail "shipped guard's own self-test has no failing case" "$SHIPPED_OUT"
else
  _pass "shipped guard's embedded self-test (incl. the positive/offending case) passes"
fi

# Helper: copy the guard with NON_MUTATING_FLAGS replaced by $1, run it
# standalone. The refusal this test is about happens at module import time,
# before the guard ever touches git or $REPO_ROOT, so running the copy from
# a plain scratch directory (no git checkout) is representative.
run_with_flags() {
  local replacement="$1" copy="$WORK/guard-variant-$RANDOM.py"
  sed "s/^NON_MUTATING_FLAGS = .*/NON_MUTATING_FLAGS = ${replacement}/" "$GUARD" > "$copy"
  python3 "$copy" 2>&1
}

# ── 2. Positive control: an empty list must be refused ───────────────────────
# Watched to fail first: on the version of this guard shipped before D#2493's
# fix, this same replacement does NOT reproduce the Discussion's "exits 0
# reporting clean" premise — the degenerate pattern also swallows several of
# the guard's own self-test fixtures, so self-test failure already caught it,
# exiting 1 for an unrelated-looking reason ("expected a finding, got none")
# rather than naming the actual cause. The fix's job is to name the cause
# directly, not to introduce a non-zero exit that already existed by accident.
EMPTY_OUT="$(run_with_flags '()')"
EMPTY_RC=$?
if [[ $EMPTY_RC -ne 0 ]]; then
  _pass "NON_MUTATING_FLAGS = () makes the guard refuse (non-zero exit)"
else
  _fail "empty NON_MUTATING_FLAGS refused" "exit=0, guard did not refuse:
$EMPTY_OUT"
fi
if grep -qi "degenerate" <<<"$EMPTY_OUT" && grep -q "NON_MUTATING_FLAGS" <<<"$EMPTY_OUT"; then
  _pass "the refusal names NON_MUTATING_FLAGS and the degenerate-pattern cause"
else
  _fail "refusal names the cause" "output did not name NON_MUTATING_FLAGS/degenerate:
$EMPTY_OUT"
fi

# ── 3. A list of empty strings must ALSO be refused — a bare length check
#      would let this through; the compiled pattern must be checked ─────────
BLANK_OUT="$(run_with_flags '("", "")')"
BLANK_RC=$?
if [[ $BLANK_RC -ne 0 ]]; then
  _pass 'NON_MUTATING_FLAGS = ("", "") makes the guard refuse'
else
  _fail 'blank-string NON_MUTATING_FLAGS refused' "exit=0, guard did not refuse:
$BLANK_OUT"
fi
if grep -qi "degenerate" <<<"$BLANK_OUT" && grep -q "NON_MUTATING_FLAGS" <<<"$BLANK_OUT"; then
  _pass "the blank-string refusal also names the cause"
else
  _fail "blank-string refusal names the cause" "output did not name NON_MUTATING_FLAGS/degenerate:
$BLANK_OUT"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -gt 0 ]] && exit 1
exit 0
