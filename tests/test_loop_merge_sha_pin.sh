#!/usr/bin/env bash
# tests/test_loop_merge_sha_pin.sh — D#2124: the loop's unattended auto-merge
# pins --match-head-commit to the SHA the CI gate itself resolved, the same
# protection the manual path (ci-status-check.sh's ci_merge_sha_pinned)
# already has.
#
# Run: bash tests/test_loop_merge_sha_pin.sh
# Expects: all assertions pass, exit 0
#
# New file (not tests/test_merge_gate.sh) by deliberate Spec placement:
# D#2128 appended ~110 lines at tests/test_merge_gate.sh:544 and that file
# plus tests/test_loop_phased_step5.sh are being worked in parallel by
# D#2119 — this file stays out of both.
#
# What this file does NOT try to prove: that GitHub actually refuses a
# merge whose head moved. _gh_merge's GH_MERGE=echo mock always returns 0,
# so no local test can assert a refusal. That behaviour is GitHub's, and is
# already covered on the manual path by tests/test_ci_status_check.sh
# CS-9a/CS-9b (CI_MERGE_MODE=conflict -> exit 9). The honest boundary here
# is: the flag, carrying the gated SHA, reaches gh.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REAL_REPO_ROOT/scripts/loop-phased-step5.sh"
# shellcheck source=lib/blackboard-fixture.sh
source "$REAL_REPO_ROOT/tests/lib/blackboard-fixture.sh"
# D#2283: this suite used to write its pr_state fixtures straight through
# the in-repo blackboard symlink into production state. Redirect
# AUTONOMOUS_TEAM_STATE_DIR to a scratch dir for the life of this suite, and
# resolve the fixture dir through the helper so writes land there instead —
# on the clean path and on a crash alike.
blackboard_scratch_state_dir || {
  echo "FATAL: could not create scratch state dir" >&2
  exit 1
}
SCRATCH_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
trap 'rm -rf "$SCRATCH_STATE_DIR"' EXIT
BB_PR_STATE_DIR="$(blackboard_pr_state_dir "$REAL_REPO_ROOT")" || {
  echo "FATAL: could not resolve blackboard pr_state dir" >&2
  exit 1
}
PASS=0
FAIL=0

# -----------------------------------------------------------------------
# Test harness (same shape as tests/test_merge_gate.sh's, duplicated here
# rather than sourced — this file must not touch that one).
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

assert_contains() {
  local label="$1" expected_substr="$2" actual="$3"
  if echo "$actual" | grep -qF -- "$expected_substr"; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label"
    echo "        expected to contain: $expected_substr"
    echo "        actual output:"
    echo "$actual" | head -20 | sed 's/^/          /'
    FAIL=$((FAIL + 1))
  fi
}

assert_not_contains() {
  local label="$1" absent_substr="$2" actual="$3"
  if echo "$actual" | grep -qF -- "$absent_substr"; then
    echo "  FAIL: $label"
    echo "        expected NOT to contain: $absent_substr"
    echo "        actual output:"
    echo "$actual" | head -20 | sed 's/^/          /'
    FAIL=$((FAIL + 1))
  else
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  fi
}

# -----------------------------------------------------------------------
# Fixture helpers — same shape as tests/test_merge_gate.sh's.
# -----------------------------------------------------------------------
_make_config_file() {
  local tmpfile
  tmpfile=$(mktemp --suffix='.json')
  cat > "$tmpfile" <<'JSON'
{
  "gates": {
    "auto_merge": true,
    "security_review": false,
    "budget_check": false,
    "idea_generation": true,
    "stall_detection": false,
    "wiki_sync": false,
    "human_verification": false,
    "self_observe_executor": false,
    "self_observe_impl_coord": false,
    "docs_writer": false,
    "incident_commander": false,
    "release_manager": false,
    "runbook_writer": false,
    "phased_orchestration": true,
    "phased_code_review": true
  },
  "policies": {},
  "settings": {},
  "audit_log": []
}
JSON
  echo "$tmpfile"
}

