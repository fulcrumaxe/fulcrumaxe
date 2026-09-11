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
  if printf '%s' "$actual" | grep -qF -- "$bad_substr"; then
    echo "  FAIL: $label"; echo "        should NOT contain: $bad_substr"; echo "        actual: $actual"
    FAIL=$((FAIL + 1))
  else
    echo "  PASS: $label"; PASS=$((PASS + 1))
  fi
}

_gha() { printf '{"name":"%s","status":"completed","conclusion":"%s","app":{"slug":"github-actions"},"html_url":"%s"}' "$1" "$2" "${3:-}"; }

# D#2318 added four names to CI_REQUIRED_CHECKS (preflight, publish denylist,
# PR link policy, PR mutation evidence). `missing` outranks every other bucket
# in _ci_evaluate, so any fixture meant to exercise a DIFFERENT bucket has to
# carry a green entry for these four too, or the four newly-required-but-absent
# names silently take over the test. _D2318_GREEN is that green entry set,
# spliced into every such fixture below rather than duplicated by hand.
_D2318_GREEN="$(_gha 'preflight (always-on gates)' success)"','"$(_gha 'publish denylist' success)"','"$(_gha 'PR link policy' success)"','"$(_gha 'PR mutation evidence' success)"
_D2318_SKIPPED="$(_gha 'preflight (always-on gates)' skipped)"','"$(_gha 'publish denylist' skipped)"','"$(_gha 'PR link policy' skipped)"','"$(_gha 'PR mutation evidence' skipped)"

ALL_GREEN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' success)"','"$_D2318_GREEN"','"$(_gha 'open-source export audit' success)"']'

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
BAD='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' failure 'https://github.com/x/y/actions/runs/1')"','"$_D2318_GREEN"','"$(_gha 'open-source export audit' success)"']'
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
MISSING='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$_D2318_GREEN"','"$(_gha 'open-source export audit' success)"']'
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
FAKE_GREEN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$SPOOFED"','"$_D2318_GREEN"','"$(_gha 'open-source export audit' success)"']'
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
PENDING_RUN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"',{"name":"backend (import-smoke)","status":"in_progress","conclusion":null,"app":{"slug":"github-actions"},"html_url":""},'"$_D2318_GREEN"','"$(_gha 'open-source export audit' success)"']'
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
# The other non-matrix required checks (D#2318 added four more alongside
# "backend (import-smoke)") register normally, so all of them are present.
MISSING_MATRIX='['"$(_gha 'backend (import-smoke)' success)"','"$_D2318_GREEN"','"$(_gha 'open-source export audit' success)"']'
# All required names present and skipped — what "fixing the matrix names"
# would produce. D#2318's four additions are included here too: this fixture
# means "every required check registered but did not run", and after D#2318
# that is eight names, not four.
ALL_SKIPPED='['"$(_gha tui skipped)"','"$(_gha dashboard skipped)"','"$(_gha ts-backend skipped)"','"$(_gha 'backend (import-smoke)' skipped)"','"$_D2318_SKIPPED"','"$(_gha 'open-source export audit' skipped)"']'

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
# This used to assert `!= disabled` and deliberately NOT `!= pass`, because an
# all-skipped required set really did return pass and tightening it inside
# D#1944 would have made merging stricter in the change meant to unblock it.
# D#1987 is the sequenced change that closes that hole, so the `!= pass` half
# is no longer withheld: both are asserted here now, and the full three-state
# behaviour is CS-18 below.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-16: all-skipped checks with CI enabled are not a stand-down ==="
export CI_KILL_SWITCH_OVERRIDE=HTTP_404
export CI_STATUS_OVERRIDE_20151="$ALL_SKIPPED"
export CI_STATUS_HEAD_SHA_20151="deadbeef51"
OUT=$(_run_status 20151); RC=$?
assert_not_contains "CS-16: STATE is not disabled (only the variable can do that)" "STATE:disabled" "$OUT"
assert_not_contains "CS-16: STATE is not pass either (D#1987 — skipped is not green)" "STATE:pass" "$OUT"
assert_exit_1 "CS-16: an all-skipped required set blocks the merge" "$RC"
unset CI_STATUS_OVERRIDE_20151 CI_STATUS_HEAD_SHA_20151

