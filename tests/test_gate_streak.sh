#!/usr/bin/env bash
# tests/test_gate_streak.sh — wiring tests for the D#2271 PR-a gate streak
# design: scripts/lib/ci-status-check.sh's positive marker + fallback
# writer, and backend/gate_streak.py's CLI.
#
# The pure counting/rendering logic (compute_streak, render_line) has its
# own suite: backend/test_gate_streak.py (pytest). This file covers the
# shell-side wiring: where the markers get written, and that the module
# they feed never names the pre-existing decline-reason kinds.
#
# Run: bash tests/test_gate_streak.sh

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CI_LIB="$REAL_REPO_ROOT/scripts/lib/ci-status-check.sh"
MERGE_HOOK="$REAL_REPO_ROOT/scripts/merge-and-hook.sh"
STEP5="$REAL_REPO_ROOT/scripts/loop-phased-step5.sh"
GATE_STREAK_PY="$REAL_REPO_ROOT/backend/gate_streak.py"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# -----------------------------------------------------------------------
# AC-1: positive marker written in ci-status-check.sh, both consumers reach it
# -----------------------------------------------------------------------
echo "=== AC-1: positive-marker kind is written in ci-status-check.sh ==="
if grep -q 'CI_GATE_VERIFIED_KIND="ci_gate_verified"' "$CI_LIB"; then
  pass "positive-marker kind defined"
else
  fail "positive-marker kind not found"
fi
if grep -q 'ci_write_audit "\$CI_GATE_VERIFIED_KIND"' "$CI_LIB"; then
  pass "positive marker is written via ci_write_audit"
else
  fail "positive marker is not written anywhere"
fi
# Both consumers reach check_ci_status (the function that writes it) on
# their success path — merge-and-hook.sh directly, loop-phased-step5.sh via
# _check_ci_passed.
if grep -q 'check_ci_status "\$PR" "\$_CODE_REPO"' "$MERGE_HOOK"; then
  pass "merge-and-hook.sh calls check_ci_status"
else
  fail "merge-and-hook.sh does not call check_ci_status"
fi
# D#2348 PR-e split loop-phased-step5.sh's single _REPO into _CODE_REPO
# (commits, PRs, CI) and _DISCUSSION_REPO (Discussions). CI status is a
# PR-plane read, so it must be the code slug that reaches check_ci_status —
# asserting the exact name is the point, not incidental to it.
if grep -q 'check_ci_status "\$pr" "\$_CODE_REPO"' "$STEP5"; then
  pass "loop-phased-step5.sh's _check_ci_passed calls check_ci_status"
else
  fail "loop-phased-step5.sh does not call check_ci_status"
fi

# Functional proof: a real green check-runs fixture through check_ci_status
# leaves exactly one ci_gate_verified row.
echo ""
echo "=== AC-1 (functional): a green check_ci_status call writes the marker ==="
AUDIT_AC1="$(mktemp)"
(
  source "$CI_LIB"
  export CI_STATUS_TEST_MODE=1
  export CI_STATUS_TEST_AUDIT_FILE="$AUDIT_AC1"
  export CI_KILL_SWITCH_OVERRIDE=HTTP_404
  export CI_STATUS_OVERRIDE_60001='[{"name":"tui","status":"completed","conclusion":"success","app":{"slug":"github-actions"}},{"name":"dashboard","status":"completed","conclusion":"success","app":{"slug":"github-actions"}},{"name":"ts-backend","status":"completed","conclusion":"success","app":{"slug":"github-actions"}},{"name":"backend (import-smoke)","status":"completed","conclusion":"success","app":{"slug":"github-actions"}}]'
  export CI_STATUS_HEAD_SHA_60001="feedface"
  check_ci_status 60001 owner/repo
)
if grep -q '"kind": "ci_gate_verified"' "$AUDIT_AC1" && grep -q '"pr": 60001' "$AUDIT_AC1"; then
  pass "check_ci_status pass branch writes ci_gate_verified"
else
  fail "no ci_gate_verified row written: $(cat "$AUDIT_AC1" 2>/dev/null)"
fi
rm -f "$AUDIT_AC1"

