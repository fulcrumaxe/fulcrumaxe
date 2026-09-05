#!/usr/bin/env bash
# tests/test_ci_status_check.sh — Unit tests for scripts/lib/ci-status-check.sh (D#1614)
#
# Run: bash tests/test_ci_status_check.sh
# Expects: all assertions pass, exit 0
#
# Follows this repo's existing plain-bash test-script convention (see
# tests/test_two_gate_check.sh, the direct sibling of this lib) rather than
# bats — no .bats runner is wired into this repo's actual test flow outside
# tests/test_coldstart.bats, and mirroring the sibling lib's own test style
# keeps this consistent with what's really run day to day.
#
# Uses CI_STATUS_OVERRIDE_<PR> / CI_STATUS_HEAD_SHA_<PR> / CI_PR_FILES_<PR> /
# CI_PROVENANCE_BLOCKED_<disc> env vars to supply fixture data without making
# real GitHub API calls.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CI_LIB="$REAL_REPO_ROOT/scripts/lib/ci-status-check.sh"

# D#1944: check_ci_status now reads the CI_DISABLED repo variable before it
# fetches anything else. Every test below that is not ABOUT the kill switch
# pins that read to "authoritatively absent" (HTTP 404) through the test seam,
# so the whole suite still makes zero GitHub API calls. Tests that ARE about
# the switch override these two locally and restore them afterwards.
export CI_STATUS_TEST_MODE=1
export CI_KILL_SWITCH_OVERRIDE=HTTP_404
# D#2271 PR-a: check_ci_status's STATUS=pass branch now writes an audit row
# (ci_write_audit) on every green result — several tests below reach that
# branch (the ALL_GREEN fixtures). Pin CI_STATUS_TEST_AUDIT_FILE globally so
# none of that lands in the real audit trail; _ci_audit_path only honours it
# with CI_STATUS_TEST_MODE=1, which is already set above.
export CI_STATUS_TEST_AUDIT_FILE="$(mktemp -t ci-status-check-tests.XXXXXX)"
trap 'rm -f "$CI_STATUS_TEST_AUDIT_FILE"' EXIT

PASS=0
FAIL=0

assert_exit_0() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then echo "  PASS: $label (exit 0)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 0, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_exit_1() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 1 ]; then echo "  PASS: $label (exit 1)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 1, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_reason_empty() {
  local label="$1" actual="$2" line
  line="$(printf '%s\n' "$actual" | grep '^REASON:' || true)"
  if [ "$line" = "REASON:" ]; then
    echo "  PASS: $label"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label — expected an empty reason, got: $line"; FAIL=$((FAIL + 1))
  fi
}

assert_exit_2() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 2 ]; then echo "  PASS: $label (exit 2)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 2, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_exit_9() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 9 ]; then echo "  PASS: $label (exit 9)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 9, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_contains() {
  local label="$1" expected_substr="$2" actual="$3"
  if printf '%s' "$actual" | grep -qF "$expected_substr"; then
    echo "  PASS: $label"; PASS=$((PASS + 1));
  else
    echo "  FAIL: $label"; echo "        expected to contain: $expected_substr"; echo "        actual: $actual"
    FAIL=$((FAIL + 1))
  fi
}

assert_not_contains() {
  local label="$1" bad_substr="$2" actual="$3"
  if printf '%s' "$actual" | grep -qF "$bad_substr"; then
    echo "  FAIL: $label"; echo "        should NOT contain: $bad_substr"; echo "        actual: $actual"
    FAIL=$((FAIL + 1))
  else
    echo "  PASS: $label"; PASS=$((PASS + 1))
  fi
}

_gha() { printf '{"name":"%s","status":"completed","conclusion":"%s","app":{"slug":"github-actions"},"html_url":"%s"}' "$1" "$2" "${3:-}"; }

ALL_GREEN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' success)"']'