# -----------------------------------------------------------------------
# CS-17 (AC-11): pin the required set. Widening it unannounced is what would
# turn a real block into a silent pass, so any change to the array has to come
# through this assertion — that part is unchanged.
#
# What changed (D#2456) is how the assertion is written. It used to restate the
# array as a literal and check the count. A restated literal is only ever an
# assertion about the moment someone wrote it: whoever adds a sixth name edits
# the literal in the same commit, the count moves with it, and the test goes on
# passing without anyone having argued for the addition. That is exactly how
# "open-source export audit" arrived (D#1989) and then sat in the required list
# meaning nothing on the plane where PRs merge.
#
# So the pin is a DIFFERENCE now. `baseline` below is the array as D#1989 left
# it — a frozen historical record, not a statement of what it ought to be — and
# the live array has to equal that baseline with exactly one *named* element
# removed, in order. One element, named, nothing else moved. A sixth name fails
# here on the length comparison; a reordering fails on the positional one; and
# neither can be absorbed by editing a list to match.
#
# D#2318 extends the same difference rather than replacing it: the live array
# now has to equal (D#1989 baseline minus "open-source export audit") PLUS
# four NAMED additions, in order. Each addition is listed individually so a
# later change that widens the count without arguing for a specific name still
# fails here, the same way a silently-removed name would have.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-17: required set is the D#1989 baseline minus one, plus the D#2318 additions ==="
CS17_REMOVED="open-source export audit"
CS17_ADDED=("publish denylist" "preflight (always-on gates)" "PR link policy" "PR mutation evidence")
(
  source "$CI_LIB"
  # The array exactly as D#1989 left it. Do not "update" this to match a new
  # value — it is the fixed point the difference is measured from.
  baseline=("tui" "dashboard" "ts-backend" "backend (import-smoke)" "open-source export audit")

  expected=()
  for n in "${baseline[@]}"; do
    if [ "$n" != "$CS17_REMOVED" ]; then expected+=("$n"); fi
  done
  expected+=("${CS17_ADDED[@]}")

  if [ $(( ${#baseline[@]} - 1 + ${#CS17_ADDED[@]} )) -ne "${#expected[@]}" ]; then
    echo "        baseline does not contain '$CS17_REMOVED' exactly once" >&2
    exit 1
  fi
  if [ "${#CI_REQUIRED_CHECKS[@]}" -ne "${#expected[@]}" ]; then
    echo "        live array has ${#CI_REQUIRED_CHECKS[@]} names; baseline-minus-one-plus-additions has ${#expected[@]}" >&2
    exit 1
  fi
  i=0
  while [ "$i" -lt "${#expected[@]}" ]; do
    if [ "${CI_REQUIRED_CHECKS[$i]}" != "${expected[$i]}" ]; then
      echo "        position $i: live '${CI_REQUIRED_CHECKS[$i]}' != expected '${expected[$i]}'" >&2
      exit 1
    fi
    i=$(( i + 1 ))
  done
  exit 0
)
if [ $? -eq 0 ]; then
  echo "  PASS: CI_REQUIRED_CHECKS is the D#1989 baseline minus '$CS17_REMOVED', plus the D#2318 additions, in order"; PASS=$((PASS + 1))
else
  echo "  FAIL: CI_REQUIRED_CHECKS is not the D#1989 baseline minus that one name plus exactly the D#2318 additions"; FAIL=$((FAIL + 1))
fi
(
  source "$CI_LIB"
  for n in "${CI_REQUIRED_CHECKS[@]}"; do
    if [ "$n" = "$CS17_REMOVED" ]; then exit 1; fi
  done
  exit 0
)
if [ $? -eq 0 ]; then
  echo "  PASS: '$CS17_REMOVED' is the element that came out"; PASS=$((PASS + 1))
else
  echo "  FAIL: '$CS17_REMOVED' is still a required check name"; FAIL=$((FAIL + 1))
fi
(
  source "$CI_LIB"
  for n in "${CS17_ADDED[@]}"; do
    found=0
    for live in "${CI_REQUIRED_CHECKS[@]}"; do
      if [ "$live" = "$n" ]; then found=1; break; fi
    done
    if [ "$found" -ne 1 ]; then
      echo "        D#2318 addition '$n' is not in the live array" >&2
      exit 1
    fi
  done
  exit 0
)
if [ $? -eq 0 ]; then
  echo "  PASS: all four D#2318 additions are present in CI_REQUIRED_CHECKS"; PASS=$((PASS + 1))
else
  echo "  FAIL: at least one D#2318 addition is missing from CI_REQUIRED_CHECKS"; FAIL=$((FAIL + 1))
fi
# D#1987 inverted this assertion. It used to require the accept set to still
# read `not in ("success", "skipped")` — a deliberate hold saying "the
# tightening is a separate, sequenced change". This IS that change, so the pin
# now points the other way and holds the tightening in place: `skipped` must
# never reappear beside `success`. Kept as a source-level pin (the behaviour is
# covered by CS-18) because a future edit could re-widen the accept set without
# any single behavioural test obviously going red.
if grep -qF 'not in ("success", "skipped")' "$CI_LIB"; then
  echo "  FAIL: 'skipped' is back in the conclusion accept set — a required check that did not run would read as green again (D#1987)"; FAIL=$((FAIL + 1))
else
  echo "  PASS: 'skipped' is not in the conclusion accept set"; PASS=$((PASS + 1))
fi
if python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" "$REAL_REPO_ROOT/.github/workflows/ci.yml" 2>/dev/null; then
  echo "  PASS: .github/workflows/ci.yml still parses as YAML"; PASS=$((PASS + 1))
else
  echo "  FAIL: .github/workflows/ci.yml does not parse as YAML"; FAIL=$((FAIL + 1))
fi

# ═══════════════════════════════════════════════════════════════════════════
# D#1987 — a required check that did not run is not a required check that
# passed.
#
# The hole: `skipped` sat in the evaluator's accept set beside `success`. For
# `pull_request`, GitHub runs the workflow definition from the PR's HEAD, so a
# PR that keeps the required job names but adds any false job-level `if:`
# produces correctly-named `completed/skipped` check-runs, the gate returns 0,
# and the PR merges itself through the gate it just created with zero code
# tested.
#
# The distinction that matters, and the reason this is safe to tighten: a JOB
# skipped by its own job-level `if:` registers a check-run whose conclusion is
# `skipped` — that is the laundering path closed here. A STEP skipped inside a
# job that ran does not: the job's own conclusion is `success`, which this
# change does not touch and cannot see.
# ═══════════════════════════════════════════════════════════════════════════

_reason_of() { printf '%s\n' "$1" | grep '^REASON:' | head -1; }

# -----------------------------------------------------------------------
# CS-18 (SEC-1): failed / absent / skipped are three states, not two.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-18: a skipped required check blocks, distinctly from failed and absent ==="
export CI_KILL_SWITCH_OVERRIDE=HTTP_404

# 18a — every required name completed/skipped. This is the self-suppression
# shape, and before D#1987 it returned rc=0/STATE=pass.
export CI_STATUS_OVERRIDE_20161="$ALL_SKIPPED"
export CI_STATUS_HEAD_SHA_20161="deadbeef61"
OUT_SKIPPED=$(_run_status 20161); RC=$?
assert_exit_1 "CS-18a: all-skipped required set is blocked" "$RC"
assert_contains "CS-18a: STATE is the distinct 'skipped' token" "STATE:skipped" "$OUT_SKIPPED"
assert_not_contains "CS-18a: STATE is never laundered to pass" "STATE:pass" "$OUT_SKIPPED"
# D#2456 took "open-source export audit" out of the required set, so it is no
# longer a name this assertion could look for. Asserted per required name
# instead of against one chosen name — which is what "every skipped name" was
# claiming anyway, and it now actually checks it. D#2318's four additions are
# required names too, so they belong in this loop the same as the original four.
_FAILING_SKIPPED="$(printf '%s\n' "$OUT_SKIPPED" | grep '^FAILING:' | head -1)"
for _n in tui dashboard ts-backend 'backend (import-smoke)' 'preflight (always-on gates)' 'publish denylist' 'PR link policy' 'PR mutation evidence'; do
  assert_contains "CS-18a: skipped name '$_n' is surfaced in FAILING" "$_n" "$_FAILING_SKIPPED"
done
unset CI_STATUS_OVERRIDE_20161 CI_STATUS_HEAD_SHA_20161

# 18b — a single skipped name among the green ones. The realistic shape: one
# job turned off, not the whole workflow. (D#2456: the skipped name here used
# to be "open-source export audit"; it is a required name that carries this
# case, and that one is no longer required.)
ONE_SKIPPED='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' skipped)"','"$_D2318_GREEN"']'
export CI_STATUS_OVERRIDE_20162="$ONE_SKIPPED"
export CI_STATUS_HEAD_SHA_20162="deadbeef62"
OUT=$(_run_status 20162); RC=$?
assert_exit_1 "CS-18b: one skipped name among green ones is still blocked" "$RC"
assert_contains "CS-18b: STATE is skipped" "STATE:skipped" "$OUT"
assert_contains "CS-18b: the reason names the one check that did not run" "backend (import-smoke)" "$OUT"
unset CI_STATUS_OVERRIDE_20162 CI_STATUS_HEAD_SHA_20162

# 18c — the three reasons are asserted to differ FROM EACH OTHER, not to equal
# three hardcoded strings. A literal assertion keeps passing forever if two of
# these later converge on the same text, which is the exact defect (three
# causes collapsing into one operator-visible string) one layer up.
FAILED_SET='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' failure 'https://x/y/runs/9')"','"$_D2318_GREEN"','"$(_gha 'open-source export audit' success)"']'
ABSENT_SET='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$_D2318_GREEN"','"$(_gha 'open-source export audit' success)"']'

export CI_STATUS_OVERRIDE_20163="$FAILED_SET"; export CI_STATUS_HEAD_SHA_20163="deadbeef63"
OUT_FAILED=$(_run_status 20163); RC_FAILED=$?
unset CI_STATUS_OVERRIDE_20163 CI_STATUS_HEAD_SHA_20163

export CI_STATUS_OVERRIDE_20164="$ABSENT_SET"; export CI_STATUS_HEAD_SHA_20164="deadbeef64"
OUT_ABSENT=$(_run_status 20164); RC_ABSENT=$?
unset CI_STATUS_OVERRIDE_20164 CI_STATUS_HEAD_SHA_20164

R_SKIPPED="$(_reason_of "$OUT_SKIPPED")"
R_FAILED="$(_reason_of "$OUT_FAILED")"
R_ABSENT="$(_reason_of "$OUT_ABSENT")"

echo "  observed reason (failed):  $R_FAILED"
echo "  observed reason (absent):  $R_ABSENT"
echo "  observed reason (skipped): $R_SKIPPED"

for _pair in "skipped/absent:$R_SKIPPED:$R_ABSENT" "skipped/failed:$R_SKIPPED:$R_FAILED" "absent/failed:$R_ABSENT:$R_FAILED"; do
  _label="${_pair%%:*}"; _rest="${_pair#*:}"; _a="${_rest%%:*}"; _b="${_rest#*:}"
  if [ -n "$_a" ] && [ -n "$_b" ] && [ "$_a" != "$_b" ]; then
    echo "  PASS: CS-18c: reasons differ ($_label)"; PASS=$((PASS + 1))
  else
    echo "  FAIL: CS-18c: reasons for $_label are identical or empty"
    echo "        a: $_a"; echo "        b: $_b"; FAIL=$((FAIL + 1))
  fi
done
assert_exit_1 "CS-18c: the failed set blocks" "$RC_FAILED"
assert_exit_1 "CS-18c: the absent set blocks" "$RC_ABSENT"

# 18e — a mixed red+skipped set reports BOTH causes. The red one outranks the
# skipped ones for STATE and the head of REASON, but outranking is not
# replacing: dropping the skipped names would collapse two distinct causes into
# one report inside the very change that exists to stop that, and the surviving
# cause would look complete to whoever read it.
echo ""
echo "=== CS-18e: a mixed failed+skipped set surfaces both causes ==="
MIXED_RED_SKIPPED='['"$(_gha tui failure 'https://x/y/runs/7')"','"$(_gha dashboard skipped)"','"$(_gha ts-backend skipped)"','"$(_gha 'backend (import-smoke)' success)"','"$_D2318_GREEN"']'
export CI_STATUS_OVERRIDE_20165="$MIXED_RED_SKIPPED"
export CI_STATUS_HEAD_SHA_20165="deadbeef65"
OUT=$(_run_status 20165); RC=$?
assert_exit_1 "CS-18e: mixed red+skipped set is blocked" "$RC"
assert_contains "CS-18e: STATE stays fail (red outranks skipped)" "STATE:fail" "$OUT"
assert_contains "CS-18e: REASON leads with the red check" "required check(s) failed: tui" "$OUT"
# The two skipped names must reach BOTH operator-visible fields. Asserted per
# name against each field separately, rather than against one whole formatted
# string, so a later wording change cannot quietly drop a name while still
# matching the assertion.
_FAILING_LINE="$(printf '%s\n' "$OUT" | grep '^FAILING:' | head -1)"
_REASON_LINE="$(printf '%s\n' "$OUT" | grep '^REASON:' | head -1)"
for _n in dashboard ts-backend; do
  assert_contains "CS-18e: FAILING carries the skipped name '$_n'" "$_n" "$_FAILING_LINE"
  assert_contains "CS-18e: REASON names the skipped check '$_n'" "$_n" "$_REASON_LINE"
done
unset CI_STATUS_OVERRIDE_20165 CI_STATUS_HEAD_SHA_20165

# 18d — the conclusions the D#1987 body measured as ALREADY correct stay
# correct. This item exists to prove the change did not disturb them, and to
# keep them distinct from the new `skipped` state.
echo ""
echo "=== CS-18d: cancelled / timed_out / neutral / stale still fail, and are not 'skipped' ==="
_pr=20170
for _c in cancelled timed_out neutral stale; do
  _pr=$((_pr + 1))
  _D2318_SAME_C="$(_gha 'preflight (always-on gates)' "$_c")"','"$(_gha 'publish denylist' "$_c")"','"$(_gha 'PR link policy' "$_c")"','"$(_gha 'PR mutation evidence' "$_c")"
  _SET='['"$(_gha tui "$_c")"','"$(_gha dashboard "$_c")"','"$(_gha ts-backend "$_c")"','"$(_gha 'backend (import-smoke)' "$_c")"','"$_D2318_SAME_C"','"$(_gha 'open-source export audit' "$_c")"']'
  export "CI_STATUS_OVERRIDE_${_pr}=$_SET"
  export "CI_STATUS_HEAD_SHA_${_pr}=deadbeef${_pr}"
  OUT=$(_run_status "$_pr"); RC=$?
  assert_exit_1 "CS-18d/$_c: still blocked" "$RC"
  assert_contains "CS-18d/$_c: STATE is fail, not skipped" "STATE:fail" "$OUT"
  unset "CI_STATUS_OVERRIDE_${_pr}" "CI_STATUS_HEAD_SHA_${_pr}"
done

# -----------------------------------------------------------------------
# CS-19 (SEC-2): the kill switch and a skipped check never trade places.
#
# `disabled` is reached before any check-run is fetched, so it cannot be
# produced by check-run input; `skipped` is derived only from check-run input,
# so it cannot be produced by the variable. Asserting both directions is what
# keeps a future edit from letting CI_DISABLED='true' launder into a pass.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-19: CI_DISABLED and a skipped check are independent states ==="
_sec2_row() { # label, kill-switch, want_rc, want_state
  local label="$1" ks="$2" want_rc="$3" want_state="$4" pr="$5"
  export CI_KILL_SWITCH_OVERRIDE="$ks"
  export "CI_STATUS_OVERRIDE_${pr}=$ALL_SKIPPED"
  export "CI_STATUS_HEAD_SHA_${pr}=deadbeef${pr}"
  local out rc
  out=$(_run_status "$pr"); rc=$?
  if [ "$rc" -eq "$want_rc" ]; then
    echo "  PASS: $label rc=$want_rc"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label expected rc=$want_rc, got $rc"; FAIL=$((FAIL + 1))
  fi
  assert_contains "$label STATE=$want_state" "STATE:$want_state" "$out"
  unset "CI_STATUS_OVERRIDE_${pr}" "CI_STATUS_HEAD_SHA_${pr}"
}
# Switch OFF (false, and authoritatively absent): a skipped required check has
# no legitimate explanation left, so it blocks.
_sec2_row "CS-19/false + all-skipped:"    false    1 skipped  20181
_sec2_row "CS-19/HTTP_404 + all-skipped:" HTTP_404 1 skipped  20182
# Switch ON: still the D#1944 stand-down, exit 2, unchanged. A PR that turns
# this into a pass fails D#1987's Spec explicitly.
_sec2_row "CS-19/true + all-skipped:"     true     2 disabled 20183
export CI_KILL_SWITCH_OVERRIDE=HTTP_404

# -----------------------------------------------------------------------
# CS-20 (SEC-3): while CI is switched off, a PR touching .github/workflows/**
# is blocked regardless of provenance — and that block self-removes when the
# switch goes off. Both halves are asserted: a blanket refusal to edit
# workflows would pass the first assertion and fail the second.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-20: workflow edits are blocked while CI_DISABLED='true' ==="
_prov_row() { # label, want_rc, files, kill-switch, provenance-blocked, pr, disc
  local label="$1" want_rc="$2" files="$3" ks="$4" prov="$5" pr="$6" disc="$7"
  export CI_KILL_SWITCH_OVERRIDE="$ks"
  export "CI_PR_FILES_${pr}=$files"
  export "CI_PROVENANCE_BLOCKED_${disc}=$prov"
  local rc=0
  ( source "$CI_LIB"; check_ci_provenance_gate "$pr" "test-owner/test-repo" "$disc" ) >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq "$want_rc" ]; then
    echo "  PASS: $label (exit $want_rc)"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label expected exit $want_rc, got $rc"; FAIL=$((FAIL + 1))
  fi
  unset "CI_PR_FILES_${pr}" "CI_PROVENANCE_BLOCKED_${disc}"
}
_WF_FILES=$'.github/workflows/ci.yml\nscripts/lib/ci-status-check.sh'
_NO_WF_FILES=$'scripts/lib/ci-status-check.sh\ntests/test_ci_status_check.sh'