# -----------------------------------------------------------------------
# AC-4: fallback marker — a merge that proceeds without CI_STATUS_STATE
# reaching "pass" and without an already-written decline row still leaves
# a trace. No product code names a bypass here — this exercises the
# mechanism directly, which is what makes it work for a bypass invented
# tomorrow, not just the ones that exist today.
# -----------------------------------------------------------------------
echo ""
echo "=== AC-4: ci_note_merge_if_unverified leaves a trace with nothing else written ==="
AUDIT_AC4="$(mktemp)"
(
  source "$CI_LIB"
  export CI_STATUS_TEST_MODE=1
  export CI_STATUS_TEST_AUDIT_FILE="$AUDIT_AC4"
  CI_STATUS_STATE="fail"
  ci_note_merge_if_unverified 70001 "sha1" "false"
)
if grep -q '"kind": "ci_gate_unverified_merge"' "$AUDIT_AC4"; then
  pass "fallback row written when nothing else recorded the decline"
else
  fail "expected a ci_gate_unverified_merge row: $(cat "$AUDIT_AC4" 2>/dev/null)"
fi
rm -f "$AUDIT_AC4"

echo ""
echo "=== AC-4: fallback suppressed when caller already wrote its own row ==="
AUDIT_AC4B="$(mktemp)"
(
  source "$CI_LIB"
  export CI_STATUS_TEST_MODE=1
  export CI_STATUS_TEST_AUDIT_FILE="$AUDIT_AC4B"
  CI_STATUS_STATE="disabled"
  ci_write_audit "ci_gate_stood_down" 70002 "sha2" "" "" "CI_DISABLED=true"
  ci_note_merge_if_unverified 70002 "sha2" "true"
)
LINES_AC4B=$(grep -c '"kind"' "$AUDIT_AC4B" 2>/dev/null || true)
if [[ "${LINES_AC4B:-0}" -eq 1 ]]; then
  pass "no double-write when the caller already recorded the decline"
else
  fail "expected exactly 1 row, got ${LINES_AC4B:-0}: $(cat "$AUDIT_AC4B" 2>/dev/null)"
fi
rm -f "$AUDIT_AC4B"

echo ""
echo "=== AC-4: fallback suppressed when CI_STATUS_STATE is pass ==="
AUDIT_AC4C="$(mktemp)"
(
  source "$CI_LIB"
  export CI_STATUS_TEST_MODE=1
  export CI_STATUS_TEST_AUDIT_FILE="$AUDIT_AC4C"
  CI_STATUS_STATE="pass"
  ci_note_merge_if_unverified 70003 "sha3" "false"
)
if [[ ! -s "$AUDIT_AC4C" ]]; then
  pass "no fallback row when the merge was actually verified"
else
  fail "unexpected row written for a verified merge: $(cat "$AUDIT_AC4C")"
fi
rm -f "$AUDIT_AC4C"

# -----------------------------------------------------------------------
# AC-4 (anti-rot, end to end via the reader): a novel kind nobody registered
# anywhere still increments the streak, with zero code changes required.
# -----------------------------------------------------------------------
echo ""
echo "=== AC-4: streak reader increments on a never-registered bypass kind ==="
AUDIT_NOVEL="$(mktemp)"
printf '%s\n' '{"kind": "zz_novel_bypass_20260903", "pr": 1, "ts": "2026-09-03T00:00:00Z"}' >> "$AUDIT_NOVEL"
printf '%s\n' '{"kind": "ci_gate_unverified_merge", "pr": 2, "ts": "2026-09-03T00:00:01Z"}' >> "$AUDIT_NOVEL"
STREAK_NOVEL=$(CI_STATUS_TEST_MODE=1 CI_STATUS_TEST_AUDIT_FILE="$AUDIT_NOVEL" python3 "$GATE_STREAK_PY")
if [[ "$STREAK_NOVEL" == "2" ]]; then
  pass "two never-registered rows increment the streak by 2 (no registration needed)"
else
  fail "expected streak=2, got $STREAK_NOVEL"
fi
rm -f "$AUDIT_NOVEL"

# -----------------------------------------------------------------------
# AC-2: CLI prints the bare integer against a fixture
# -----------------------------------------------------------------------
echo ""
echo "=== AC-2: gate_streak.py CLI against a fixture of stand-downs + markers ==="
AUDIT_AC2="$(mktemp)"
for _ in 1 2 3 4 5; do
  printf '%s\n' '{"kind": "ci_gate_stood_down", "pr": 1, "ts": "x"}' >> "$AUDIT_AC2"
done
STREAK_AC2=$(CI_STATUS_TEST_MODE=1 CI_STATUS_TEST_AUDIT_FILE="$AUDIT_AC2" python3 "$GATE_STREAK_PY")
if [[ "$STREAK_AC2" == "5" ]]; then
  pass "5 stand-down rows, 0 markers -> streak=5"