# -----------------------------------------------------------------------
# CS-1 (AC-1): lib exists, exposes check_ci_status, sourced not inlined
# -----------------------------------------------------------------------
echo "=== CS-1: lib file + function contract ==="
if [ -f "$CI_LIB" ] && grep -q 'check_ci_status' "$CI_LIB"; then
  echo "  PASS: lib file exists and defines check_ci_status"; PASS=$((PASS + 1))
else
  echo "  FAIL: lib file missing or check_ci_status not defined"; FAIL=$((FAIL + 1))
fi
for f in "$REAL_REPO_ROOT/scripts/merge-and-hook.sh" "$REAL_REPO_ROOT/scripts/loop-phased-step5.sh"; do
  if grep -qE 'source.*ci-status-check\.sh|\. .*ci-status-check\.sh' "$f"; then
    echo "  PASS: $(basename "$f") sources ci-status-check.sh"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $(basename "$f") does not source ci-status-check.sh"; FAIL=$((FAIL + 1))
  fi
done

# No naive success-substring grep anywhere in the lib (AC-6).
if grep -qE 'grep\s+(-\w+\s+)*.?success' "$CI_LIB"; then
  echo "  FAIL: naive 'grep success' pattern found in $CI_LIB"; FAIL=$((FAIL + 1))
else
  echo "  PASS: no naive 'grep success' pattern in $CI_LIB"; PASS=$((PASS + 1))
fi

# No set -x / --verbose around gh calls (AC-14).
if grep -qE 'set -x|--verbose' "$CI_LIB"; then
  echo "  FAIL: set -x / --verbose found in $CI_LIB (token leak risk)"; FAIL=$((FAIL + 1))
else
  echo "  PASS: no set -x / --verbose in $CI_LIB"; PASS=$((PASS + 1))
fi

# Bounded wait — no unbounded while true / until success (AC-9).
if grep -qE 'while true|until\s+.*success' "$CI_LIB"; then
  echo "  FAIL: unbounded loop construct found in $CI_LIB"; FAIL=$((FAIL + 1))
else
  echo "  PASS: no unbounded loop construct in $CI_LIB (fixed max-iteration bound)"; PASS=$((PASS + 1))
fi

# -----------------------------------------------------------------------
# Helper: run check_ci_status in a clean subshell
# -----------------------------------------------------------------------
_run_status() {
  local pr="$1"; shift
  (
    source "$CI_LIB"
    check_ci_status "$pr" "test-owner/test-repo" "$@"
    rc=$?
    echo "RC:$rc"
    echo "STATE:${CI_STATUS_STATE:-}"
    echo "REASON:${CI_STATUS_FAIL_REASON:-}"
    echo "FAILING:${CI_STATUS_FAILING_CHECKS:-}"
    echo "URL:${CI_STATUS_RUN_URL:-}"
    exit "$rc"
  )
}

# -----------------------------------------------------------------------
# CS-2 (AC-2): all four checks green -> pass
# -----------------------------------------------------------------------
echo ""
echo "=== CS-2: all required checks green -> exit 0 ==="
export CI_STATUS_OVERRIDE_20001="$ALL_GREEN"
export CI_STATUS_HEAD_SHA_20001="deadbeef01"
OUT=$(_run_status 20001); RC=$?
assert_exit_0 "CS-2: all-green PR passes" "$RC"
unset CI_STATUS_OVERRIDE_20001 CI_STATUS_HEAD_SHA_20001

# -----------------------------------------------------------------------
# CS-3 (AC-2/AC-3): backend (import-smoke) fails -> blocked, named in FAILING
# (this is the exact #1610 incident shape: everything but backend is green)
# -----------------------------------------------------------------------
echo ""
echo "=== CS-3: backend (import-smoke) fails -> blocked ==="
BAD='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' failure 'https://github.com/x/y/actions/runs/1')"']'
export CI_STATUS_OVERRIDE_20002="$BAD"
export CI_STATUS_HEAD_SHA_20002="deadbeef02"
OUT=$(_run_status 20002); RC=$?
assert_exit_1 "CS-3: mixed pass/fail blocked" "$RC"
assert_contains "CS-3: FAILING names backend (import-smoke)" "backend (import-smoke)" "$OUT"
assert_contains "CS-3: URL surfaced" "actions/runs/1" "$OUT"
unset CI_STATUS_OVERRIDE_20002 CI_STATUS_HEAD_SHA_20002