# Switch ON — the new block. "no" is an internal PR: the pre-existing
# provenance gate would have cleared it, which is what makes this row prove the
# block is independent of provenance rather than riding on it.
_prov_row "CS-20a: internal PR touching workflows/** is blocked while CI is off" \
          1 "$_WF_FILES" true no 40011 8011
# Scoped, not blanket: the same switch state, a PR touching no workflow file.
_prov_row "CS-20b: internal PR touching no workflow file is unaffected" \
          0 "$_NO_WF_FILES" true no 40012 8012
# Self-removal: the identical workflow-touching PR once CI is back on.
_prov_row "CS-20c: the block self-removes when CI_DISABLED is not 'true'" \
          0 "$_WF_FILES" false no 40013 8013
# The pre-existing D#1588 external block is undisturbed in both switch states.
_prov_row "CS-20d: provenance:external + workflows/** still blocked (switch off)" \
          1 "$_WF_FILES" false yes 40014 8014
_prov_row "CS-20e: provenance:external touching no workflow file still passes" \
          0 "$_NO_WF_FILES" false yes 40015 8015
export CI_KILL_SWITCH_OVERRIDE=HTTP_404

# ═══════════════════════════════════════════════════════════════════════════
# CS-21 (D#2456) — the workflow half and the array half are ONE change.
#
# Removing "open-source export audit" from CI_REQUIRED_CHECKS is only correct
# because ci.yml's export-audit job no longer registers a check-run where its
# repository condition is false. Put that condition back on the steps and the
# job concludes `success` on every plane again — the original defect, now minus
# the required-list entry that made it visible. So 21a pins where the condition
# lives, and 21b/21c run the same fixture on each side of the array edit.
#
# Two shapes are covered, because the change was planned around a prediction
# that turned out to be wrong and the correction is worth keeping visible:
#
#   21b/21c — the audit check-run is registered with `conclusion: skipped`.
#             This is what a job-level `if:` actually produces here, measured
#             on run 34067010184. It lands in `did_not_run` (D#1987).
#   21d/21e — no audit check-run at all. This is what the change was planned
#             expecting, and it is what deleting the job outright would give.
#             It lands in `missing`.
#
# Both are a hard block while the name is required, and neither is once it is
# not. The prediction being wrong changed which bucket does the blocking; it
# did not change the reason the two edits cannot be sequenced.
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "=== CS-21a: the export-audit repository guard is job-level, not step-level ==="
if python3 - "$REAL_REPO_ROOT/.github/workflows/ci.yml" <<'PYEOF'
import sys, yaml

