#!/usr/bin/env bash
# tests/test_pr_dependents.sh — tests for scripts/lib/pr-dependents.sh and its
# wiring into both merge hosts (D#2020, Spec Acceptance items 1-7).
#
# D#2020 found that merging a base PR deletes its branch even when another
# open PR is based on it, which closes that PR outright (GitHub then refuses
# both a --base change on a closed PR and reopening a PR whose base ref is
# gone — a deadlock). The panel rejected auto-retargeting dependents (it
# changes a PR's effective diff without moving its head SHA, which the
# D#1777 stale-pass invalidator can't see) in favor of the narrower fix
# tested here: suppress --delete-branch when an open PR still depends on
# the branch being merged.
#
# Every failure mode in the new step must degrade to "merge, keep the
# branch" — never to a blocked or aborted merge. That is what items 5 and 7
# below exist to prove.
#
# Run: bash tests/test_pr_dependents.sh

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STEP5_SCRIPT="$REAL_REPO_ROOT/scripts/loop-phased-step5.sh"
CI_LIB="$REAL_REPO_ROOT/scripts/lib/ci-status-check.sh"
PR_DEP_LIB="$REAL_REPO_ROOT/scripts/lib/pr-dependents.sh"
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

assert_exit_0() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then echo "  PASS: $label (exit 0)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 0, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_contains() {
  local label="$1" expected_substr="$2" actual="$3"
  # `grep -qF --` guards against a needle that starts with '-' (e.g.
  # "--delete-branch") being parsed as an option instead of a pattern.
  if echo "$actual" | grep -qF -- "$expected_substr"; then
    echo "  PASS: $label"; PASS=$((PASS + 1))
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
    echo "  PASS: $label"; PASS=$((PASS + 1))
  fi
}

# -----------------------------------------------------------------------
# Fixture helpers (mirrors tests/test_loop_phased_step5.sh's harness)
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
    "phased_orchestration": false,
    "phased_code_review": false
  },
  "policies": {},
  "settings": {},
  "audit_log": []
}
JSON
  echo "$tmpfile"
}

_set_gate_true() {
  local config_file="$1" gate="$2"
  python3 -c "
import json
d = json.load(open('$config_file'))
d['gates']['$gate'] = True
json.dump(d, open('$config_file', 'w'), indent=2)
"
}

_write_snapshot_spec_ready() {
  local path="$1" disc_num="$2"
  python3 -c "
import json
from datetime import datetime, timezone
snap = {
    'discussions': [
        {'number': $disc_num, 'title': 'Test feature $disc_num', 'body': '<!-- STATUS:SPEC_READY --> some spec content'}
    ],
    'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
}
json.dump(snap, open('$path', 'w'))
"
}

_write_pr_state_sec_entry() {
  local pr_num="$1" disc_num="$2" phase="$3" needs_sec="$4" fix_cycles="${5:-0}"
  local bb_dir="$BB_PR_STATE_DIR"
  mkdir -p "$bb_dir"
  python3 -c "
import json
entry = {
    'value': {
        'pr': $pr_num, 'discussion': $disc_num, 'phase': '$phase',
        'spawned_phases': [], 'completed_phases': [],
        'needs_security_review': $needs_sec, 'fix_cycle_count': $fix_cycles,
        'respawn_count': 0, 'last_envelope': {}, 'blocked_reason': None,
        'created_at': '2026-01-01T00:00:00+00:00', 'updated_at': '2026-01-01T00:00:00+00:00'
    },
    'version': 1, 'updated_at': '2026-01-01T00:00:00+00:00', 'updated_by': 'test'
}
json.dump(entry, open('$bb_dir/$pr_num.json', 'w'), indent=2)
"
}

_remove_pr_state_entry() {
  rm -f "$BB_PR_STATE_DIR/$1.json"
}