# -----------------------------------------------------------------------
# CS-4 (AC-4): required check deleted/renamed (absent) -> blocked, not silently pass
# -----------------------------------------------------------------------
echo ""
echo "=== CS-4: required check absent (job deleted) -> blocked ==="
MISSING='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"']'
export CI_STATUS_OVERRIDE_20003="$MISSING"
export CI_STATUS_HEAD_SHA_20003="deadbeef03"
OUT=$(_run_status 20003); RC=$?
assert_exit_1 "CS-4: absent required check blocked" "$RC"
assert_contains "CS-4: reason names absent check" "backend (import-smoke)" "$OUT"
unset CI_STATUS_OVERRIDE_20003 CI_STATUS_HEAD_SHA_20003

# -----------------------------------------------------------------------
# CS-5 (AC-5): spoofed third-party app posts success under a required name;
# the real github-actions run for that name is failing/absent -> still blocked
# -----------------------------------------------------------------------
echo ""
echo "=== CS-5: spoofed third-party check-run not honored ==="
SPOOF_NAME="backend (import-smoke)"
SPOOFED='{"name":"'"$SPOOF_NAME"'","status":"completed","conclusion":"success","app":{"slug":"some-third-party-app"},"html_url":""}'
FAKE_GREEN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$SPOOFED"']'
export CI_STATUS_OVERRIDE_20004="$FAKE_GREEN"
export CI_STATUS_HEAD_SHA_20004="deadbeef04"
OUT=$(_run_status 20004); RC=$?
assert_exit_1 "CS-5: spoofed app-slug check-run rejected, real gate still blocks" "$RC"
assert_contains "CS-5: absent-required reason (spoofed run filtered out)" "backend (import-smoke)" "$OUT"
unset CI_STATUS_OVERRIDE_20004 CI_STATUS_HEAD_SHA_20004

# -----------------------------------------------------------------------
# CS-6 (AC-6): fail-closed parsing — empty array, pending, and gh error
# -----------------------------------------------------------------------
echo ""
echo "=== CS-6a: empty check-run array -> blocked (pending, never pass) ==="
export CI_STATUS_OVERRIDE_20005="[]"
export CI_STATUS_HEAD_SHA_20005="deadbeef05"
OUT=$(_run_status 20005); RC=$?
assert_exit_1 "CS-6a: empty array blocked" "$RC"
unset CI_STATUS_OVERRIDE_20005 CI_STATUS_HEAD_SHA_20005

echo ""
echo "=== CS-6b: a required check still in-progress (status != completed) -> blocked ==="
PENDING_RUN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"',{"name":"backend (import-smoke)","status":"in_progress","conclusion":null,"app":{"slug":"github-actions"},"html_url":""}]'
export CI_STATUS_OVERRIDE_20006="$PENDING_RUN"
export CI_STATUS_HEAD_SHA_20006="deadbeef06"
OUT=$(_run_status 20006); RC=$?
assert_exit_1 "CS-6b: in-progress required check blocked, not merged" "$RC"
unset CI_STATUS_OVERRIDE_20006 CI_STATUS_HEAD_SHA_20006

echo ""
echo "=== CS-6c: simulated gh api error -> hard block ==="
export CI_STATUS_OVERRIDE_20007="GH_API_ERROR"
export CI_STATUS_HEAD_SHA_20007="deadbeef07"
OUT=$(_run_status 20007); RC=$?
assert_exit_1 "CS-6c: gh api error fails closed" "$RC"
unset CI_STATUS_OVERRIDE_20007 CI_STATUS_HEAD_SHA_20007