job = yaml.safe_load(open(sys.argv[1]))["jobs"]["export-audit"]
cond = str(job.get("if", ""))
problems = []
if "github.repository" not in cond:
    problems.append("job-level if: does not test github.repository")
if "CI_DISABLED" not in cond:
    problems.append("job-level if: dropped the CI_DISABLED kill switch")
# The `env` context is not available when a job-level `if:` is evaluated, so a
# guard written as env.FOO resolves empty and skips the job everywhere --
# including where the audit is supposed to run. Catch that spelling here; on
# every plane but the engine it is indistinguishable from the correct one.
if "env." in cond:
    problems.append("job-level if: reads the env context, which is unavailable there")
for step in job.get("steps") or []:
    if "github.repository" in str(step.get("if", "")):
        problems.append("step %r still carries its own repository guard" % step.get("name"))
for p in problems:
    print("        " + p, file=sys.stderr)
sys.exit(1 if problems else 0)
PYEOF
then
  echo "  PASS: export-audit is guarded once, on the job, so its check-run says skipped and not success elsewhere"; PASS=$((PASS + 1))
else
  echo "  FAIL: export-audit's repository guard is not where the required-list edit assumes"; FAIL=$((FAIL + 1))
fi

# What a head here really produces once the job's `if:` is false: the required
# names green, and the audit check-run registered as `skipped`. Copied from the
# observed shape of run 34067010184, not imagined.
SKIPPED_AUDIT_RUN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' success)"','"$_D2318_GREEN"','"$(_gha 'open-source export audit' skipped)"']'
# The shape the change was planned around, and the shape deleting the job would
# give: no audit check-run at all.
NO_AUDIT_RUN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' success)"','"$_D2318_GREEN"']'