# =========================================================================
# Item 6 — bash -n on both changed shell files
# =========================================================================
echo ""
echo "=== Item 6: bash -n scripts/lib/pr-dependents.sh and scripts/loop-phased-step5.sh ==="
bash -n "$PR_DEP_LIB"; assert_exit_0 "bash -n pr-dependents.sh" "$?"
bash -n "$STEP5_SCRIPT"; assert_exit_0 "bash -n loop-phased-step5.sh" "$?"

# =========================================================================
# Item 4 — manual path: ci_merge_sha_pinned's 4th arg via CI_MERGE_MODE=echo
# =========================================================================
echo ""
echo "=== Item 4: ci_merge_sha_pinned CI_MERGE_MODE=echo seam, both delete-branch cases ==="
_run_ci_merge() {
  local pr="$1" sha="$2" mode="${3:-}"
  (
    source "$CI_LIB"
    export CI_MERGE_MODE=echo
    if [ -n "$mode" ]; then
      ci_merge_sha_pinned "$pr" "test-owner/test-repo" "$sha" "$mode"
    else
      ci_merge_sha_pinned "$pr" "test-owner/test-repo" "$sha"
    fi
  )
}

OUT_DEFAULT=$(_run_ci_merge 40001 deadbeef01); RC_DEFAULT=$?
assert_exit_0 "default (no 4th arg) merge succeeds" "$RC_DEFAULT"
assert_contains "default mode still deletes the branch (backward-compat)" "--delete-branch" "$OUT_DEFAULT"

OUT_DELETE=$(_run_ci_merge 40002 deadbeef02 delete); RC_DELETE=$?
assert_exit_0 "explicit delete mode merge succeeds" "$RC_DELETE"
assert_contains "explicit 'delete' mode contains --delete-branch" "--delete-branch" "$OUT_DELETE"

OUT_KEEP=$(_run_ci_merge 40003 deadbeef03 keep); RC_KEEP=$?
assert_exit_0 "keep mode merge succeeds" "$RC_KEEP"
assert_not_contains "'keep' mode omits --delete-branch" "--delete-branch" "$OUT_KEEP"

# Mutation for item 4: pass --delete-branch unconditionally (ignore the 4th
# arg) -> OUT_KEEP would contain --delete-branch and this assertion would
# fail. Verified by temporarily hard-coding the flag back in and re-running
# (see PR description for the transcript); restored before commit.

# =========================================================================
# Items 2, 3, 5, 7 — loop path (scripts/loop-phased-step5.sh merging phase)
# =========================================================================

echo ""
echo "=== Item 3: loop path, no dependents -> --delete-branch present ==="
CFG_A=$(_make_config_file); _set_gate_true "$CFG_A" phased_orchestration; _set_gate_true "$CFG_A" phased_code_review
SNAP_A=$(mktemp --suffix='.json'); _write_snapshot_spec_ready "$SNAP_A" 90001
_write_pr_state_sec_entry 96001 90001 "merging" "False" 0