# -----------------------------------------------------------------------
# CS-7 (AC-7): zero-checks-on-fresh-head grace — --wait mode does not exit 0
# -----------------------------------------------------------------------
echo ""
echo "=== CS-7: --wait mode on all-empty override never exits 0 (bounded timeout) ==="
export CI_STATUS_OVERRIDE_20008="[]"
export CI_STATUS_HEAD_SHA_20008="deadbeef08"
export CI_MAX_WAIT_SECONDS=2
export CI_POLL_INTERVAL=1
OUT=$(_run_status 20008 --wait); RC=$?
assert_exit_1 "CS-7: --wait never passes on zero check-runs" "$RC"
assert_contains "CS-7: timeout reason surfaced" "timed out" "$OUT"
unset CI_STATUS_OVERRIDE_20008 CI_STATUS_HEAD_SHA_20008 CI_MAX_WAIT_SECONDS CI_POLL_INTERVAL

# -----------------------------------------------------------------------
# CS-8 (AC-15): provenance ordering — external PR touching workflows/** not
# auto-trusted until the D#1588 intake-approved human gate clears
# -----------------------------------------------------------------------
echo ""
echo "=== CS-8: provenance:external PR touching .github/workflows/** not auto-trusted ==="
_run_provenance() {
  local pr="$1" disc="$2"
  (
    source "$CI_LIB"
    check_ci_provenance_gate "$pr" "test-owner/test-repo" "$disc"
    rc=$?
    echo "REASON:${CI_STATUS_FAIL_REASON:-}"
    exit "$rc"
  )
}
export CI_PR_FILES_20009=".github/workflows/ci.yml
scripts/foo.sh"
export CI_PROVENANCE_BLOCKED_9001="yes"
OUT=$(_run_provenance 20009 9001); RC=$?
assert_exit_1 "CS-8: workflow-touching external PR blocked pending intake-approved" "$RC"
assert_contains "CS-8: reason cites D#1588 intake gate" "intake-approved" "$OUT"
unset CI_PR_FILES_20009 CI_PROVENANCE_BLOCKED_9001

echo ""
echo "=== CS-8b: PR that does not touch workflows/** is unaffected by provenance gate ==="
export CI_PR_FILES_20010="scripts/foo.sh
README.md"
export CI_PROVENANCE_BLOCKED_9002="yes"
OUT=$(_run_provenance 20010 9002); RC=$?
assert_exit_0 "CS-8b: non-workflow-touching PR passes provenance gate" "$RC"
unset CI_PR_FILES_20010 CI_PROVENANCE_BLOCKED_9002

# -----------------------------------------------------------------------
# CS-9 (AC-8): SHA-pinned merge + 409 head-moved re-gate
# -----------------------------------------------------------------------
echo ""
echo "=== CS-9a: ci_merge_sha_pinned echo mode carries the pinned SHA ==="
_run_merge() {
  local pr="$1" sha="$2"
  (
    source "$CI_LIB"
    ci_merge_sha_pinned "$pr" "test-owner/test-repo" "$sha"
    rc=$?
    echo "REASON:${CI_STATUS_FAIL_REASON:-}"
    exit "$rc"
  )
}
export CI_MERGE_MODE=echo
OUT=$(_run_merge 30001 deadbeef99 2>&1); RC=$?
assert_exit_0 "CS-9a: echo-mode merge succeeds" "$RC"
assert_contains "CS-9a: pinned sha present in merge args" "sha=deadbeef99" "$OUT"
unset CI_MERGE_MODE

echo ""
echo "=== CS-9b: simulated 409 (head moved) -> exit 9, merge NOT completed, re-gate required ==="
export CI_MERGE_MODE=conflict
OUT=$(_run_merge 30002 stale-sha 2>&1); RC=$?
assert_exit_9 "CS-9b: conflict mode returns 9 (re-gate signal), not 0" "$RC"
assert_contains "CS-9b: reason mentions head moved" "head moved" "$OUT"
unset CI_MERGE_MODE