# _cs21_required_still_has_audit <pr> — run the gate with the pre-D#2456
# required set restored. Not a hypothetical: this is the state the repo would
# be in had only the workflow half landed.
_cs21_old_required() {
  (
    source "$CI_LIB"
    CI_REQUIRED_CHECKS=("tui" "dashboard" "ts-backend" "backend (import-smoke)" "open-source export audit")
    check_ci_status "$1" "test-owner/test-repo"
    rc=$?
    echo "RC:$rc"
    echo "STATE:${CI_STATUS_STATE:-}"
    echo "REASON:${CI_STATUS_FAIL_REASON:-}"
    exit "$rc"
  )
}

echo ""
echo "=== CS-21b: skipped audit check-run + name still required -> blocked ==="
export CI_STATUS_OVERRIDE_20191="$SKIPPED_AUDIT_RUN"
export CI_STATUS_HEAD_SHA_20191="deadbeef91"
OUT=$(_cs21_old_required 20191); RC=$?
assert_exit_1 "CS-21b: half the change blocks every merge, as designed" "$RC"
assert_contains "CS-21b: STATE is the did-not-run token, not fail" "STATE:skipped" "$OUT"
assert_contains "CS-21b: reason names the check that did not run" "open-source export audit" "$OUT"
unset CI_STATUS_OVERRIDE_20191 CI_STATUS_HEAD_SHA_20191