OUT_A=$(AF_CONTROL_PLANE_CONFIG="$CFG_A" SNAPSHOT_PATH="$SNAP_A" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 DASHBOARD_TOUCHED=no DISCUSSING_MOCK='[]' \
  HAS_LABEL_96001_code_review_passed=yes \
  PR_DEPENDENTS_TEST_MODE=1 PR_DEP_HEADREF_96001="branch-noop" \
  PR_DEP_OPEN_LIST_JSON='[]' \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$STEP5_SCRIPT" 2>&1)
RC_A=$?
assert_exit_0 "item 3: host exits 0" "$RC_A"
assert_contains "item 3: merge is attempted" "GH_MERGE_ARGS" "$OUT_A"
assert_contains "item 3: no dependents -> --delete-branch present" "--delete-branch" "$OUT_A"
_remove_pr_state_entry 96001
rm -f "$CFG_A" "$SNAP_A"

echo ""
echo "=== Item 2: loop path, one open dependent -> --delete-branch absent ==="
CFG_B=$(_make_config_file); _set_gate_true "$CFG_B" phased_orchestration; _set_gate_true "$CFG_B" phased_code_review
SNAP_B=$(mktemp --suffix='.json'); _write_snapshot_spec_ready "$SNAP_B" 90002
_write_pr_state_sec_entry 96002 90002 "merging" "False" 0

OUT_B=$(AF_CONTROL_PLANE_CONFIG="$CFG_B" SNAPSHOT_PATH="$SNAP_B" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 DASHBOARD_TOUCHED=no DISCUSSING_MOCK='[]' \
  HAS_LABEL_96002_code_review_passed=yes \
  PR_DEPENDENTS_TEST_MODE=1 PR_DEP_HEADREF_96002="branch-with-dep" \
  PR_DEP_OPEN_LIST_JSON='[{"number":96003,"baseRefName":"branch-with-dep"}]' \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$STEP5_SCRIPT" 2>&1)
RC_B=$?
assert_exit_0 "item 2: host exits 0" "$RC_B"
assert_contains "item 2: merge is attempted" "GH_MERGE_ARGS" "$OUT_B"
assert_not_contains "item 2: open dependent -> --delete-branch absent" "--delete-branch" "$OUT_B"
assert_contains "item 2: warning names the dependent PR" "96003" "$OUT_B"
_remove_pr_state_entry 96002
rm -f "$CFG_B" "$SNAP_B"

# Mutation for items 2 and 3: swap the `if [ "$_DEP_DELETE_BRANCH" = "true" ]`
# branch in loop-phased-step5.sh so --delete-branch is always appended (or
# never appended). Either change flips exactly one of OUT_A / OUT_B and was
# confirmed against a scratch copy before this file was finalized.

echo ""
echo "=== Item 5: fail-safe — dependents lookup fails, merge still occurs, branch kept ==="
CFG_C=$(_make_config_file); _set_gate_true "$CFG_C" phased_orchestration; _set_gate_true "$CFG_C" phased_code_review
SNAP_C=$(mktemp --suffix='.json'); _write_snapshot_spec_ready "$SNAP_C" 90003
_write_pr_state_sec_entry 96004 90003 "merging" "False" 0

OUT_C=$(AF_CONTROL_PLANE_CONFIG="$CFG_C" SNAPSHOT_PATH="$SNAP_C" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 DASHBOARD_TOUCHED=no DISCUSSING_MOCK='[]' \
  HAS_LABEL_96004_code_review_passed=yes \
  PR_DEPENDENTS_TEST_MODE=1 PR_DEP_LOOKUP_FAIL=1 \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$STEP5_SCRIPT" 2>&1)
RC_C=$?
assert_exit_0 "item 5: host exits 0 despite lookup failure" "$RC_C"
assert_contains "item 5: merge still occurs" "GH_MERGE_ARGS" "$OUT_C"
assert_not_contains "item 5: unknown dependents -> --delete-branch absent (safe side)" "--delete-branch" "$OUT_C"
_remove_pr_state_entry 96004
rm -f "$CFG_C" "$SNAP_C"

# Mutation for item 5: change the non-zero-rc branch in loop-phased-step5.sh
# to `continue`/abort instead of falling through to the merge call — GH_MERGE_ARGS
# then never appears and this assertion fails.

echo ""
echo "=== Item 7: set -u safety — pr-dependents.sh not sourced, merge still completes ==="
CFG_D=$(_make_config_file); _set_gate_true "$CFG_D" phased_orchestration; _set_gate_true "$CFG_D" phased_code_review
SNAP_D=$(mktemp --suffix='.json'); _write_snapshot_spec_ready "$SNAP_D" 90004
_write_pr_state_sec_entry 96005 90004 "merging" "False" 0

OUT_D=$(AF_CONTROL_PLANE_CONFIG="$CFG_D" SNAPSHOT_PATH="$SNAP_D" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 DASHBOARD_TOUCHED=no DISCUSSING_MOCK='[]' \
  HAS_LABEL_96005_code_review_passed=yes \
  PR_DEPENDENTS_DISABLE=1 \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$STEP5_SCRIPT" 2>&1)
RC_D=$?
assert_exit_0 "item 7: host exits 0 with the lib unavailable (no set -u abort)" "$RC_D"
assert_contains "item 7: merge line is still emitted" "GH_MERGE_ARGS" "$OUT_D"
assert_not_contains "item 7: lib absent -> treated as unknown, --delete-branch absent" "--delete-branch" "$OUT_D"
_remove_pr_state_entry 96005
rm -f "$CFG_D" "$SNAP_D"

# Mutation for item 7: reference a new variable (e.g. $_DEP_DELETE_BRANCH)
# before it is initialised, or delete the `declare -F pr_dependents_list`
# guard. With PR_DEPENDENTS_DISABLE=1 (function absent) that makes `set -u`
# abort the merging-phase iteration and GH_MERGE_ARGS never appears.

echo ""
echo "=== Item 8 (fix-cycle 1): gh pr list truncation at --limit is unknown, not empty ==="
# gh pr list has no --limit by default (30 on gh 2.96.0) and truncates
# SILENTLY past it: rc=0 with exactly `limit` entries is byte-indistinguishable
# from a genuine short/empty result. A real dependent sitting just outside the
# page must never read as "no dependents". Simulated here with
# PR_DEP_LIST_LIMIT=2 against a 2-entry fixture that DOES contain a real
# dependent (PR 97002, based on the merging PR's branch) — the raw count
# lands exactly on the (deliberately tiny) limit, so the answer must come
# back unknown, not "0 dependents found".
_run_pr_dependents_list() {
  (
    source "$PR_DEP_LIB"
    export PR_DEPENDENTS_TEST_MODE=1
    export PR_DEP_LIST_LIMIT="$1"
    export PR_DEP_HEADREF_97001="branch-truncated"
    export PR_DEP_OPEN_LIST_JSON='[{"number":97002,"baseRefName":"branch-truncated"},{"number":97003,"baseRefName":"main"}]'
    pr_dependents_list 97001 test-owner/test-repo
    rc=$?
    echo "RC:$rc LIST:[$PR_DEP_LIST] REASON:$PR_DEP_REASON"
  )
}

OUT_E=$(_run_pr_dependents_list 2)
assert_contains "item 8: raw count at the (tiny) limit reports RC:1 (unknown)" "RC:1" "$OUT_E"
assert_contains "item 8: reason names the limit, not a plain empty answer" "cannot rule out a dependent beyond the page" "$OUT_E"
assert_not_contains "item 8: does NOT silently report an empty list at RC:0" "RC:0 LIST:[]" "$OUT_E"

# Same fixture, limit raised past the raw count (5 > 2 entries) — the real
# dependent must still be found normally. Proves item 8's guard fires only
# on the truncation boundary, not on every lookup.
OUT_F=$(_run_pr_dependents_list 5)
assert_contains "item 8: raised limit -> RC:0, real dependent still found" "RC:0 LIST:[97002]" "$OUT_F"

# Mutation for item 8: this is the exact bug the security reviewer found in
# fix-cycle 1 -- before this fix, the raw-count/limit comparison in
# pr_dependents_list did not exist at all, so a truncated page read as a
# clean answer. Disabled the guard directly (`if false && [ "$raw_count"
# -eq "$list_limit" ]`) and re-ran this file: "RC:1 (unknown)" and the
# "cannot rule out a dependent" reason both went red (2/4 assertions in
# this block), confirming the guard is load-bearing rather than
# incidental. Restored before this file was finalized.
# vanished. Confirmed red, then restored before this file was finalized.

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  echo "PRESUM: fail step=test_pr_dependents exit=1 checks=$((PASS + FAIL))"
  exit 1
fi
echo "PRESUM: pass"
exit 0