# -----------------------------------------------------------------------
# CS-10 (AC-13): durable audit row on a CI-gate block
# -----------------------------------------------------------------------
echo ""
echo "=== CS-10: ci_write_audit writes a durable ci_gate_block row ==="
AUDIT_TMP="$(mktemp)"
(
  source "$CI_LIB"
  export CI_STATUS_TEST_AUDIT_FILE="$AUDIT_TMP"
  ci_write_audit "ci_gate_block" 40001 "abc123" "backend (import-smoke)" "https://x/y/1" "required check(s) failed"
)
if [ -s "$AUDIT_TMP" ] && grep -q '"kind": "ci_gate_block"' "$AUDIT_TMP"; then
  echo "  PASS: audit row written with kind=ci_gate_block"; PASS=$((PASS + 1))
else
  echo "  FAIL: audit row missing or malformed"; echo "  content: $(cat "$AUDIT_TMP" 2>/dev/null)"; FAIL=$((FAIL + 1))
fi
rm -f "$AUDIT_TMP"

# -----------------------------------------------------------------------
# CS-11 (AC-12): --force-no-ci bypass writes an audited manual_merge_ci_bypass row
# (exercised at the merge-and-hook.sh level — see tests/test_merge_and_hook_ci_gate.sh)
# -----------------------------------------------------------------------
echo ""
echo "=== CS-11: ci_write_audit supports manual_merge_ci_bypass kind ==="
AUDIT_TMP2="$(mktemp)"
(
  source "$CI_LIB"
  export CI_STATUS_TEST_AUDIT_FILE="$AUDIT_TMP2"
  ci_write_audit "manual_merge_ci_bypass" 40002 "abc456" "" "" "operator judgment call: GitHub outage"
)
assert_contains "CS-11: bypass audit row present" '"kind": "manual_merge_ci_bypass"' "$(cat "$AUDIT_TMP2")"
rm -f "$AUDIT_TMP2"

# ═══════════════════════════════════════════════════════════════════════════
# D#1944 — the gate stands down explicitly when CI_DISABLED='true'.
#
# The failure this guards against is subtle: making the three matrix
# check-run names register as `skipped` would ALSO unblock merges, because
# the evaluator accepts `skipped` as green. That turns a loud, correct
# refusal into a silent unconditional pass with zero code tested. So the
# assertions below are about `disabled` being a state of its own that no
# check-run input can produce, and about a failed read never becoming
# either answer.
# ═══════════════════════════════════════════════════════════════════════════

# A set missing exactly the three matrix names — the live shape of this bug.
MISSING_MATRIX='['"$(_gha 'backend (import-smoke)' success)"']'
# All four present and skipped — what "fixing the matrix names" would produce.
ALL_SKIPPED='['"$(_gha tui skipped)"','"$(_gha dashboard skipped)"','"$(_gha ts-backend skipped)"','"$(_gha 'backend (import-smoke)' skipped)"']'

# -----------------------------------------------------------------------
# CS-12 (AC-1): `disabled` is a distinct status with its own exit code, and
# is reachable from ANY check-run input — including an all-green one, which
# is how we prove it is derived from the variable and not from the checks.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-12: CI_DISABLED=true -> STATE=disabled, rc=2 (not 0, not 1) ==="
export CI_KILL_SWITCH_OVERRIDE=true
export CI_STATUS_OVERRIDE_20101="$ALL_GREEN"
export CI_STATUS_HEAD_SHA_20101="deadbeef11"
OUT=$(_run_status 20101); RC=$?
assert_exit_2 "CS-12a: all-green input still returns rc=2, not 0" "$RC"
assert_contains "CS-12a: STATE is exactly disabled" "STATE:disabled" "$OUT"
assert_not_contains "CS-12a: STATE is never laundered to pass" "STATE:pass" "$OUT"
assert_reason_empty "CS-12a: reason is empty on a stand-down (nothing failed)" "$OUT"
unset CI_STATUS_OVERRIDE_20101 CI_STATUS_HEAD_SHA_20101