echo ""
echo "=== CS-21c: skipped audit check-run + name removed -> the same head merges ==="
export CI_STATUS_OVERRIDE_20192="$SKIPPED_AUDIT_RUN"
export CI_STATUS_HEAD_SHA_20192="deadbeef92"
OUT=$(_run_status 20192); RC=$?
assert_exit_0 "CS-21c: with both halves landed, a skipped audit check-run is not a block" "$RC"
assert_contains "CS-21c: STATE is pass" "STATE:pass" "$OUT"
unset CI_STATUS_OVERRIDE_20192 CI_STATUS_HEAD_SHA_20192

echo ""
echo "=== CS-21d: absent audit check-run + name still required -> blocked ==="
export CI_STATUS_OVERRIDE_20193="$NO_AUDIT_RUN"
export CI_STATUS_HEAD_SHA_20193="deadbeef93"
OUT=$(_cs21_old_required 20193); RC=$?
assert_exit_1 "CS-21d: the absent shape blocks too, by a different bucket" "$RC"
assert_contains "CS-21d: reason names the check that is not there" "open-source export audit" "$OUT"
unset CI_STATUS_OVERRIDE_20193 CI_STATUS_HEAD_SHA_20193

echo ""
echo "=== CS-21e: absent audit check-run + name removed -> the same head merges ==="
export CI_STATUS_OVERRIDE_20194="$NO_AUDIT_RUN"
export CI_STATUS_HEAD_SHA_20194="deadbeef94"
OUT=$(_run_status 20194); RC=$?
assert_exit_0 "CS-21e: an absent audit check-run is not a block either" "$RC"
assert_contains "CS-21e: STATE is pass" "STATE:pass" "$OUT"
unset CI_STATUS_OVERRIDE_20194 CI_STATUS_HEAD_SHA_20194

