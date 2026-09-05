#!/usr/bin/env bash
# tests/test_planned_prs_label.sh — fixture suite for
# scripts/lib/planned-prs-label.sh's decision function (D#2272 Spec item 9).
#
# planned_prs_label_action() is pure (no network) so this suite drives it
# directly with decision-keyword fixtures, then — to prove the wiring, not
# just the isolated function — re-derives the same decision from a realistic
# Discussion body/comments pair via the real discussion_close_decision()
# (scripts/lib/discussion-close-guard.sh) and confirms the label action
# matches. This is the "given a Discussion whose decision is unknown for a
# missing field, applies needs-planned-prs; given one that closes, it does
# not" acceptance criterion end to end, without a real Discussion or a
# mocked `gh`.
#
# Usage: bash tests/test_planned_prs_label.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/discussion-close-guard.sh
source "$REPO_ROOT/scripts/lib/discussion-close-guard.sh"
# shellcheck source=scripts/lib/planned-prs-label.sh
source "$REPO_ROOT/scripts/lib/planned-prs-label.sh"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    _pass "$label"
  else
    _fail "$label — expected '$expected', got '$actual'"
  fi
}

echo "=== test_planned_prs_label: pure decision function ==="

assert_eq "close_decision=unknown -> apply" "apply" "$(planned_prs_label_action "unknown")"
assert_eq "close_decision=close -> clear"   "clear" "$(planned_prs_label_action "close")"
assert_eq "close_decision=hold -> noop"     "noop"  "$(planned_prs_label_action "hold")"
assert_eq "close_decision='' -> noop (fail-safe default)" "noop" "$(planned_prs_label_action "")"

echo ""
echo "=== test_planned_prs_label: end-to-end via discussion_close_decision ==="

echo ""
echo "--- Given a Discussion whose decision is unknown for a missing field, applies ---"
NO_FIELD_BODY="<!-- STATUS:SPEC_READY SINCE:2026-09-02T00:00:00Z -->

## Intent
A normal-looking Discussion body with no planned_prs frontmatter anywhere."

discussion_close_decision "$NO_FIELD_BODY" 0 "false" ""
assert_eq "missing-field fixture resolves to CLOSE_DECISION=unknown" "unknown" "$CLOSE_DECISION"
ACTION=$(planned_prs_label_action "$CLOSE_DECISION")
assert_eq "missing-field fixture -> needs-planned-prs label action is apply" "apply" "$ACTION"

echo ""
echo "--- Given a Discussion that closes, does not apply (clears instead) ---"
CLOSES_BODY="<!-- STATUS:SPEC_READY SINCE:2026-09-02T00:00:00Z -->

---
planned_prs: 1
---

## Spec
One planned PR."

discussion_close_decision "$CLOSES_BODY" 0 "false" ""
assert_eq "planned_prs:1 fixture resolves to CLOSE_DECISION=close" "close" "$CLOSE_DECISION"
ACTION=$(planned_prs_label_action "$CLOSE_DECISION")
assert_eq "closing fixture -> needs-planned-prs label action is clear, never apply" "clear" "$ACTION"

echo ""
echo "--- A declared planned_prs: 0 hold-open never gets flagged as missing ---"
HOLD_ZERO_BODY="<!-- STATUS:SPEC_READY SINCE:2026-09-02T00:00:00Z -->

---
planned_prs: 0
---

## Spec
Operational completion, no PR."

discussion_close_decision "$HOLD_ZERO_BODY" 3 "false" ""
assert_eq "planned_prs:0 fixture resolves to CLOSE_DECISION=hold" "hold" "$CLOSE_DECISION"
ACTION=$(planned_prs_label_action "$CLOSE_DECISION")
assert_eq "deliberate planned_prs:0 hold -> label action is noop, not apply" "noop" "$ACTION"

echo ""
echo "== Summary: $PASS passed, $FAIL failed =="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