export CI_STATUS_OVERRIDE_20102="[]"
export CI_STATUS_HEAD_SHA_20102="deadbeef12"
OUT=$(_run_status 20102); RC=$?
assert_exit_2 "CS-12b: empty check-run set also returns rc=2" "$RC"
assert_contains "CS-12b: STATE is exactly disabled" "STATE:disabled" "$OUT"
unset CI_STATUS_OVERRIDE_20102 CI_STATUS_HEAD_SHA_20102
export CI_KILL_SWITCH_OVERRIDE=HTTP_404

# -----------------------------------------------------------------------
# CS-13 (AC-2): four outcomes on three code paths. Run against a check-run
# set MISSING the three matrix names, so a wrong fallthrough shows up as a
# state change rather than being masked.
#
# Then the three read-FAILURE rows are repeated against an all-`success`
# set. That repeat is the non-vacuous half: if a read failure were quietly
# treated as "CI is on", a green set would hide it completely.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-13: variable-read outcome table (against a missing-matrix set) ==="
_ks_row() {
  local label="$1" override="$2" want_rc="$3" want_state="$4" want_reason="$5" runs="$6" pr="$7"
  export CI_KILL_SWITCH_OVERRIDE="$override"
  export "CI_STATUS_OVERRIDE_${pr}=$runs"
  export "CI_STATUS_HEAD_SHA_${pr}=deadbeef${pr}"
  local out rc
  out=$(_run_status "$pr"); rc=$?
  if [ "$rc" -eq "$want_rc" ]; then
    echo "  PASS: $label rc=$want_rc"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label expected rc=$want_rc, got $rc"; FAIL=$((FAIL + 1))
  fi
  assert_contains "$label STATE=$want_state" "STATE:$want_state" "$out"
  if [ -n "$want_reason" ]; then
    assert_contains "$label reason names the cause" "$want_reason" "$out"
  else
    assert_reason_empty "$label reason is empty" "$out"
  fi
  unset "CI_STATUS_OVERRIDE_${pr}" "CI_STATUS_HEAD_SHA_${pr}"
}

_ks_row "CS-13/true:"        true         2 disabled ""                                 "$MISSING_MATRIX" 20111
_ks_row "CS-13/HTTP_404:"    HTTP_404     1 fail     "required check absent"            "$MISSING_MATRIX" 20112
_ks_row "CS-13/false:"       false        1 fail     "required check absent"            "$MISSING_MATRIX" 20113
_ks_row "CS-13/HTTP_403:"    HTTP_403     1 fail     "could not determine CI_DISABLED state" "$MISSING_MATRIX" 20114
_ks_row "CS-13/HTTP_500:"    HTTP_500     1 fail     "could not determine CI_DISABLED state" "$MISSING_MATRIX" 20115
_ks_row "CS-13/GH_API_ERROR:" GH_API_ERROR 1 fail    "could not determine CI_DISABLED state" "$MISSING_MATRIX" 20116

echo ""
echo "=== CS-13b: the three read-failure rows again, against an ALL-GREEN set ==="
_ks_row "CS-13b/HTTP_403:"    HTTP_403     1 fail "could not determine CI_DISABLED state" "$ALL_GREEN" 20121
_ks_row "CS-13b/HTTP_500:"    HTTP_500     1 fail "could not determine CI_DISABLED state" "$ALL_GREEN" 20122
_ks_row "CS-13b/GH_API_ERROR:" GH_API_ERROR 1 fail "could not determine CI_DISABLED state" "$ALL_GREEN" 20123
export CI_KILL_SWITCH_OVERRIDE=HTTP_404

# -----------------------------------------------------------------------
# CS-14 (AC-3): the two consumers agree on the string, byte for byte.
# ci.yml evaluates `vars.CI_DISABLED != 'true'` — case-sensitive, untrimmed.
# Only the exact byte string `true` may produce a stand-down here, or the
# workflow and the gate can disagree about whether CI ran.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-14: only the exact byte string 'true' yields disabled ==="
_PR=20130
for v in "true" "True" "TRUE" "1" "yes" "on" " true " ""; do
  _PR=$((_PR + 1))
  export CI_KILL_SWITCH_OVERRIDE="$v"
  export "CI_STATUS_OVERRIDE_${_PR}=$ALL_GREEN"
  export "CI_STATUS_HEAD_SHA_${_PR}=deadbeef${_PR}"
  OUT=$(_run_status "$_PR"); RC=$?
  if [ "$v" = "true" ]; then
    assert_contains "CS-14: value '$v' -> disabled" "STATE:disabled" "$OUT"
  else
    assert_not_contains "CS-14: value '$v' -> NOT disabled" "STATE:disabled" "$OUT"
  fi
  unset "CI_STATUS_OVERRIDE_${_PR}" "CI_STATUS_HEAD_SHA_${_PR}"