# -----------------------------------------------------------------------
# CS-22 (D#2463): two fail-open paths in the duplicate-name / empty-required
# handling. CS-22a/b reproduce the exact rollup shape measured on a real PR
# (fulcrumaxe/fulcrumaxe PR #155): three required names each posted twice by
# two separate workflow runs on the same head SHA. CS-22c covers the
# empty-required-set path, which needs a different seam (CI_REQUIRED_CHECKS
# itself, not a check-runs override) since it is never empty in production.
# -----------------------------------------------------------------------
echo ""
echo "=== CS-22a: duplicate required names, every duplicate green -> still passes ==="
DUP_ALL_GREEN='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' success)"','"$(_gha 'preflight (always-on gates)' success)"','"$(_gha 'publish denylist' success 'https://x/runs/new')"','"$(_gha 'publish denylist' success 'https://x/runs/old')"','"$(_gha 'PR link policy' success 'https://x/runs/new')"','"$(_gha 'PR link policy' success 'https://x/runs/old')"','"$(_gha 'PR mutation evidence' success 'https://x/runs/new')"','"$(_gha 'PR mutation evidence' success 'https://x/runs/old')"']'
export CI_STATUS_OVERRIDE_20195="$DUP_ALL_GREEN"
export CI_STATUS_HEAD_SHA_20195="deadbeef95"
OUT=$(_run_status 20195); RC=$?
assert_exit_0 "CS-22a: three required names duplicated, every copy green, still passes" "$RC"
unset CI_STATUS_OVERRIDE_20195 CI_STATUS_HEAD_SHA_20195