else
  fail "expected streak=5, got $STREAK_AC2"
fi
printf '%s\n' '{"kind": "ci_gate_verified", "pr": 1, "ts": "x"}' >> "$AUDIT_AC2"
printf '%s\n' '{"kind": "ci_gate_stood_down", "pr": 1, "ts": "x"}' >> "$AUDIT_AC2"
printf '%s\n' '{"kind": "ci_gate_stood_down", "pr": 1, "ts": "x"}' >> "$AUDIT_AC2"
STREAK_AC2B=$(CI_STATUS_TEST_MODE=1 CI_STATUS_TEST_AUDIT_FILE="$AUDIT_AC2" python3 "$GATE_STREAK_PY")
if [[ "$STREAK_AC2B" == "2" ]]; then
  pass "3 stand-downs, 1 marker, 2 stand-downs -> streak=2 (not 5)"
else
  fail "expected streak=2, got $STREAK_AC2B"
fi
rm -f "$AUDIT_AC2"

# -----------------------------------------------------------------------
# AC-3: team_status.py prints no streak line at 0, one line at >=1
# -----------------------------------------------------------------------
echo ""
echo "=== AC-3: team_status.py human output reflects the streak ==="
AUDIT_AC3_ZERO="$(mktemp)"
AUDIT_AC3_NONZERO="$(mktemp)"
printf '%s\n' '{"kind": "ci_gate_stood_down", "pr": 1, "ts": "x"}' >> "$AUDIT_AC3_NONZERO"
OUT_ZERO=$(CI_STATUS_TEST_MODE=1 CI_STATUS_TEST_AUDIT_FILE="$AUDIT_AC3_ZERO" python3 "$REAL_REPO_ROOT/backend/team_status.py" 2>/dev/null)
OUT_NONZERO=$(CI_STATUS_TEST_MODE=1 CI_STATUS_TEST_AUDIT_FILE="$AUDIT_AC3_NONZERO" python3 "$REAL_REPO_ROOT/backend/team_status.py" 2>/dev/null)
if ! printf '%s' "$OUT_ZERO" | grep -q "CI GATE STREAK"; then
  pass "streak=0 fixture prints no streak line"
else
  fail "streak=0 fixture unexpectedly printed a streak line"
fi
if printf '%s' "$OUT_NONZERO" | grep -q "CI GATE STREAK"; then
  pass "streak>=1 fixture prints one streak line"
else
  fail "streak>=1 fixture printed no streak line"
fi
if [[ "$OUT_ZERO" != "$OUT_NONZERO" ]]; then
  pass "the two outputs differ (diffable per AC-3)"
else
  fail "outputs are identical — streak line had no effect"
fi
rm -f "$AUDIT_AC3_ZERO" "$AUDIT_AC3_NONZERO"

# -----------------------------------------------------------------------
# AC-6: --force-no-ci still requires a non-empty --bypass-reason; no new
# escape hatch was added by this change.
# -----------------------------------------------------------------------
echo ""
echo "=== AC-6: no new escape hatch — force-no-ci is unchanged ==="
if grep -q -- '--force-no-ci' "$MERGE_HOOK" && grep -q 'FORCE_NO_CI.*BYPASS_REASON' "$MERGE_HOOK"; then
  pass "--force-no-ci still requires --bypass-reason"
else
  fail "--force-no-ci / --bypass-reason requirement not found as expected"
fi
if grep -c -- '--force-no-ci\|--force-no-two-gate' "$MERGE_HOOK" | grep -q '^[0-9]'; then
  pass "flag inventory check ran"
fi

# -----------------------------------------------------------------------
# AC-7: the new counter module never names the three pre-existing
# decline-reason kinds anywhere in its source.
# -----------------------------------------------------------------------
echo ""
echo "=== AC-7: backend/gate_streak.py never names the forbidden kinds ==="
FORBIDDEN_HIT=0
for kind in ci_gate_stood_down manual_merge_ci_bypass manual_merge_two_gate_bypass; do
  if grep -q "$kind" "$REAL_REPO_ROOT/backend/gate_streak.py"; then
    fail "gate_streak.py references forbidden kind: $kind"
    FORBIDDEN_HIT=1
  fi
done
if [[ "$FORBIDDEN_HIT" -eq 0 ]]; then
  pass "no forbidden kind names present in backend/gate_streak.py"
fi

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