done
export CI_KILL_SWITCH_OVERRIDE=HTTP_404

# -----------------------------------------------------------------------
# CS-15 (AC-4): the seam is inert in production.
#
# This is the security-expert's disqualifier under test: "if ci_gate_stood_down
# can fire for any reason other than the repo variable authoritatively reading
# 'true' — a read failure defaulting open, an env override, unset-treated-as-
# true — then it is --force-no-ci with a nicer name."
#
# With CI_STATUS_TEST_MODE unset, CI_KILL_SWITCH_OVERRIDE=true must buy
# nothing: the lib does the real read, the stubbed CLI fails, and that is a
# hard block — not a stand-down.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-15: CI_KILL_SWITCH_OVERRIDE is inert without CI_STATUS_TEST_MODE=1 ==="
STUB_DIR="$(mktemp -d)"
printf '#!/usr/bin/env bash\nexit 127\n' > "$STUB_DIR/gh"
chmod +x "$STUB_DIR/gh"
OUT=$(
  env -u CI_STATUS_TEST_MODE \
      PATH="$STUB_DIR:$PATH" \
      CI_KILL_SWITCH_OVERRIDE=true \
  bash -c '
    source "'"$CI_LIB"'"
    check_ci_status 20141 "test-owner/test-repo"
    rc=$?
    echo "RC:$rc"
    echo "STATE:${CI_STATUS_STATE:-}"
    echo "REASON:${CI_STATUS_FAIL_REASON:-}"
    exit "$rc"
  '
); RC=$?
assert_exit_1 "CS-15: env override alone does not stand the gate down" "$RC"
assert_not_contains "CS-15: STATE is not disabled" "STATE:disabled" "$OUT"
assert_contains "CS-15: reason is the unknown-read block" "could not determine CI_DISABLED state" "$OUT"
rm -rf "$STUB_DIR"

# -----------------------------------------------------------------------
# CS-15b (AC-1/AC-2/AC-3, R4): the three newly-gated seams are inert without
# CI_STATUS_TEST_MODE=1, called directly rather than through check_ci_status.
#
# CS-15 above used to export a per-PR head-SHA mock and check-runs override
# for PR 20141 alongside its kill-switch assertion, but the kill switch
# blocks first (:273 runs before either fetch seam is reached), so those two
# exports were already dead before this change and would have become
# actively misleading post-gate. R4 replaces them with this block, which
# calls the seams directly. Run under `env -u CI_STATUS_TEST_MODE` because
# line 27 exports it suite-wide.
#
# Capped at exactly three assertions (cost-analyst's verification budget) —
# one per newly-gated seam, not a mutation sweep.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-15b: head-SHA / check-runs / audit-path seams are inert without CI_STATUS_TEST_MODE=1 ==="
STUB_DIR2="$(mktemp -d)"
printf '#!/usr/bin/env bash\nexit 127\n' > "$STUB_DIR2/gh"
chmod +x "$STUB_DIR2/gh"

SHA_OUT=$(
  env -u CI_STATUS_TEST_MODE PATH="$STUB_DIR2:$PATH" CI_STATUS_HEAD_SHA_20142="cafebabe42" \
    bash -c 'source "'"$CI_LIB"'"; _ci_fetch_head_sha 20142 "test-owner/test-repo"' 2>/dev/null
)
assert_not_contains "CS-15b: head-SHA mock is inert (AC-1)" "cafebabe42" "$SHA_OUT"

