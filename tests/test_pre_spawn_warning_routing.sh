#!/usr/bin/env bash
# tests/test_pre_spawn_warning_routing.sh — D#2144: pre-spawn-check.sh's
# WARNINGS array (9 WARNINGS+= sites, JSON key "warnings") has always had
# zero consumers -- stderr is discarded at spawn-agent.sh's call site
# (`2>/dev/null`) and backend/spawn_payload.py never read the JSON key that
# already carries the array through to build_payload(). Producing the
# warning string already worked; that was never the bug. This asserts the
# CONSUMER side: a warning present in PSC_JSON_INPUT (the same env var
# spawn-agent.sh sets from pre-spawn-check.sh's real JSON output) lands at
# a named, greppable destination after build_payload() runs.
#
# Run: bash tests/test_pre_spawn_warning_routing.sh

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# This suite does not write pr_state/blackboard fixtures, so it does not use
# tests/lib/blackboard-fixture.sh's blackboard_scratch_state_dir -- it only
# needs an isolated AUTONOMOUS_TEAM_STATE_DIR for audit_trail.jsonl, which a
# plain mktemp -d + trap covers without touching the blackboard at all.
SCRATCH_STATE_DIR="$(mktemp -d)"
trap 'rm -rf "$SCRATCH_STATE_DIR"' EXIT
export AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR"

PASS=0
FAIL=0

assert_exit_0() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then echo "  PASS: $label (exit 0)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 0, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  case "$haystack" in
    *"$needle"*) echo "  PASS: $label"; PASS=$((PASS + 1)) ;;
    *) echo "  FAIL: $label -- expected to find: $needle"; FAIL=$((FAIL + 1)) ;;
  esac
}

echo "=== D#2144: pre-spawn-check.sh warning routing ==="

# --- Test 1: a warning present in PSC_JSON_INPUT lands at the destination ---
# This is the exact env var and shape spawn-agent.sh sets from pre-spawn-check.sh's
# real (unstubbed) JSON output -- see scripts/spawn-agent.sh's PSC_JSON_INPUT.
WARNING_TEXT="control_plane.py show could not run (exit 126): TEST FIXTURE -- permission denied"
PSC_JSON_INPUT_TEST=$(python3 -c "import json,sys; print(json.dumps({'warnings':[sys.argv[1]]}))" "$WARNING_TEXT")

SP_OUT=$(PSC_JSON_INPUT="$PSC_JSON_INPUT_TEST" _ROLE=executor _DISC=2144 \
  PYTHONPATH="$REAL_REPO_ROOT" python3 -m backend.spawn_payload 2>&1)
SP_RC=$?
assert_exit_0 "build_payload() with a forced warning does not fail the spawn build" "$SP_RC"

# Item 2's acceptance command, verbatim and paste-able (the destination is
# $AUTONOMOUS_TEAM_STATE_DIR/audit.jsonl -- state_paths.AUDIT_LOG):
#   grep 'control_plane.py show could not run' "$AUTONOMOUS_TEAM_STATE_DIR/audit.jsonl"
AUDIT_HIT=$(grep -F "$WARNING_TEXT" "$SCRATCH_STATE_DIR/audit.jsonl" 2>/dev/null || true)
assert_contains "forced warning is greppable in audit.jsonl afterward" "$AUDIT_HIT" "$WARNING_TEXT"
assert_contains "audit row is tagged source=pre_spawn_check" "$AUDIT_HIT" '"source": "pre_spawn_check"'
assert_contains "audit row carries the role" "$AUDIT_HIT" 'role=executor'

# --- Test 2: no warnings -> no audit row written (silence stays silent, no noise) ---
SCRATCH_STATE_DIR_2="$(mktemp -d)"
SP_OUT_2=$(AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR_2" PSC_JSON_INPUT='{"warnings":[]}' _ROLE=executor _DISC=2144 \
  PYTHONPATH="$REAL_REPO_ROOT" python3 -m backend.spawn_payload 2>&1)
SP_RC_2=$?
assert_exit_0 "build_payload() with no warnings still succeeds" "$SP_RC_2"
if [ -f "$SCRATCH_STATE_DIR_2/audit.jsonl" ]; then
  echo "  FAIL: no audit.jsonl should be written when there are no warnings"
  FAIL=$((FAIL + 1))
else
  echo "  PASS: no audit.jsonl written when there are no warnings"
  PASS=$((PASS + 1))
fi
rm -rf "$SCRATCH_STATE_DIR_2"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
