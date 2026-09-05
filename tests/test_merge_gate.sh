#!/usr/bin/env bash
# tests/test_merge_gate.sh — Merge gate NACK-label tests (D#1007)
#
# Run: bash tests/test_merge_gate.sh
# Expects: all assertions pass, exit 0
#
# Tests the NACK label logic added in D#1007:
#   - Merge blocked when security-needs-fix + all pass-labels coexist
#   - Merge blocked when only security-issue (deprecated alias) present
#   - Merge proceeds when only pass-labels present (no NACK labels)
#   - Audit entry written on every merge attempt (pass or block)

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REAL_REPO_ROOT/scripts/loop-phased-step5.sh"
# shellcheck source=lib/blackboard-fixture.sh
source "$REAL_REPO_ROOT/tests/lib/blackboard-fixture.sh"
# D#2283: redirect AUTONOMOUS_TEAM_STATE_DIR to a scratch dir for the life of
# this suite, so pr_state fixtures and audit.jsonl writes land there instead
# of ~/.autonomous-forever-state — on the clean path and on a crash alike.
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

assert_contains() {
  local label="$1" expected_substr="$2" actual="$3"
  if echo "$actual" | grep -qF "$expected_substr"; then
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
  if echo "$actual" | grep -qF "$absent_substr"; then
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
# Fixture helpers
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

_write_snapshot_spec_ready() {
  local path="$1" disc_num="$2"
  python3 -c "
import json
from datetime import datetime, timezone
snap = {
    'discussions': [
        {
            'number': $disc_num,
            'title': 'Test feature $disc_num',
            'body': '<!-- STATUS:SPEC_READY --> some spec content'
        }
    ],
    # 'now', not a fixed date: loop-phased-step5 skips a snapshot past
    # MAX_AGE=600s, and these tests exist to exercise the fast path.
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
# Test MG-1: NACK label blocks merge even when all pass-labels present
#
# Scenario: PR has code-review-passed + acceptance-passed + security-review-passed
# AND security-needs-fix. The NACK check must fire before the pass-label
# check and refuse merge. GH_MERGE_ARGS must NOT appear in output.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-1: security-needs-fix + all pass-labels — merge BLOCKED ==="
CFG_MG1=$(_make_config_file)
SNAP_MG1=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG1" 10071
_write_pr_state_merging 10070 10071 "True"

OUTPUT_MG1=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG1" SNAPSHOT_PATH="$SNAP_MG1" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10070_code_review_passed=yes \
  HAS_LABEL_10070_acceptance_passed=yes \
  HAS_LABEL_10070_security_review_passed=yes \
  NACK_LABEL_10070_security_needs_fix=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG1=$?

assert_exit_0 "MG-1: exit code 0 when NACK blocks" "$RC_MG1"
assert_not_contains "MG-1: gh pr merge NOT called when NACK present" "GH_MERGE_ARGS" "$OUTPUT_MG1"
assert_contains "MG-1: NACK block message appears in log" "NACK label present" "$OUTPUT_MG1"
assert_contains "MG-1: specific NACK label named in message" "security-needs-fix" "$OUTPUT_MG1"
# Audit entry written for the blocked attempt
assert_contains "MG-1: audit entry written" "MERGE_AUDIT" "$OUTPUT_MG1"
assert_contains "MG-1: audit entry shows passed_nack_check=false" '"passed_nack_check": false' "$OUTPUT_MG1"

_remove_pr_state_entry 10070
rm -f "$CFG_MG1" "$SNAP_MG1"

# -----------------------------------------------------------------------
# Test MG-2: Deprecated security-issue label also blocks merge
#
# Scenario: PR has only security-issue (no pass-labels). The deprecated
# alias must still block merge — the NACK list includes both names.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-2: deprecated security-issue label alone — merge BLOCKED ==="
CFG_MG2=$(_make_config_file)
SNAP_MG2=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG2" 10073
_write_pr_state_merging 10072 10073 "False"

OUTPUT_MG2=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG2" SNAPSHOT_PATH="$SNAP_MG2" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  NACK_LABEL_10072_security_issue=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG2=$?

assert_exit_0 "MG-2: exit code 0 when deprecated NACK blocks" "$RC_MG2"
assert_not_contains "MG-2: gh pr merge NOT called for security-issue" "GH_MERGE_ARGS" "$OUTPUT_MG2"
assert_contains "MG-2: NACK block message appears" "NACK label present" "$OUTPUT_MG2"
assert_contains "MG-2: deprecated label named in block message" "security-issue" "$OUTPUT_MG2"

_remove_pr_state_entry 10072
rm -f "$CFG_MG2" "$SNAP_MG2"

# -----------------------------------------------------------------------
# Test MG-3: code-review-needs-fix blocks merge
# -----------------------------------------------------------------------
echo ""
echo "=== MG-3: code-review-needs-fix label — merge BLOCKED ==="
CFG_MG3=$(_make_config_file)
SNAP_MG3=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG3" 10075
_write_pr_state_merging 10074 10075 "False"

OUTPUT_MG3=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG3" SNAPSHOT_PATH="$SNAP_MG3" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  NACK_LABEL_10074_code_review_needs_fix=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG3=$?

assert_exit_0 "MG-3: exit code 0 when code-review-needs-fix blocks" "$RC_MG3"
assert_not_contains "MG-3: gh pr merge NOT called" "GH_MERGE_ARGS" "$OUTPUT_MG3"
assert_contains "MG-3: NACK block message appears" "NACK label present" "$OUTPUT_MG3"

_remove_pr_state_entry 10074
rm -f "$CFG_MG3" "$SNAP_MG3"

# -----------------------------------------------------------------------
# Test MG-4: do-not-merge label blocks merge
# -----------------------------------------------------------------------
echo ""
echo "=== MG-4: do-not-merge label — merge BLOCKED ==="
CFG_MG4=$(_make_config_file)
SNAP_MG4=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG4" 10077
_write_pr_state_merging 10076 10077 "False"

OUTPUT_MG4=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG4" SNAPSHOT_PATH="$SNAP_MG4" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  NACK_LABEL_10076_do_not_merge=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG4=$?

assert_exit_0 "MG-4: exit code 0 when do-not-merge blocks" "$RC_MG4"
assert_not_contains "MG-4: gh pr merge NOT called" "GH_MERGE_ARGS" "$OUTPUT_MG4"
assert_contains "MG-4: NACK block message appears" "NACK label present" "$OUTPUT_MG4"

_remove_pr_state_entry 10076
rm -f "$CFG_MG4" "$SNAP_MG4"

# -----------------------------------------------------------------------
# Test MG-5: No NACK labels + all pass-labels — merge proceeds
#
# Verify the happy path still works after adding the NACK check.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-5: no NACK labels + all pass-labels — merge PROCEEDS ==="
CFG_MG5=$(_make_config_file)
SNAP_MG5=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG5" 10079
_write_pr_state_merging 10078 10079 "False"

OUTPUT_MG5=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG5" SNAPSHOT_PATH="$SNAP_MG5" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10078_code_review_passed=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG5=$?

assert_exit_0 "MG-5: exit code 0 when no NACK present" "$RC_MG5"
# With code-review-passed present and no security needed, merge should proceed.
assert_contains "MG-5: merge proceeds when no NACK present" "GH_MERGE_ARGS" "$OUTPUT_MG5"
assert_not_contains "MG-5: no NACK block message on clean PR" "NACK label present" "$OUTPUT_MG5"
# Audit entry written with passed_nack_check=true
assert_contains "MG-5: audit entry shows passed_nack_check=true" '"passed_nack_check": true' "$OUTPUT_MG5"

_remove_pr_state_entry 10078
rm -f "$CFG_MG5" "$SNAP_MG5"

# -----------------------------------------------------------------------
# Test MG-6: wip label blocks merge
# -----------------------------------------------------------------------
echo ""
echo "=== MG-6: wip label — merge BLOCKED ==="
CFG_MG6=$(_make_config_file)
SNAP_MG6=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG6" 10081
_write_pr_state_merging 10080 10081 "False"

OUTPUT_MG6=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG6" SNAPSHOT_PATH="$SNAP_MG6" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  NACK_LABEL_10080_wip=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG6=$?

assert_exit_0 "MG-6: exit code 0 when wip blocks" "$RC_MG6"
assert_not_contains "MG-6: gh pr merge NOT called for wip" "GH_MERGE_ARGS" "$OUTPUT_MG6"
assert_contains "MG-6: NACK block message for wip" "NACK label present" "$OUTPUT_MG6"

_remove_pr_state_entry 10080
rm -f "$CFG_MG6" "$SNAP_MG6"

# -----------------------------------------------------------------------
# Test MG-7: acceptance-failed blocks merge
# -----------------------------------------------------------------------
echo ""
echo "=== MG-7: acceptance-failed label — merge BLOCKED ==="
CFG_MG7=$(_make_config_file)
SNAP_MG7=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG7" 10083
_write_pr_state_merging 10082 10083 "False"

OUTPUT_MG7=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG7" SNAPSHOT_PATH="$SNAP_MG7" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  NACK_LABEL_10082_acceptance_failed=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG7=$?

assert_exit_0 "MG-7: exit code 0 when acceptance-failed blocks" "$RC_MG7"
assert_not_contains "MG-7: gh pr merge NOT called for acceptance-failed" "GH_MERGE_ARGS" "$OUTPUT_MG7"
assert_contains "MG-7: NACK block message for acceptance-failed" "NACK label present" "$OUTPUT_MG7"

_remove_pr_state_entry 10082
rm -f "$CFG_MG7" "$SNAP_MG7"

# -----------------------------------------------------------------------
# Test MG-9: security-review-needs-fix synonym blocks merge even with all pass-labels
#
# Scenario: PR has code-review-passed + acceptance-passed + security-review-passed
# AND security-review-needs-fix. This synonym must be treated as a NACK
# signal — the merge must be blocked. GH_MERGE_ARGS must NOT appear.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-9: security-review-needs-fix + all pass-labels — merge BLOCKED ==="
CFG_MG9=$(_make_config_file)
SNAP_MG9=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG9" 10099
_write_pr_state_merging 10098 10099 "True"

OUTPUT_MG9=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG9" SNAPSHOT_PATH="$SNAP_MG9" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10098_code_review_passed=yes \
  HAS_LABEL_10098_acceptance_passed=yes \
  HAS_LABEL_10098_security_review_passed=yes \
  NACK_LABEL_10098_security_review_needs_fix=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG9=$?

assert_exit_0 "MG-9: exit code 0 when security-review-needs-fix blocks" "$RC_MG9"
assert_not_contains "MG-9: gh pr merge NOT called when security-review-needs-fix present" "GH_MERGE_ARGS" "$OUTPUT_MG9"
assert_contains "MG-9: NACK block message appears in log" "NACK label present" "$OUTPUT_MG9"
assert_contains "MG-9: specific NACK label named in message" "security-review-needs-fix" "$OUTPUT_MG9"
assert_contains "MG-9: audit entry written" "MERGE_AUDIT" "$OUTPUT_MG9"
assert_contains "MG-9: audit entry shows passed_nack_check=false" '"passed_nack_check": false' "$OUTPUT_MG9"

_remove_pr_state_entry 10098
rm -f "$CFG_MG9" "$SNAP_MG9"

# -----------------------------------------------------------------------
# Test MG-10 (D#1958): the exact label set CLAUDE.md's Merge Gate Protocol
# now names reaches the merging phase unblocked.
#
# Scenario: PR carries code-review-passed + security-review-passed +
# acceptance-passed. needs_security_review=True forces the conditional
# security check, so this also proves security-review-passed is the
# spelling the gate actually reads (D#1958's bare spelling is not).
# -----------------------------------------------------------------------
echo ""
echo "=== MG-10: CLAUDE.md's documented label set — merge PROCEEDS ==="
CFG_MG10=$(_make_config_file)
SNAP_MG10=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG10" 10101
_write_pr_state_merging 10100 10101 "True"

OUTPUT_MG10=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG10" SNAPSHOT_PATH="$SNAP_MG10" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10100_code_review_passed=yes \
  HAS_LABEL_10100_security_review_passed=yes \
  HAS_LABEL_10100_acceptance_passed=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG10=$?

assert_exit_0 "MG-10: exit code 0 when documented label set is present" "$RC_MG10"
assert_contains "MG-10: merge proceeds with code-review-passed + security-review-passed + acceptance-passed" "GH_MERGE_ARGS" "$OUTPUT_MG10"
assert_contains "MG-10: merged successfully logged" "merged successfully" "$OUTPUT_MG10"
assert_not_contains "MG-10: no NACK block on clean PR" "NACK label present" "$OUTPUT_MG10"

_remove_pr_state_entry 10100
rm -f "$CFG_MG10" "$SNAP_MG10"

# -----------------------------------------------------------------------
# Test MG-11 (D#1777): a force-push newer than code-review-passed
# invalidates the label — merge BLOCKED.
#
# Scenario: code-review-passed was applied, then the head was force-pushed
# afterward. The gate must not trust the label — it should be reported
# stale and removed before the code-review-passed check runs, so the
# merge blocks on "label missing", the same way it would if the label
# had never been applied.
#
# This also covers the code-review F1 finding: HAS_LABEL_10102_code_review_passed
# stays "yes" for the whole run below — it is never unset. The only reason
# _has_label reports it absent is _STALE_INVALIDATED_LABELS (set the
# instant staleness is detected, in _has_label itself). That's
# deliberate: it simulates remove_label's remote DELETE never landing
# (5xx / rate limit / token scope), and proves the merge decision doesn't
# wait on, or depend on, that call succeeding.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-11: code-review-passed earned before a force-push — merge BLOCKED ==="
CFG_MG11=$(_make_config_file)
SNAP_MG11=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG11" 10103
_write_pr_state_merging 10102 10103 "False"

OUTPUT_MG11=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG11" SNAPSHOT_PATH="$SNAP_MG11" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10102_code_review_passed=yes \
  LABEL_TS_10102_code_review_passed=2026-07-25T04:50:27Z \
  FORCE_PUSH_TS_10102=2026-07-28T06:02:53Z \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG11=$?

assert_exit_0 "MG-11: exit code 0 when stale label blocks" "$RC_MG11"
assert_contains "MG-11: stale label reported" "STALE_LABEL_REMOVED: pr=10102 label=code-review-passed" "$OUTPUT_MG11"
assert_not_contains "MG-11: gh pr merge NOT called on stale label" "GH_MERGE_ARGS" "$OUTPUT_MG11"
assert_contains "MG-11: merge blocked on missing code-review-passed" "code-review-passed label missing" "$OUTPUT_MG11"

_remove_pr_state_entry 10102
rm -f "$CFG_MG11" "$SNAP_MG11"

# -----------------------------------------------------------------------
# Test MG-12 (D#1777): negative direction — the label was (re-)earned
# after the force-push. Merge PROCEEDS, and nothing is reported stale.
#
# This is the flip side of MG-11 in the same mechanism: same PR shape,
# only the ordering of the two timestamps changes. A helper hard-wired to
# always call things stale would fail this case.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-12: code-review-passed (re-)earned after the force-push — merge PROCEEDS ==="
CFG_MG12=$(_make_config_file)
SNAP_MG12=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG12" 10105
_write_pr_state_merging 10104 10105 "False"

OUTPUT_MG12=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG12" SNAPSHOT_PATH="$SNAP_MG12" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10104_code_review_passed=yes \
  FORCE_PUSH_TS_10104=2026-07-28T06:02:53Z \
  LABEL_TS_10104_code_review_passed=2026-07-28T09:00:00Z \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG12=$?

assert_exit_0 "MG-12: exit code 0 when label postdates force-push" "$RC_MG12"
assert_not_contains "MG-12: nothing reported stale" "STALE_LABEL_REMOVED" "$OUTPUT_MG12"
assert_contains "MG-12: merge proceeds" "GH_MERGE_ARGS" "$OUTPUT_MG12"

_remove_pr_state_entry 10104
rm -f "$CFG_MG12" "$SNAP_MG12"

# -----------------------------------------------------------------------
# Test MG-13 (D#1777) — ORDERING, not exclusion. Renamed after a
# security-review finding: this test proves _check_nack_labels
# short-circuits the merging phase before _invalidate_stale_pass_labels
# ever runs (the call site at :1245 sits in the `else` branch of the NACK
# check), NOT that _REVIEW_PASS_LABELS excludes NACK labels. A mutation
# test confirmed this by instrumenting function entry: with
# acceptance-failed present, _invalidate_stale_pass_labels never executes
# for this PR at all — the merge is blocked one step earlier. The same
# mutation test then poisoned _REVIEW_PASS_LABELS with acceptance-failed
# and do-not-merge and this test still passed 47/47, because it was never
# exercising that array. That property (the two arrays stay disjoint) is
# what META-1 below actually checks — that's the fix for this finding,
# not a change to this test's assertions, which remain valid for what
# they do prove: a force-pushed PR carrying a NACK label is still blocked,
# via the NACK path, regardless of any force-push mock present.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-13 (ordering): NACK short-circuits merging before force-push invalidation ever runs ==="
CFG_MG13=$(_make_config_file)
SNAP_MG13=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG13" 10107
_write_pr_state_merging 10106 10107 "False"

OUTPUT_MG13=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG13" SNAPSHOT_PATH="$SNAP_MG13" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10106_code_review_passed=yes \
  NACK_LABEL_10106_acceptance_failed=yes \
  FORCE_PUSH_TS_10106=2026-07-28T06:02:53Z \
  LABEL_TS_10106_code_review_passed=2026-07-25T04:50:27Z \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG13=$?

assert_exit_0 "MG-13: exit code 0 when NACK blocks despite force-push" "$RC_MG13"
assert_not_contains "MG-13: gh pr merge NOT called" "GH_MERGE_ARGS" "$OUTPUT_MG13"
assert_contains "MG-13: NACK block message appears" "NACK label present" "$OUTPUT_MG13"
assert_contains "MG-13: acceptance-failed named as the NACK" "acceptance-failed" "$OUTPUT_MG13"

_remove_pr_state_entry 10106
rm -f "$CFG_MG13" "$SNAP_MG13"

# -----------------------------------------------------------------------
# Test MG-14 (D#2123, criterion 4) — a base_ref_changed event newer than
# the label invalidates it, with no force-push at all. This is the class
# the filed fix (add one token to the old force-push-only selector) was
# supposed to close.
#
# Mutation that must make this fail: in _invalidate_stale_pass_labels'
# SPAWN_AGENT branch (the one this test actually exercises), delete the
# `cand="${!mock_base_var:-}"` block that folds BASE_REF_TS into
# stale_after_ts -- i.e. undo the base-ref arm of D#2123. Run and paste
# both outputs in the PR body.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-14 (D#2123): base_ref_changed after the label — merge BLOCKED ==="
CFG_MG14=$(_make_config_file)
SNAP_MG14=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG14" 10109
_write_pr_state_merging 10108 10109 "False"

OUTPUT_MG14=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG14" SNAPSHOT_PATH="$SNAP_MG14" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10108_code_review_passed=yes \
  LABEL_TS_10108_code_review_passed=2026-08-20T00:52:36Z \
  BASE_REF_TS_10108=2026-08-20T20:24:34Z \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG14=$?

assert_exit_0 "MG-14: exit code 0 when base-ref-changed label blocks" "$RC_MG14"
assert_contains "MG-14: stale label reported" "STALE_LABEL_REMOVED: pr=10108 label=code-review-passed" "$OUTPUT_MG14"
assert_not_contains "MG-14: gh pr merge NOT called on stale label" "GH_MERGE_ARGS" "$OUTPUT_MG14"

_remove_pr_state_entry 10108
rm -f "$CFG_MG14" "$SNAP_MG14"

# -----------------------------------------------------------------------
# Test MG-15 (D#2123, criterion 5) — the head moved (a new `committed`
# event) after the label, with no head_ref_force_pushed event at all.
# This is the exact shape that let all five real PRs (#1999, #2002-#2005)
# through: a retarget-merge lands a merge-of-main commit on the branch
# after review, and the old force-push-only selector never saw it.
#
# Mutation that must make this fail: in the same SPAWN_AGENT branch,
# delete the `cand="${!mock_committed_var:-}"` block that folds
# COMMITTED_TS into stale_after_ts. Run and paste both outputs in the
# PR body.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-15 (D#2123): head moved via committed event, no force-push — merge BLOCKED ==="
CFG_MG15=$(_make_config_file)
SNAP_MG15=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG15" 10111
_write_pr_state_merging 10110 10111 "False"

OUTPUT_MG15=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG15" SNAPSHOT_PATH="$SNAP_MG15" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10110_code_review_passed=yes \
  LABEL_TS_10110_code_review_passed=2026-08-20T00:52:36Z \
  COMMITTED_TS_10110=2026-08-20T20:57:42Z \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG15=$?

assert_exit_0 "MG-15: exit code 0 when committed-after-label blocks" "$RC_MG15"
assert_contains "MG-15: stale label reported" "STALE_LABEL_REMOVED: pr=10110 label=code-review-passed" "$OUTPUT_MG15"
assert_not_contains "MG-15: gh pr merge NOT called on stale label" "GH_MERGE_ARGS" "$OUTPUT_MG15"

_remove_pr_state_entry 10110
rm -f "$CFG_MG15" "$SNAP_MG15"

# -----------------------------------------------------------------------
# Test MG-16 (D#2123, criterion 6) — NEGATIVE test: base advancement
# (`main` moving forward under an unrelated merge) must NOT invalidate a
# pass label. Base advancement emits no timeline event at all, so there
# is no mock to set for it -- this test proves that by omission: no
# FORCE_PUSH_TS, no COMMITTED_TS, no BASE_REF_TS mock is set for this PR,
# and the label survives and the merge proceeds. This is the regression
# guard against the ~6x/hour over-invalidation failure mode a base_sha
# (rather than base_ref) comparison would have caused.
# -----------------------------------------------------------------------
echo ""
echo "=== MG-16 (D#2123, negative): base advancement alone — label survives, merge PROCEEDS ==="
CFG_MG16=$(_make_config_file)
SNAP_MG16=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_MG16" 10113
_write_pr_state_merging 10112 10113 "False"

OUTPUT_MG16=$(AF_CONTROL_PLANE_CONFIG="$CFG_MG16" SNAPSHOT_PATH="$SNAP_MG16" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  DISCUSSING_MOCK='[]' \
  HAS_LABEL_10112_code_review_passed=yes \
  LABEL_TS_10112_code_review_passed=2026-08-20T00:52:36Z \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_MG16=$?

assert_exit_0 "MG-16: exit code 0 when base advancement alone" "$RC_MG16"
assert_not_contains "MG-16: nothing reported stale on base advancement" "STALE_LABEL_REMOVED" "$OUTPUT_MG16"
assert_contains "MG-16: merge proceeds" "GH_MERGE_ARGS" "$OUTPUT_MG16"

_remove_pr_state_entry 10112
rm -f "$CFG_MG16" "$SNAP_MG16"

# -----------------------------------------------------------------------
# Test META-1 (D#1777 code-review fix): _REVIEW_PASS_LABELS must never
# contain a _NACK_LABELS entry.
#
# MG-13 above drives the merging phase end-to-end and proves ordering —
# it does not, and structurally cannot, prove this. A mutation test
# poisoned _REVIEW_PASS_LABELS with acceptance-failed and do-not-merge
# and MG-13 still passed 47/47, because _check_nack_labels — pre-existing,
# untouched by this change — blocks on acceptance-failed on its own,
# before _invalidate_stale_pass_labels ever runs for that PR. MG-13
# exercises the gate's overall outcome, not _REVIEW_PASS_LABELS's actual
# content.
#
# This test reads the two array literals directly out of the script
# source and checks for intersection, so it depends only on the array
# _invalidate_stale_pass_labels actually iterates — not on
# _check_nack_labels, and not on call placement.
#
# This disjointness invariant is no longer just hygiene: _check_nack_labels
# now reads through the same map-consulting _has_label (the F1 fix routes
# every _has_label call through _STALE_INVALIDATED_LABELS first), so a
# NACK label that ever ended up in that map would suppress the NACK read
# too. It's safe today only because the map is populated exclusively from
# _REVIEW_PASS_LABELS entries — i.e. only because this test's assertion
# holds. Don't retire this as a redundant style check; it's the thing
# keeping that suppression from being possible.
# -----------------------------------------------------------------------
echo ""
echo "=== META-1: _REVIEW_PASS_LABELS must not contain any _NACK_LABELS entry ==="

_extract_bash_array() {
  # $1 = array name (e.g. _NACK_LABELS), $2 = script path.
  # Prints one label per line, quotes and surrounding whitespace stripped.
  awk -v name="$1" '
    $0 ~ "^"name"=\\(" { in_arr=1; next }
    in_arr && /^\)/ { in_arr=0; next }
    in_arr { gsub(/"/,""); gsub(/^[ \t]+|[ \t]+$/,""); if (length($0)) print }
  ' "$2"
}

NACK_SET=$(_extract_bash_array "_NACK_LABELS" "$SCRIPT" | sort)
PASS_SET=$(_extract_bash_array "_REVIEW_PASS_LABELS" "$SCRIPT" | sort)

if [ -z "$NACK_SET" ] || [ -z "$PASS_SET" ]; then
  echo "  FAIL: META-1: could not extract _NACK_LABELS and/or _REVIEW_PASS_LABELS from $SCRIPT"
  FAIL=$((FAIL + 1))
else
  META1_INTERSECTION=$(comm -12 <(echo "$NACK_SET") <(echo "$PASS_SET"))
  if [ -z "$META1_INTERSECTION" ]; then
    echo "  PASS: META-1: no NACK label present in _REVIEW_PASS_LABELS"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: META-1: NACK label(s) found in _REVIEW_PASS_LABELS: $META1_INTERSECTION"
    FAIL=$((FAIL + 1))
  fi
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