RUNS_OUT=$(
  env -u CI_STATUS_TEST_MODE PATH="$STUB_DIR2:$PATH" CI_STATUS_OVERRIDE_20142="$ALL_GREEN" \
    bash -c 'source "'"$CI_LIB"'"; _ci_fetch_check_runs_json 20142 "test-owner/test-repo" cafebabe42' 2>/dev/null
)
assert_not_contains "CS-15b: check-runs mock is inert (AC-2)" "$ALL_GREEN" "$RUNS_OUT"

AUDIT_OUT=$(
  env -u CI_STATUS_TEST_MODE CI_STATUS_TEST_AUDIT_FILE=/dev/null \
    bash -c 'source "'"$CI_LIB"'"; _ci_audit_path' 2>/dev/null
); AUDIT_RC=$?
if [ "$AUDIT_RC" -eq 0 ] && [ "$AUDIT_OUT" != "/dev/null" ] && [ -n "$AUDIT_OUT" ] && \
   [[ "$AUDIT_OUT" == *audit.jsonl ]]; then
  echo "  PASS: CS-15b: audit-path redirect is inert and falls back, not fails (AC-3)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: CS-15b: audit-path redirect is inert and falls back, not fails (AC-3)"
  echo "        rc=$AUDIT_RC out=[$AUDIT_OUT]"
  FAIL=$((FAIL + 1))
fi
rm -rf "$STUB_DIR2"

# -----------------------------------------------------------------------
# CS-16 (AC-5): absent-and-skipped checks with CI ENABLED do not stand down.
#
# Deliberately asserts `!= disabled`, NOT `!= pass`. Today an all-skipped
# required set returns pass — that is a separate, known hole (a PR can
# suppress its own required checks with a false job-level `if:`), and
# tightening it here would make merging stricter in the same change that is
# meant to unblock it. This pins the boundary this change owns and nothing
# more.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-16: all-skipped checks with CI enabled are not a stand-down ==="
export CI_KILL_SWITCH_OVERRIDE=HTTP_404
export CI_STATUS_OVERRIDE_20151="$ALL_SKIPPED"
export CI_STATUS_HEAD_SHA_20151="deadbeef51"
OUT=$(_run_status 20151); RC=$?
assert_not_contains "CS-16: STATE is not disabled (only the variable can do that)" "STATE:disabled" "$OUT"
unset CI_STATUS_OVERRIDE_20151 CI_STATUS_HEAD_SHA_20151

# -----------------------------------------------------------------------
# CS-17 (AC-11): this change must not touch what the gate requires. The four
# required names and the accept set are out of scope by design — widening
# either one is what would turn the block into a silent pass.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-17: required check names and accept set are unchanged ==="
(
  source "$CI_LIB"
  expected=("tui" "dashboard" "ts-backend" "backend (import-smoke)")
  if [ "${#CI_REQUIRED_CHECKS[@]}" -eq 4 ] && [ "${CI_REQUIRED_CHECKS[*]}" = "${expected[*]}" ]; then
    exit 0
  fi
  exit 1
)
if [ $? -eq 0 ]; then
  echo "  PASS: CI_REQUIRED_CHECKS is byte-identical to its pre-change value"; PASS=$((PASS + 1))
else
  echo "  FAIL: CI_REQUIRED_CHECKS changed"; FAIL=$((FAIL + 1))
fi
if grep -qF 'not in ("success", "skipped")' "$CI_LIB"; then
  echo "  PASS: the conclusion accept set is untouched (out of scope here)"; PASS=$((PASS + 1))
else
  echo "  FAIL: the conclusion accept set changed — that tightening is a separate, sequenced change"; FAIL=$((FAIL + 1))
fi
if python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" "$REAL_REPO_ROOT/.github/workflows/ci.yml" 2>/dev/null; then
  echo "  PASS: .github/workflows/ci.yml still parses as YAML"; PASS=$((PASS + 1))
else
  echo "  FAIL: .github/workflows/ci.yml does not parse as YAML"; FAIL=$((FAIL + 1))
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