# Accepts one or more discussion numbers; writes a SPEC_READY snapshot
# containing all of them so a single script invocation can drive multiple
# PRs through the merging phase in one pass (needed for the
# pass-survival case).
_write_snapshot_spec_ready_multi() {
  local path="$1"
  shift
  local discs_json
  discs_json=$(printf '%s\n' "$@" | python3 -c '
import json, sys
nums = [int(l) for l in sys.stdin if l.strip()]
print(json.dumps(nums))
')
  python3 -c "
import json
from datetime import datetime, timezone
nums = $discs_json
snap = {
    'discussions': [
        {
            'number': n,
            'title': f'Test feature {n}',
            'body': '<!-- STATUS:SPEC_READY --> some spec content'
        }
        for n in nums
    ],
    'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
}
json.dump(snap, open('$path', 'w'))
"
}

_write_pr_state_merging() {
  local pr_num="$1" disc_num="$2" needs_sec="${3:-False}"
  local bb_dir="$BB_PR_STATE_DIR"
  mkdir -p "$bb_dir"
  python3 -c "
import json
entry = {
    'value': {
        'pr': $pr_num,
        'discussion': $disc_num,
        'phase': 'merging',
        'spawned_phases': [],
        'completed_phases': [],
        'needs_security_review': $needs_sec,
        'fix_cycle_count': 0,
        'respawn_count': 0,
        'last_envelope': {},
        'blocked_reason': None,
        'created_at': '2026-01-01T00:00:00+00:00',
        'updated_at': '2026-01-01T00:00:00+00:00'
    },
    'version': 1,
    'updated_at': '2026-01-01T00:00:00+00:00',
    'updated_by': 'test'
}
json.dump(entry, open('$bb_dir/$pr_num.json', 'w'), indent=2)
"
}

_remove_pr_state_entry() {
  local pr_num="$1"
  rm -f "$BB_PR_STATE_DIR/$pr_num.json"
}

# -----------------------------------------------------------------------
# Test SP-1: pinned case — the CI gate resolved a head SHA, so the merge
# call must carry --match-head-commit followed by exactly that SHA.
# -----------------------------------------------------------------------
echo ""
echo "=== SP-1: gate resolved a SHA — merge call is pinned to it ==="
CFG_SP1=$(_make_config_file)
SNAP_SP1=$(mktemp --suffix='.json')
_write_snapshot_spec_ready_multi "$SNAP_SP1" 20201
_write_pr_state_merging 20200 20201 "False"

GATE_SHA_SP1="deadbeefcafe0123456789abcdef0123456789ab"

OUTPUT_SP1=$(AF_CONTROL_PLANE_CONFIG="$CFG_SP1" SNAPSHOT_PATH="$SNAP_SP1" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  CI_PASSED_SHA="$GATE_SHA_SP1" \
  HAS_LABEL_20200_code_review_passed=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_SP1=$?

assert_exit_0 "SP-1: exit code 0" "$RC_SP1"
assert_contains "SP-1: merge attempted" "GH_MERGE_ARGS" "$OUTPUT_SP1"
assert_contains "SP-1: pinned to the gated SHA" "--match-head-commit $GATE_SHA_SP1" "$OUTPUT_SP1"
assert_contains "SP-1: merged successfully logged" "merged successfully" "$OUTPUT_SP1"

_remove_pr_state_entry 20200
rm -f "$CFG_SP1" "$SNAP_SP1"

# -----------------------------------------------------------------------
# Test SP-2: unpinned case — the gate was green with no SHA (e.g. test
# mode's default, mirroring the D#1944 CI_DISABLED stand-down and the
# lib-failed-to-source path). The merge must still happen, must not
# carry --match-head-commit, and the run must log that it merged
# unpinned — skipping the merge would regress D#1944.
# -----------------------------------------------------------------------
echo ""
echo "=== SP-2: gate green with no SHA — merge PROCEEDS, unpinned, and says so ==="
CFG_SP2=$(_make_config_file)
SNAP_SP2=$(mktemp --suffix='.json')
_write_snapshot_spec_ready_multi "$SNAP_SP2" 20211
_write_pr_state_merging 20210 20211 "False"

OUTPUT_SP2=$(AF_CONTROL_PLANE_CONFIG="$CFG_SP2" SNAPSHOT_PATH="$SNAP_SP2" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_20210_code_review_passed=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_SP2=$?

assert_exit_0 "SP-2: exit code 0" "$RC_SP2"
assert_contains "SP-2: merge still attempted (skipping would regress D#1944)" "GH_MERGE_ARGS" "$OUTPUT_SP2"
assert_not_contains "SP-2: merge call does NOT carry --match-head-commit" "--match-head-commit" "$OUTPUT_SP2"
assert_contains "SP-2: run logs it merged unpinned" "merging unpinned" "$OUTPUT_SP2"
assert_contains "SP-2: merged successfully logged" "merged successfully" "$OUTPUT_SP2"

_remove_pr_state_entry 20210
rm -f "$CFG_SP2" "$SNAP_SP2"

# -----------------------------------------------------------------------
# Test SP-3: pass-survival case — with no gated SHA, a second PR later in
# the same pass is still processed. This is the assertion that proves an
# unpinned merge cannot take the whole pass down (the set -u unbound-
# variable abort risk the Spec is written against: loop-phased-step5.sh
# is set -uo pipefail with no errexit, and the merging phase runs inside
# the single discussions while-read loop, so an unguarded reference to an
# unset gated-SHA variable would stall every PR behind the first one, not
# just that one).
# -----------------------------------------------------------------------
echo ""
echo "=== SP-3: no gated SHA on PR #1 — PR #2 later in the pass still merges ==="
CFG_SP3=$(_make_config_file)
SNAP_SP3=$(mktemp --suffix='.json')
_write_snapshot_spec_ready_multi "$SNAP_SP3" 20221 20223
_write_pr_state_merging 20220 20221 "False"
_write_pr_state_merging 20222 20223 "False"

OUTPUT_SP3=$(AF_CONTROL_PLANE_CONFIG="$CFG_SP3" SNAPSHOT_PATH="$SNAP_SP3" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_20220_code_review_passed=yes \
  HAS_LABEL_20222_code_review_passed=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_SP3=$?

assert_exit_0 "SP-3: exit code 0 — the pass was not aborted" "$RC_SP3"
assert_not_contains "SP-3: no unbound variable abort" "unbound variable" "$OUTPUT_SP3"
assert_contains "SP-3: PR #1 (20220) merged unpinned" "D#20221 PR#20220: merging unpinned" "$OUTPUT_SP3"
assert_contains "SP-3: PR #2 (20222) reached (proves pass continued past PR #1)" "D#20223 PR#20222" "$OUTPUT_SP3"
assert_contains "SP-3: PR #2 (20222) also merged successfully" "D#20223 PR#20222: merged successfully" "$OUTPUT_SP3"

_remove_pr_state_entry 20220
_remove_pr_state_entry 20222
rm -f "$CFG_SP3" "$SNAP_SP3"

# -----------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