echo ""
echo "=== CS-22b: duplicate required name, one copy failing -> now blocked regardless of which copy is last ==="
# Reproduces the exact defect: 'PR link policy' posted twice, one failing
# copy and one successful copy. The successful copy is LAST in the array —
# the position the old 'last occurrence wins' logic trusted — so this fixture
# is the shape that used to fail OPEN (pass) and must now fail CLOSED.
DUP_ONE_FAILING='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' success)"','"$(_gha 'preflight (always-on gates)' success)"','"$(_gha 'publish denylist' success)"','"$(_gha 'PR mutation evidence' success)"','"$(_gha 'PR link policy' failure 'https://x/runs/failed-one')"','"$(_gha 'PR link policy' success 'https://x/runs/success-one')"']'
export CI_STATUS_OVERRIDE_20196="$DUP_ONE_FAILING"
export CI_STATUS_HEAD_SHA_20196="deadbeef96"
OUT=$(_run_status 20196); RC=$?
assert_exit_1 "CS-22b: a failing duplicate blocks even though a green duplicate is last in the array" "$RC"
assert_contains "CS-22b: FAILING names the duplicated check" "PR link policy" "$OUT"
unset CI_STATUS_OVERRIDE_20196 CI_STATUS_HEAD_SHA_20196

echo ""
echo "=== CS-22c: empty required-checks set -> fails closed and loud, never silently passes ==="
DUP_ALL_GREEN_20197="$DUP_ALL_GREEN"
export CI_STATUS_OVERRIDE_20197="$DUP_ALL_GREEN_20197"
export CI_STATUS_HEAD_SHA_20197="deadbeef97"
OUT=$(
  (
    source "$CI_LIB"
    CI_REQUIRED_CHECKS=()
    check_ci_status 20197 "test-owner/test-repo"
    rc=$?
    echo "RC:$rc"
    echo "STATE:${CI_STATUS_STATE:-}"
    echo "REASON:${CI_STATUS_FAIL_REASON:-}"
    exit "$rc"
  )
)
RC=$?
assert_exit_1 "CS-22c: an empty CI_REQUIRED_CHECKS blocks instead of vacuously passing" "$RC"
assert_contains "CS-22c: reason says the required-checks list is empty" "required-checks list is empty" "$OUT"
assert_not_contains "CS-22c: never lands in STATE:pass" "STATE:pass" "$OUT"
unset CI_STATUS_OVERRIDE_20197 CI_STATUS_HEAD_SHA_20197

echo ""
echo "=== CS-22d: duplicate required name, failing copy LAST -> the other ordering also blocks ==="
# CS-22b already proves failure-first/success-last blocks. _bucket ranking is
# a min() over all entries for the name, so array position shouldn't matter —
# but that was exactly the previous code's bug (it mattered, silently). Prove
# the reverse ordering too, so "order-independent" is a checked fact rather
# than an implementation detail nobody is asserting on.
DUP_ONE_FAILING_REVERSED='['"$(_gha tui success)"','"$(_gha dashboard success)"','"$(_gha ts-backend success)"','"$(_gha 'backend (import-smoke)' success)"','"$(_gha 'preflight (always-on gates)' success)"','"$(_gha 'publish denylist' success)"','"$(_gha 'PR mutation evidence' success)"','"$(_gha 'PR link policy' success 'https://x/runs/success-one')"','"$(_gha 'PR link policy' failure 'https://x/runs/failed-one')"']'
export CI_STATUS_OVERRIDE_20198="$DUP_ONE_FAILING_REVERSED"
export CI_STATUS_HEAD_SHA_20198="deadbeef98"
OUT=$(_run_status 20198); RC=$?
assert_exit_1 "CS-22d: a failing duplicate blocks with the failing copy last too" "$RC"
assert_contains "CS-22d: FAILING names the duplicated check" "PR link policy" "$OUT"
unset CI_STATUS_OVERRIDE_20198 CI_STATUS_HEAD_SHA_20198

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
