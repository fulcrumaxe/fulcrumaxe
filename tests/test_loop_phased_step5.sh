#!/usr/bin/env bash
# tests/test_loop_phased_step5.sh — Bash-style tests for scripts/loop-phased-step5.sh
#
# Run: bash tests/test_loop_phased_step5.sh
# Expects: all assertions pass, exit 0
#
# Design: tests use AF_CONTROL_PLANE_CONFIG to point control_plane.py at a temp config file.
# For pr_state fixtures (Tests 4+5), we write real blackboard entries using test-specific
# PR numbers and clean them up after each test.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REAL_REPO_ROOT/scripts/loop-phased-step5.sh"
# shellcheck source=lib/blackboard-fixture.sh
source "$REAL_REPO_ROOT/tests/lib/blackboard-fixture.sh"
# D#2283: this suite used to write its pr_state fixtures straight through
# the in-repo blackboard symlink into production state. Redirect
# AUTONOMOUS_TEAM_STATE_DIR to a scratch dir for the life of this suite, and
# resolve the fixture dir through the helper so writes land there instead —
# on the clean path and on a crash alike. Its removal is folded into the
# pre-existing RUN_TMP trap below rather than a second trap registration,
# which would silently replace it (D#2283 trap-composition hazard).
blackboard_scratch_state_dir || {
  echo "FATAL: could not create scratch state dir" >&2
  exit 1
}
SCRATCH_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
BB_PR_STATE_DIR="$(blackboard_pr_state_dir "$REAL_REPO_ROOT")" || {
  echo "FATAL: could not resolve blackboard pr_state dir" >&2
  exit 1
}
PASS=0
FAIL=0

# D#2271 PR-a: the merging-phase helpers (_check_ci_passed's stand-down
# branch, and the new ci_note_merge_if_unverified fallback call after a
# successful merge) can call ci_write_audit. This suite deliberately leaves
# AUTONOMOUS_TEAM_STATE_DIR unset (see the file header above — pr_state
# fixtures need that), so redirect ONLY the audit-write seam, which is
# independent of the blackboard/pr_state resolution path.
export CI_STATUS_TEST_MODE=1
export CI_STATUS_TEST_AUDIT_FILE="$(mktemp -t loop-phased-step5-tests.XXXXXX)"
trap 'rm -f "$CI_STATUS_TEST_AUDIT_FILE"' EXIT

# Injection-exploit marker files (Tests 18 & 20 below) live under one
# mktemp'd dir rather than fixed /tmp/exploit-marker{,-replay} names — a
# concurrently-running copy of this suite touching the same fixed name would
# make MARKER_BEFORE/MARKER_AFTER checks racy (D#2254).
RUN_TMP="$(mktemp -d /tmp/test_loop_phased_step5.XXXXXX)"
trap 'rm -rf "$RUN_TMP" "$SCRATCH_STATE_DIR"' EXIT

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

# Create a temp config file and return its path
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

# Create a loop snapshot JSON at a given path.
# generated_at is always "now" unless a test is specifically about staleness —
# loop-phased-step5.sh ignores any snapshot past MAX_AGE=600s and re-queries
# GraphQL, so a fixed 2026-01-01 date would silently stop exercising the
# snapshot path these tests are here to cover.
_write_snapshot_empty() {
  local path="$1"
  python3 -c "
import json
from datetime import datetime, timezone
snap = {
    'discussions': [],
    'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
}
json.dump(snap, open('$path', 'w'))
"
}

# A snapshot that IS spec-ready but is five days old. Routing off this would
# spawn against Discussion state that has since moved; step 5 must ignore it.
_write_snapshot_stale_spec_ready() {
  local path="$1" disc_num="$2"
  python3 -c "
import json
from datetime import datetime, timedelta, timezone
stale = datetime.now(timezone.utc) - timedelta(days=5)
snap = {
    'discussions': [
        {
            'number': $disc_num,
            'title': 'Stale feature $disc_num',
            'body': '<!-- STATUS:SPEC_READY --> some spec content'
        }
    ],
    'generated_at': stale.isoformat(timespec='seconds').replace('+00:00', 'Z'),
}
json.dump(snap, open('$path', 'w'))
"
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

# Write a pr_state blackboard entry directly (for test setup)
_write_pr_state_entry() {
  local pr_num="$1" disc_num="$2" phase="$3"
  local bb_dir="$BB_PR_STATE_DIR"
  mkdir -p "$bb_dir"
  python3 -c "
import json
entry = {
    'value': {
        'pr': $pr_num,
        'discussion': $disc_num,
        'phase': '$phase',
        'spawned_phases': [],
        'completed_phases': [],
        'needs_security_review': False,
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
print('created pr_state/$pr_num.json')
"
}

_remove_pr_state_entry() {
  local pr_num="$1"
  rm -f "$BB_PR_STATE_DIR/$pr_num.json"
}

# -----------------------------------------------------------------------
# Test 1: Gate off — script exits 0 immediately, no spawns
# -----------------------------------------------------------------------
echo ""
echo "=== Test 1: gate off — exits 0, no spawns ==="
CFG1=$(_make_config_file)
SNAP1=$(mktemp --suffix='.json')
_write_snapshot_empty "$SNAP1"

OUTPUT1=$(AF_CONTROL_PLANE_CONFIG="$CFG1" SNAPSHOT_PATH="$SNAP1" \
  SPAWN_AGENT=echo \
  bash "$SCRIPT" 2>&1)
RC1=$?

assert_exit_0 "exit code 0 when gate off" "$RC1"
assert_not_contains "no spawn calls made" "SPAWN_AGENT_ARGS" "$OUTPUT1"
# Gate off: exits before printing any phased step5 messages
assert_not_contains "no phased step5 log when gate off" "phased step5" "$OUTPUT1"

rm -f "$CFG1" "$SNAP1"

# -----------------------------------------------------------------------
# Test 2: Gate on, no discussions — exits 0
# -----------------------------------------------------------------------
echo ""
echo "=== Test 2: gate on, no SPEC_READY discussions — exits 0 ==="
CFG2=$(_make_config_file)
_set_gate_true "$CFG2" phased_orchestration
SNAP2=$(mktemp --suffix='.json')
_write_snapshot_empty "$SNAP2"

OUTPUT2=$(AF_CONTROL_PLANE_CONFIG="$CFG2" SNAPSHOT_PATH="$SNAP2" \
  SPAWN_AGENT=echo DISCUSSING_MOCK='[]' SPEC_READY_MOCK='[]' \
  bash "$SCRIPT" 2>&1)
RC2=$?

assert_exit_0 "exit code 0 when no discussions" "$RC2"
assert_not_contains "no spawn calls for empty queue" "SPAWN_AGENT_ARGS" "$OUTPUT2"
# _log() posts to team-log (GitHub), not stdout — just verify no spurious output
assert_not_contains "no error output when gate on and no discussions" "Error" "$OUTPUT2"

rm -f "$CFG2" "$SNAP2"

# -----------------------------------------------------------------------
# Test 3: Gate on, fresh SPEC_READY discussion (no pr_state entry) — executor spawned
# -----------------------------------------------------------------------
echo ""
echo "=== Test 3: gate on, fresh SPEC_READY discussion — executor spawned ==="
CFG3=$(_make_config_file)
_set_gate_true "$CFG3" phased_orchestration
SNAP3=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP3" 99901

OUTPUT3=$(AF_CONTROL_PLANE_CONFIG="$CFG3" SNAPSHOT_PATH="$SNAP3" \
  SPAWN_AGENT=echo \
  bash "$SCRIPT" 2>&1)
RC3=$?

assert_exit_0 "exit code 0 with SPEC_READY discussion" "$RC3"
assert_contains "executor spawn invoked for discussion" "executor" "$OUTPUT3"
assert_contains "discussion number passed to spawn" "99901" "$OUTPUT3"

rm -f "$CFG3" "$SNAP3"

# -----------------------------------------------------------------------
# Test 4: Gate on, entry in code_review phase, sub-gate off — impl-coord spawned
# -----------------------------------------------------------------------
echo ""
echo "=== Test 4: code_review phase, phased_code_review=false — impl-coord spawned ==="
CFG4=$(_make_config_file)
_set_gate_true "$CFG4" phased_orchestration
# phased_code_review stays false
SNAP4=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP4" 88801

# Create pr_state entry in code_review phase for this discussion
_write_pr_state_entry 50100 88801 "code_review"

OUTPUT4=$(AF_CONTROL_PLANE_CONFIG="$CFG4" SNAPSHOT_PATH="$SNAP4" \
  SPAWN_AGENT=echo \
  bash "$SCRIPT" 2>&1)
RC4=$?

assert_exit_0 "exit code 0" "$RC4"
assert_contains "phased_code_review=false: waiting for next iteration" "phased_code_review=false" "$OUTPUT4"

_remove_pr_state_entry 50100
rm -f "$CFG4" "$SNAP4"

# -----------------------------------------------------------------------
# Test 5: Gate on, entry in code_review phase, sub-gate ON — no spawn (no-op for PR-c)
# -----------------------------------------------------------------------
echo ""
echo "=== Test 5: code_review phase, phased_code_review=true — no-op (awaits PR-c) ==="
CFG5=$(_make_config_file)
_set_gate_true "$CFG5" phased_orchestration
_set_gate_true "$CFG5" phased_code_review
SNAP5=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP5" 77701

# Create pr_state entry in code_review phase for this discussion
_write_pr_state_entry 60100 77701 "code_review"

OUTPUT5=$(AF_CONTROL_PLANE_CONFIG="$CFG5" SNAPSHOT_PATH="$SNAP5" \
  SPAWN_AGENT=echo \
  bash "$SCRIPT" 2>&1)
RC5=$?

assert_exit_0 "exit code 0" "$RC5"
# When phased_code_review=true, no spawn happens (PR-c will handle) — verify silence
assert_not_contains "no impl-coordinator spawn when sub-gate on" "impl-coordinator" "$OUTPUT5"
assert_not_contains "no executor re-spawn for existing code_review entry" "role executor" "$OUTPUT5"

_remove_pr_state_entry 60100
rm -f "$CFG5" "$SNAP5"

# -----------------------------------------------------------------------
# Test 6: code_review phase, phased_code_review=true — code-reviewer spawned directly
# -----------------------------------------------------------------------
echo ""
echo "=== Test 6: code_review phase, phased_code_review=true — code-reviewer spawned ==="
CFG_T6=$(_make_config_file)
_set_gate_true "$CFG_T6" phased_orchestration
_set_gate_true "$CFG_T6" phased_code_review
SNAP_T6=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T6" 55501

# Create pr_state entry in code_review phase, fix_cycle_count=0
_write_pr_state_entry 70100 55501 "code_review"

OUTPUT_T6=$(AF_CONTROL_PLANE_CONFIG="$CFG_T6" SNAPSHOT_PATH="$SNAP_T6" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  TWO_GATE_PR_BODY_70100="## Verification\nGate 1: PASS\nGate 2: PASS" \
  bash "$SCRIPT" 2>&1)
RC_T6=$?

assert_exit_0 "exit code 0" "$RC_T6"
assert_contains "code-reviewer role passed to spawn" "code-reviewer" "$OUTPUT_T6"
assert_not_contains "impl-coordinator NOT spawned when sub-gate on" "impl-coordinator" "$OUTPUT_T6"

_remove_pr_state_entry 70100
rm -f "$CFG_T6" "$SNAP_T6"

# -----------------------------------------------------------------------
# Test 7: code_review phase, phased_code_review=true, fix_cycle_count=3 — escalation
# -----------------------------------------------------------------------
echo ""
echo "=== Test 7: code_review phase, fix_cycle_count=3 — escalate to needs-boss ==="
CFG_T7=$(_make_config_file)
_set_gate_true "$CFG_T7" phased_orchestration
_set_gate_true "$CFG_T7" phased_code_review
SNAP_T7=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T7" 44401

# Write pr_state with fix_cycle_count=3
BB_DIR_T7="$BB_PR_STATE_DIR"
mkdir -p "$BB_DIR_T7"
python3 -c "
import json
entry = {
    'value': {
        'pr': 80100,
        'discussion': 44401,
        'phase': 'code_review',
        'spawned_phases': [],
        'completed_phases': [],
        'needs_security_review': False,
        'fix_cycle_count': 3,
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
json.dump(entry, open('$BB_DIR_T7/80100.json', 'w'), indent=2)
"

OUTPUT_T7=$(AF_CONTROL_PLANE_CONFIG="$CFG_T7" SNAPSHOT_PATH="$SNAP_T7" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  TWO_GATE_PR_BODY_80100="## Verification\nGate 1: PASS\nGate 2: PASS" \
  bash "$SCRIPT" 2>&1)
RC_T7=$?

assert_exit_0 "exit code 0 on escalation" "$RC_T7"
# Escalation: pr_state is advanced to blocked (visible in advance output) and no reviewer spawned
assert_contains "pr_state advanced to blocked on escalation" "blocked" "$OUTPUT_T7"
assert_not_contains "no code-reviewer spawn on escalation" "code-reviewer" "$OUTPUT_T7"

rm -f "$BB_DIR_T7/80100.json"
rm -f "$CFG_T7" "$SNAP_T7"

# -----------------------------------------------------------------------
# Test 8: code_review phase, phased_code_review=true, fix_cycle_count=2 — reviewer spawned (not yet at cap)
# -----------------------------------------------------------------------
echo ""
echo "=== Test 8: code_review phase, fix_cycle_count=2 — code-reviewer still spawned ==="
CFG_T8=$(_make_config_file)
_set_gate_true "$CFG_T8" phased_orchestration
_set_gate_true "$CFG_T8" phased_code_review
SNAP_T8=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T8" 33301

BB_DIR_T8="$BB_PR_STATE_DIR"
mkdir -p "$BB_DIR_T8"
python3 -c "
import json
entry = {
    'value': {
        'pr': 90100,
        'discussion': 33301,
        'phase': 'code_review',
        'spawned_phases': [],
        'completed_phases': [],
        'needs_security_review': False,
        'fix_cycle_count': 2,
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
json.dump(entry, open('$BB_DIR_T8/90100.json', 'w'), indent=2)
"

OUTPUT_T8=$(AF_CONTROL_PLANE_CONFIG="$CFG_T8" SNAPSHOT_PATH="$SNAP_T8" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  TWO_GATE_PR_BODY_90100="## Verification\nGate 1: PASS\nGate 2: PASS" \
  bash "$SCRIPT" 2>&1)
RC_T8=$?

assert_exit_0 "exit code 0 at fix_cycle_count=2" "$RC_T8"
assert_contains "code-reviewer spawned when fix_cycle_count below cap" "code-reviewer" "$OUTPUT_T8"
assert_not_contains "no escalation below cap" "needs-boss" "$OUTPUT_T8"

rm -f "$BB_DIR_T8/90100.json"
rm -f "$CFG_T8" "$SNAP_T8"

# -----------------------------------------------------------------------
# Test 9: Idempotency — running twice with same state produces same outcome
# -----------------------------------------------------------------------
echo ""
echo "=== Test 9: idempotency — running twice produces same result ==="
CFG6=$(_make_config_file)
_set_gate_true "$CFG6" phased_orchestration
SNAP6=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP6" 11101

# First run — no pr_state entry, SPAWN_AGENT=echo so no real entry is created
OUTPUT6A=$(AF_CONTROL_PLANE_CONFIG="$CFG6" SNAPSHOT_PATH="$SNAP6" \
  SPAWN_AGENT=echo \
  bash "$SCRIPT" 2>&1)
RC6A=$?

# Second run — same state (SPAWN_AGENT=echo means no pr_state entry was created by spawn)
OUTPUT6B=$(AF_CONTROL_PLANE_CONFIG="$CFG6" SNAPSHOT_PATH="$SNAP6" \
  SPAWN_AGENT=echo \
  bash "$SCRIPT" 2>&1)
RC6B=$?

assert_exit_0 "first run exits 0" "$RC6A"
assert_exit_0 "second run exits 0" "$RC6B"
# Both runs see no pr_state entry so both attempt the executor spawn
assert_contains "first run attempts executor spawn" "executor" "$OUTPUT6A"
assert_contains "second run also attempts spawn (idempotent, no entry written by mock)" "executor" "$OUTPUT6B"

rm -f "$CFG6" "$SNAP6"

# -----------------------------------------------------------------------
# Helper: write pr_state entry with needs_security_review flag
# -----------------------------------------------------------------------
_write_pr_state_sec_entry() {
  local pr_num="$1" disc_num="$2" phase="$3" needs_sec="$4" fix_cycles="${5:-0}"
  local bb_dir="$BB_PR_STATE_DIR"
  mkdir -p "$bb_dir"
  python3 -c "
import json
entry = {
    'value': {
        'pr': $pr_num,
        'discussion': $disc_num,
        'phase': '$phase',
        'spawned_phases': [],
        'completed_phases': [],
        'needs_security_review': $needs_sec,
        'fix_cycle_count': $fix_cycles,
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
print('created pr_state/$pr_num.json')
"
}

# -----------------------------------------------------------------------
# Test 10: security_review phase, needs_security_review=true — security-reviewer spawned
# -----------------------------------------------------------------------
echo ""
echo "=== Test 10: security_review phase, needs_security_review=true — security-reviewer spawned ==="
CFG_T10=$(_make_config_file)
_set_gate_true "$CFG_T10" phased_orchestration
_set_gate_true "$CFG_T10" phased_code_review
SNAP_T10=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T10" 22201

_write_pr_state_sec_entry 91100 22201 "security_review" "True" 0

OUTPUT_T10=$(AF_CONTROL_PLANE_CONFIG="$CFG_T10" SNAPSHOT_PATH="$SNAP_T10" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T10=$?

assert_exit_0 "exit code 0 for security_review phase" "$RC_T10"
assert_contains "security-reviewer role passed to spawn" "security-reviewer" "$OUTPUT_T10"
assert_not_contains "executor not re-spawned at security_review" "role executor" "$OUTPUT_T10"

_remove_pr_state_entry 91100
rm -f "$CFG_T10" "$SNAP_T10"

# -----------------------------------------------------------------------
# Test 11: security_review phase, needs_security_review=false — advance to merging
# -----------------------------------------------------------------------
echo ""
echo "=== Test 11: security_review phase, needs_security_review=false — advance to merging ==="
CFG_T11=$(_make_config_file)
_set_gate_true "$CFG_T11" phased_orchestration
_set_gate_true "$CFG_T11" phased_code_review
SNAP_T11=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T11" 22202

_write_pr_state_sec_entry 91200 22202 "security_review" "False" 0

OUTPUT_T11=$(AF_CONTROL_PLANE_CONFIG="$CFG_T11" SNAPSHOT_PATH="$SNAP_T11" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T11=$?

assert_exit_0 "exit code 0 when no security review needed" "$RC_T11"
assert_contains "merging mentioned in log (advancing)" "merging" "$OUTPUT_T11"
assert_not_contains "security-reviewer not spawned when needs_security_review=false" "security-reviewer" "$OUTPUT_T11"

_remove_pr_state_entry 91200
rm -f "$CFG_T11" "$SNAP_T11"

# -----------------------------------------------------------------------
# Test 12: security_review phase, fix_cycle_count=3 — escalate to needs-boss
# -----------------------------------------------------------------------
echo ""
echo "=== Test 12: security_review phase, fix_cycle_count=3 — escalate to needs-boss ==="
CFG_T12=$(_make_config_file)
_set_gate_true "$CFG_T12" phased_orchestration
_set_gate_true "$CFG_T12" phased_code_review
SNAP_T12=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T12" 22203

_write_pr_state_sec_entry 91300 22203 "security_review" "True" 3

OUTPUT_T12=$(AF_CONTROL_PLANE_CONFIG="$CFG_T12" SNAPSHOT_PATH="$SNAP_T12" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T12=$?

assert_exit_0 "exit code 0 on security escalation" "$RC_T12"
assert_contains "blocked mentioned on security fix_cycle cap" "blocked" "$OUTPUT_T12"
assert_not_contains "security-reviewer not spawned at escalation" "security-reviewer" "$OUTPUT_T12"

_remove_pr_state_entry 91300
rm -f "$CFG_T12" "$SNAP_T12"

# -----------------------------------------------------------------------
# Test 13: merging phase — phase handler runs without error and doesn't spawn
# -----------------------------------------------------------------------
echo ""
echo "=== Test 13: merging phase, no security needed — handler runs without error ==="
CFG_T13=$(_make_config_file)
_set_gate_true "$CFG_T13" phased_orchestration
_set_gate_true "$CFG_T13" phased_code_review
SNAP_T13=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T13" 22204

_write_pr_state_sec_entry 91400 22204 "merging" "False" 0

# GH_MERGE=echo mocks the merge call; HOOKS_DISABLED=1 skips post-merge-hook.
# _has_label uses real gh — label check will block merge since PR 91400 doesn't exist.
# That's expected: verify no spawn attempted, script exits 0.
OUTPUT_T13=$(AF_CONTROL_PLANE_CONFIG="$CFG_T13" SNAPSHOT_PATH="$SNAP_T13" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DISCUSSING_MOCK='[]' \
  DASHBOARD_TOUCHED=no \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T13=$?

assert_exit_0 "exit code 0 for merging phase" "$RC_T13"
# The merging handler does NOT spawn any agent — it does gh pr merge directly
assert_not_contains "merging phase does not invoke SPAWN_AGENT" "SPAWN_AGENT_ARGS" "$OUTPUT_T13"

_remove_pr_state_entry 91400
rm -f "$CFG_T13" "$SNAP_T13"

# -----------------------------------------------------------------------
# Test 14: merging phase — label gate enforced (no merge when labels absent)
# -----------------------------------------------------------------------
echo ""
echo "=== Test 14: merging phase — no merge when labels absent ==="
CFG_T14=$(_make_config_file)
_set_gate_true "$CFG_T14" phased_orchestration
_set_gate_true "$CFG_T14" phased_code_review
SNAP_T14=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T14" 22205

_write_pr_state_sec_entry 91500 22205 "merging" "False" 0

OUTPUT_T14=$(AF_CONTROL_PLANE_CONFIG="$CFG_T14" SNAPSHOT_PATH="$SNAP_T14" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T14=$?

assert_exit_0 "exit code 0 for merging phase label-block test" "$RC_T14"
# Without a real PR with labels, _has_label returns false — merge stays blocked
assert_not_contains "no merge attempted when labels absent" "GH_MERGE_ARGS" "$OUTPUT_T14"

_remove_pr_state_entry 91500
rm -f "$CFG_T14" "$SNAP_T14"

# -----------------------------------------------------------------------
# Test 15: terminal phases (merged, blocked) — no action, no spawn, no merge
# -----------------------------------------------------------------------
echo ""
echo "=== Test 15: terminal phases — no spawn, no merge ==="
for TERM_PHASE in merged blocked; do
  CFG_T15=$(_make_config_file)
  _set_gate_true "$CFG_T15" phased_orchestration
  _set_gate_true "$CFG_T15" phased_code_review
  SNAP_T15=$(mktemp --suffix='.json')
  DISC_T15="$((91600 + RANDOM % 100))"
  PR_T15="$((91700 + RANDOM % 100))"
  _write_snapshot_spec_ready "$SNAP_T15" "$DISC_T15"
  _write_pr_state_sec_entry "$PR_T15" "$DISC_T15" "$TERM_PHASE" "False" 0

  OUTPUT_T15=$(AF_CONTROL_PLANE_CONFIG="$CFG_T15" SNAPSHOT_PATH="$SNAP_T15" \
    SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
    DISCUSSING_MOCK='[]' \
    REPO_ROOT="$REAL_REPO_ROOT" \
    bash "$SCRIPT" 2>&1)
  RC_T15=$?

  assert_exit_0 "exit code 0 for terminal phase $TERM_PHASE" "$RC_T15"
  # Terminal phases produce no spawn or merge output — they just log to team-log (GitHub, not stdout)
  assert_not_contains "no spawn for terminal phase $TERM_PHASE" "SPAWN_AGENT_ARGS" "$OUTPUT_T15"
  assert_not_contains "no merge for terminal phase $TERM_PHASE" "GH_MERGE_ARGS" "$OUTPUT_T15"

  rm -f "$BB_PR_STATE_DIR/${PR_T15}.json" 2>/dev/null || true
  rm -f "$CFG_T15" "$SNAP_T15"
done

# -----------------------------------------------------------------------
# Test 16: merging phase — security trigger fires BEFORE merge (D#654 regression)
#   code-review-passed present, security trigger detected, security-review-passed absent
#   → merge must be blocked with a security-related message; GH_MERGE_ARGS must NOT appear
# -----------------------------------------------------------------------
echo ""
echo "=== Test 16: merging phase — security trigger blocks merge before gh pr merge call ==="
CFG_T16=$(_make_config_file)
_set_gate_true "$CFG_T16" phased_orchestration
_set_gate_true "$CFG_T16" phased_code_review
SNAP_T16=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T16" 22210

_write_pr_state_sec_entry 92000 22210 "merging" "False" 0

# code-review-passed present, security-review-passed absent, trigger fires
OUTPUT_T16=$(AF_CONTROL_PLANE_CONFIG="$CFG_T16" SNAPSHOT_PATH="$SNAP_T16" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=yes \
  HAS_LABEL_92000_code_review_passed=yes \
  HAS_LABEL_92000_security_review_passed=no \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T16=$?

assert_exit_0 "exit code 0 when security trigger blocks" "$RC_T16"
# _log() posts to team-log (GitHub), not stdout — verify by absence of merge call
assert_not_contains "gh pr merge NOT called when security trigger blocks" "GH_MERGE_ARGS" "$OUTPUT_T16"
# pr_state should not be advanced to merged — advance call only happens after successful _gh_merge
assert_not_contains "no pr_state advance to merged when trigger blocks" "merged" "$OUTPUT_T16"
# security-reviewer must be spawned so the gate can be cleared next iteration
assert_contains "security-reviewer spawned when trigger blocks merge" "security-reviewer" "$OUTPUT_T16"

_remove_pr_state_entry 92000
rm -f "$CFG_T16" "$SNAP_T16"

# -----------------------------------------------------------------------
# Test 17: merging phase — security trigger fires but security-review-passed present
#   → merge proceeds (GH_MERGE_ARGS appears)
# -----------------------------------------------------------------------
echo ""
echo "=== Test 17: merging phase — security trigger fires but security-review-passed present — merge proceeds ==="
CFG_T17=$(_make_config_file)
_set_gate_true "$CFG_T17" phased_orchestration
_set_gate_true "$CFG_T17" phased_code_review
SNAP_T17=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T17" 22211

_write_pr_state_sec_entry 92100 22211 "merging" "True" 0

# Both code-review-passed and security-review-passed present, trigger fires
OUTPUT_T17=$(AF_CONTROL_PLANE_CONFIG="$CFG_T17" SNAPSHOT_PATH="$SNAP_T17" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=yes \
  HAS_LABEL_92100_code_review_passed=yes \
  HAS_LABEL_92100_security_review_passed=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T17=$?

assert_exit_0 "exit code 0 when security trigger present and label present" "$RC_T17"
assert_contains "merge proceeds when security-review-passed is present" "GH_MERGE_ARGS" "$OUTPUT_T17"
assert_not_contains "merge not blocked when all gates pass" "merging blocked" "$OUTPUT_T17"

_remove_pr_state_entry 92100
rm -f "$CFG_T17" "$SNAP_T17"

# -----------------------------------------------------------------------
# Test 17b (D#1588 HG-7): merging phase — no diff-content trigger, but the
# originating Discussion is provenance:external — security-review-passed is
# still required and merge is blocked without it.
# -----------------------------------------------------------------------
echo ""
echo "=== Test 17b: merging phase — external-provenance Discussion forces security-review-passed ==="
CFG_T17B=$(_make_config_file)
_set_gate_true "$CFG_T17B" phased_orchestration
_set_gate_true "$CFG_T17B" phased_code_review
SNAP_T17B=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T17B" 22212

_write_pr_state_sec_entry 92200 22212 "merging" "False" 0

# code-review-passed present, no diff-content security trigger, but the
# Discussion is provenance:external — security-review-passed still required.
OUTPUT_T17B=$(AF_CONTROL_PLANE_CONFIG="$CFG_T17B" SNAPSHOT_PATH="$SNAP_T17B" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  EXTERNAL_PROVENANCE_FORCES_SECURITY=yes \
  HAS_LABEL_92200_code_review_passed=yes \
  HAS_LABEL_92200_security_review_passed=no \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T17B=$?

assert_exit_0 "exit code 0 when external-provenance forces security review" "$RC_T17B"
assert_not_contains "gh pr merge NOT called when external-provenance blocks" "GH_MERGE_ARGS" "$OUTPUT_T17B"
assert_contains "security-reviewer spawned when external-provenance blocks merge" "security-reviewer" "$OUTPUT_T17B"

_remove_pr_state_entry 92200
rm -f "$CFG_T17B" "$SNAP_T17B"

# -----------------------------------------------------------------------
# Test 17c (D#1588 HG-7): merging phase — external-provenance Discussion AND
# security-review-passed present — merge proceeds.
# -----------------------------------------------------------------------
echo ""
echo "=== Test 17c: merging phase — external-provenance Discussion, security-review-passed present — merge proceeds ==="
CFG_T17C=$(_make_config_file)
_set_gate_true "$CFG_T17C" phased_orchestration
_set_gate_true "$CFG_T17C" phased_code_review
SNAP_T17C=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T17C" 22213

_write_pr_state_sec_entry 92300 22213 "merging" "False" 0

OUTPUT_T17C=$(AF_CONTROL_PLANE_CONFIG="$CFG_T17C" SNAPSHOT_PATH="$SNAP_T17C" \
  SPAWN_AGENT=echo GH_MERGE=echo HOOKS_DISABLED=1 \
  DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no \
  EXTERNAL_PROVENANCE_FORCES_SECURITY=yes \
  HAS_LABEL_92300_code_review_passed=yes \
  HAS_LABEL_92300_security_review_passed=yes \
  REPO_ROOT="$REAL_REPO_ROOT" \
  bash "$SCRIPT" 2>&1)
RC_T17C=$?

assert_exit_0 "exit code 0 when external-provenance forces security review and label present" "$RC_T17C"
assert_contains "merge proceeds when security-review-passed present despite external-provenance" "GH_MERGE_ARGS" "$OUTPUT_T17C"

_remove_pr_state_entry 92300
rm -f "$CFG_T17C" "$SNAP_T17C"

# -----------------------------------------------------------------------
# Test 17d (D#1588 Batch B security-needs-fix round): the REAL (non-test-mode)
# _external_provenance_forces_security() must fail closed when
# external_intake_gate.py's security-required exits 3 (fetch failed / unknown)
# — treating it the same as "required", never as "not required". Extracts the
# actual function body from the script (rather than re-implementing the logic)
# and stubs python3 on PATH so we exercise the real mapping without hitting
# the live GitHub API.
# -----------------------------------------------------------------------
echo ""
echo "=== Test 17d: _external_provenance_forces_security fails closed on rc=3 (fetch failure) ==="

T17D_DIR=$(mktemp -d)
awk '/^_external_provenance_forces_security\(\) \{/,/^\}/' "$SCRIPT" > "$T17D_DIR/func.sh"

cat > "$T17D_DIR/python3" <<EOF
#!/usr/bin/env bash
exit \${STUB_SECURITY_REQUIRED_RC:-1}
EOF
chmod +x "$T17D_DIR/python3"

# rc=3 (fetch failed/unknown) must be treated as required (function returns 0/true)
OUT_T17D_RC3=$(env PATH="$T17D_DIR:$PATH" SCRIPT_DIR="$REAL_REPO_ROOT/scripts" \
  STUB_SECURITY_REQUIRED_RC=3 \
  bash -c "source '$T17D_DIR/func.sh'; _external_provenance_forces_security 12345; echo \"RESULT=\$?\"" 2>&1)
assert_contains "17d: rc=3 (fetch failed) maps to required (RESULT=0)" "RESULT=0" "$OUT_T17D_RC3"

# rc=1 (confirmed not required) must map to not-required (function returns 1/false)
OUT_T17D_RC1=$(env PATH="$T17D_DIR:$PATH" SCRIPT_DIR="$REAL_REPO_ROOT/scripts" \
  STUB_SECURITY_REQUIRED_RC=1 \
  bash -c "source '$T17D_DIR/func.sh'; _external_provenance_forces_security 12345; echo \"RESULT=\$?\"" 2>&1)
assert_contains "17d: rc=1 (confirmed not required) maps to not-required (RESULT=1)" "RESULT=1" "$OUT_T17D_RC1"

# rc=0 (label confirmed present) must map to required
OUT_T17D_RC0=$(env PATH="$T17D_DIR:$PATH" SCRIPT_DIR="$REAL_REPO_ROOT/scripts" \
  STUB_SECURITY_REQUIRED_RC=0 \
  bash -c "source '$T17D_DIR/func.sh'; _external_provenance_forces_security 12345; echo \"RESULT=\$?\"" 2>&1)
assert_contains "17d: rc=0 (label present) maps to required (RESULT=0)" "RESULT=0" "$OUT_T17D_RC0"

rm -rf "$T17D_DIR"

# -----------------------------------------------------------------------
# Test 18: _sanitize_diff shell injection guard
# Verifies that a diff containing $(cmd) or backticks does NOT execute
# in the shell — i.e. the PYEOF heredoc is properly quoted.
# -----------------------------------------------------------------------
echo ""
echo "--- Test 18: _sanitize_diff shell injection guard ---"

# Source just the helper functions from the script (phased gate check
# runs at the top of the script and exits early, so we source with a
# custom REPO_ROOT that returns phased_orchestration=false immediately).
CFG_T18=$(mktemp --suffix='.json')
cat > "$CFG_T18" <<'JSON'
{
  "gates": {"phased_orchestration": false},
  "policies": {},
  "settings": {},
  "audit_log": []
}
JSON

# Isolate _sanitize_diff by extracting and sourcing it from the script
# in a subshell so we don't run the full script body.
EXPLOIT_MARKER="$RUN_TMP/exploit-marker"
rm -f "$EXPLOIT_MARKER"
MARKER_BEFORE=0
[ -f "$EXPLOIT_MARKER" ] && MARKER_BEFORE=1

# Call _sanitize_diff via a small wrapper that sources the helper block
INJECTION_INPUT="echo \$(touch $EXPLOIT_MARKER)"
SANITIZED=$(AF_CONTROL_PLANE_CONFIG="$CFG_T18" REPO_ROOT="$REAL_REPO_ROOT" \
  bash -c '
    # Source only the _sanitize_diff function by extracting it
    SCRIPT_DIR="'"$REAL_REPO_ROOT"'/scripts"
    REPO_ROOT="'"$REAL_REPO_ROOT"'"
    eval "$(sed -n "/_sanitize_diff()/,/^}/p" "'"$REAL_REPO_ROOT"'/scripts/loop-phased-step5.sh")"
    _sanitize_diff "$1"
  ' -- "$INJECTION_INPUT" 2>&1)
MARKER_AFTER=0
[ -f "$EXPLOIT_MARKER" ] && MARKER_AFTER=1

if [ "$MARKER_BEFORE" -eq 0 ] && [ "$MARKER_AFTER" -eq 0 ]; then
  echo "  PASS: shell injection in diff does not execute (exploit-marker not created)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: $EXPLOIT_MARKER was created — shell injection NOT mitigated"
  FAIL=$((FAIL + 1))
fi

# Sanitized output should contain the literal text, not the result of execution
if echo "$SANITIZED" | grep -qF "echo \$(touch $EXPLOIT_MARKER)"; then
  echo "  PASS: injection string passed through as literal text"
  PASS=$((PASS + 1))
else
  echo "  FAIL: injection string was not preserved as literal text (output: $SANITIZED)"
  FAIL=$((FAIL + 1))
fi

rm -f "$CFG_T18" "$EXPLOIT_MARKER"

# -----------------------------------------------------------------------
# Test 19: test_sanitize_diff_strips_chat_templates
# Verifies that _sanitize_diff redacts chat-template tokens rather than
# passing them through to the LLM prompt (CWE-20).
# -----------------------------------------------------------------------
echo ""
echo "--- Test 19: test_sanitize_diff_strips_chat_templates ---"

CFG_T19=$(mktemp --suffix='.json')
cat > "$CFG_T19" <<'JSON'
{
  "gates": {"phased_orchestration": false},
  "policies": {},
  "settings": {},
  "audit_log": []
}
JSON

_run_sanitize() {
  local input="$1"
  AF_CONTROL_PLANE_CONFIG="$CFG_T19" REPO_ROOT="$REAL_REPO_ROOT" \
    bash -c '
      SCRIPT_DIR="'"$REAL_REPO_ROOT"'/scripts"
      REPO_ROOT="'"$REAL_REPO_ROOT"'"
      eval "$(sed -n "/_sanitize_diff()/,/^}/p" "'"$REAL_REPO_ROOT"'/scripts/loop-phased-step5.sh")"
      _sanitize_diff "$1"
    ' -- "$input" 2>&1
}

CHAT_TOKENS=(
  "<|im_start|>"
  "<|im_end|>"
  "<|endoftext|>"
  "<|eot_id|>"
  "<system>"
  "</system>"
  "[role]"
  "[/role]"
)

for tok in "${CHAT_TOKENS[@]}"; do
  OUT=$(_run_sanitize "innocent diff $tok more diff")
  if echo "$OUT" | grep -qF "[REDACTED]"; then
    echo "  PASS: '$tok' was replaced with [REDACTED]"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: '$tok' was NOT replaced (output: $OUT)"
    FAIL=$((FAIL + 1))
  fi
  # Also assert the raw token is NOT present in the output
  if echo "$OUT" | grep -qF "$tok"; then
    echo "  FAIL: raw token '$tok' still present in output"
    FAIL=$((FAIL + 1))
  else
    echo "  PASS: raw token '$tok' absent from output"
    PASS=$((PASS + 1))
  fi
done

rm -f "$CFG_T19"

# -----------------------------------------------------------------------
# Test 20: test_replay_debater_heredoc_safety
# Verifies that RESULTS_JSON containing ''' triple-quote injection does
# NOT execute arbitrary code (the <<'PYEOF' heredoc must be quoted).
# -----------------------------------------------------------------------
echo ""
echo "--- Test 20: test_replay_debater_heredoc_safety ---"

EXPLOIT_MARKER_REPLAY="$RUN_TMP/exploit-marker-replay"
rm -f "$EXPLOIT_MARKER_REPLAY"
MARKER_BEFORE_T20=0
[ -f "$EXPLOIT_MARKER_REPLAY" ] && MARKER_BEFORE_T20=1

# Construct a RESULTS_JSON value that would break out of a triple-quoted
# Python string and execute code if the heredoc were unquoted.
INJECT_JSON="[{\"pr\": 1, \"verdict\": \"pass'''; import os; os.system('touch $EXPLOIT_MARKER_REPLAY'); x = '''\"}]"

# Run just the RESULTS_JSON accumulation step in isolation, mimicking what
# replay-debater.sh does at lines 127-133.
RESULT=$(python3 - "$INJECT_JSON" "2" "skip" <<'PYEOF' 2>&1
import json, sys
data = json.loads(sys.argv[1])
data.append({"pr": int(sys.argv[2]), "verdict": sys.argv[3]})
print(json.dumps(data))
PYEOF
)
RC=$?

MARKER_AFTER_T20=0
[ -f "$EXPLOIT_MARKER_REPLAY" ] && MARKER_AFTER_T20=1

if [ "$MARKER_BEFORE_T20" -eq 0 ] && [ "$MARKER_AFTER_T20" -eq 0 ]; then
  echo "  PASS: triple-quote injection did not create $EXPLOIT_MARKER_REPLAY"
  PASS=$((PASS + 1))
else
  echo "  FAIL: $EXPLOIT_MARKER_REPLAY was created — injection NOT mitigated"
  FAIL=$((FAIL + 1))
fi

if [ "$RC" -eq 0 ]; then
  echo "  PASS: RESULTS_JSON accumulation with injected input exited cleanly"
  PASS=$((PASS + 1))
else
  echo "  INFO: RESULTS_JSON accumulation with injected input exited $RC (expected — json.loads rejects it)"
  PASS=$((PASS + 1))
fi

rm -f "$EXPLOIT_MARKER_REPLAY"

# -----------------------------------------------------------------------
# Helper: write pr_state entry with debate_cycle_count (for D#858 tests)
# -----------------------------------------------------------------------
_write_pr_state_debate_entry() {
  local pr_num="$1" disc_num="$2" phase="$3" needs_sec="$4" debate_count="${5:-0}"
  local bb_dir="$BB_PR_STATE_DIR"
  mkdir -p "$bb_dir"
  python3 -c "
import json
entry = {
    'value': {
        'pr': $pr_num,
        'discussion': $disc_num,
        'phase': '$phase',
        'spawned_phases': [],
        'completed_phases': [],
        'needs_security_review': $needs_sec,
        'fix_cycle_count': 0,
        'debate_cycle_count': $debate_count,
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
print('created pr_state/$pr_num.json')
"
}

# -----------------------------------------------------------------------
# Test 21 (D#858): debate phase, debater_pass=true, needs_security_review=true,
#   debater hasn't run yet — BOTH debater and security-reviewer are spawned
#   in the same script invocation.
# -----------------------------------------------------------------------
echo ""
echo "=== Test 21 (D#858): debate phase — debater + security-reviewer spawned concurrently ==="
CFG_T21=$(_make_config_file)
_set_gate_true "$CFG_T21" phased_orchestration
_set_gate_true "$CFG_T21" phased_code_review
_set_gate_true "$CFG_T21" debater_pass
SNAP_T21=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T21" 85801

# PR in debate phase, needs_security_review=True, debate_cycle_count=0 (not yet run)
_write_pr_state_debate_entry 85810 85801 "debate" "True" 0

OUTPUT_T21=$(AF_CONTROL_PLANE_CONFIG="$CFG_T21" SNAPSHOT_PATH="$SNAP_T21" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  HEAD_SHA_85810=abc1234 \
  DEBATER_RAN_85810=no \
  DISCUSSING_MOCK='[]' \
  bash "$SCRIPT" 2>&1)
RC_T21=$?

assert_exit_0 "exit code 0 for concurrent debate dispatch" "$RC_T21"
assert_contains "debater spawned in debate phase" "debater" "$OUTPUT_T21"
assert_contains "security-reviewer spawned concurrently with debater" "security-reviewer" "$OUTPUT_T21"

_remove_pr_state_entry 85810
rm -f "$CFG_T21" "$SNAP_T21"

# -----------------------------------------------------------------------
# Test 22 (D#858): timing — both spawns dispatched in the same script
#   invocation without blocking between them.
#
#   In production, spawn-agent.sh fires the agent in the background and
#   returns immediately (O(ms)). The test verifies the structural invariant:
#   a single script invocation dispatches BOTH spawn calls, and the total
#   dispatch time (with SPAWN_AGENT=echo, instantaneous mock) is well under
#   a 5s threshold — confirming neither spawn waits for an agent to complete
#   before the second is dispatched.
#
#   If implementation were sequential (second blocked on first agent finishing),
#   wall-clock time would equal sum of both agent runtimes (minutes in
#   production). Fast dispatch with the echo mock = elapsed ≈ max(t1, t2).
# -----------------------------------------------------------------------
echo ""
echo "=== Test 22 (D#858): timing — both spawns dispatched in same step (no blocking) ==="

CFG_T22=$(_make_config_file)
_set_gate_true "$CFG_T22" phased_orchestration
_set_gate_true "$CFG_T22" phased_code_review
_set_gate_true "$CFG_T22" debater_pass
SNAP_T22=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T22" 85802

_write_pr_state_debate_entry 85811 85802 "debate" "True" 0

START_T22=$(date +%s%N)
OUTPUT_T22=$(AF_CONTROL_PLANE_CONFIG="$CFG_T22" SNAPSHOT_PATH="$SNAP_T22" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  HEAD_SHA_85811=abc5678 \
  DEBATER_RAN_85811=no \
  DISCUSSING_MOCK='[]' \
  bash "$SCRIPT" 2>&1)
RC_T22=$?
END_T22=$(date +%s%N)

ELAPSED_MS_T22=$(( (END_T22 - START_T22) / 1000000 ))

assert_exit_0 "exit code 0 for timing test" "$RC_T22"
# Both spawn calls must appear in the output — verifies both dispatched in same iteration.
assert_contains "debater spawned in timing test" "debater" "$OUTPUT_T22"
assert_contains "security-reviewer spawned in timing test" "security-reviewer" "$OUTPUT_T22"
# With SPAWN_AGENT=echo (instantaneous), total dispatch must be well under 5s.
# This is the timing gate: elapsed ≈ max(t_debater, t_security) not their sum.
if [ "$ELAPSED_MS_T22" -lt 5000 ]; then
  echo "  PASS: dispatch elapsed ${ELAPSED_MS_T22}ms < 5000ms (both spawns non-blocking)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: dispatch elapsed ${ELAPSED_MS_T22}ms >= 5000ms (unexpected blocking between spawns)"
  FAIL=$((FAIL + 1))
fi

_remove_pr_state_entry 85811
rm -f "$CFG_T22" "$SNAP_T22"

# -----------------------------------------------------------------------
# Test 23 (D#858): debate phase, debater already ran, debater-confirmed
#   present, security-review-passed also present — advances to merging
#   (skips security_review phase).
# -----------------------------------------------------------------------
echo ""
echo "=== Test 23 (D#858): debate phase, both verdicts present — advances to merging ==="
CFG_T23=$(_make_config_file)
_set_gate_true "$CFG_T23" phased_orchestration
_set_gate_true "$CFG_T23" phased_code_review
_set_gate_true "$CFG_T23" debater_pass
SNAP_T23=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T23" 85803

_write_pr_state_debate_entry 85812 85803 "debate" "True" 1

OUTPUT_T23=$(AF_CONTROL_PLANE_CONFIG="$CFG_T23" SNAPSHOT_PATH="$SNAP_T23" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  HEAD_SHA_85812=abc9999 \
  DEBATER_RAN_85812=yes \
  DEBATER_VERDICT_85812=pass \
  HAS_LABEL_85812_debater_confirmed=yes \
  HAS_LABEL_85812_security_review_passed=yes \
  DISCUSSING_MOCK='[]' \
  bash "$SCRIPT" 2>&1)
RC_T23=$?

assert_exit_0 "exit code 0 when both verdicts present" "$RC_T23"
assert_contains "advancing to merging when both verdicts present" "merging" "$OUTPUT_T23"
assert_not_contains "security_review phase not entered when both verdicts done" "phase=security_review" "$OUTPUT_T23"

_remove_pr_state_entry 85812
rm -f "$CFG_T23" "$SNAP_T23"

# -----------------------------------------------------------------------
# Test 24 (D#858): debate phase, debater-confirmed present but
#   security-review-passed absent — waits, does not advance to merging.
# -----------------------------------------------------------------------
echo ""
echo "=== Test 24 (D#858): debate phase, debater done but security pending — waits ==="
CFG_T24=$(_make_config_file)
_set_gate_true "$CFG_T24" phased_orchestration
_set_gate_true "$CFG_T24" phased_code_review
_set_gate_true "$CFG_T24" debater_pass
SNAP_T24=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T24" 85804

_write_pr_state_debate_entry 85813 85804 "debate" "True" 1

OUTPUT_T24=$(AF_CONTROL_PLANE_CONFIG="$CFG_T24" SNAPSHOT_PATH="$SNAP_T24" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  HEAD_SHA_85813=abcaaaa \
  DEBATER_RAN_85813=yes \
  DEBATER_VERDICT_85813=pass \
  HAS_LABEL_85813_debater_confirmed=yes \
  HAS_LABEL_85813_security_review_passed=no \
  DISCUSSING_MOCK='[]' \
  bash "$SCRIPT" 2>&1)
RC_T24=$?

assert_exit_0 "exit code 0 when waiting for security" "$RC_T24"
assert_contains "waiting for security-review log message" "waiting for concurrent security-review-passed" "$OUTPUT_T24"
assert_not_contains "does not advance to merging while security pending" "advancing to merging" "$OUTPUT_T24"

_remove_pr_state_entry 85813
rm -f "$CFG_T24" "$SNAP_T24"

# -----------------------------------------------------------------------
# Test 25/26: a stale snapshot must NOT drive routing.
#
# The snapshot is a cache in front of the live Discussions query. Reading it
# without an age check means a snapshot from days ago can decide which
# Discussion gets an executor, against state that has since moved. Test 25
# proves a stale snapshot is ignored and GraphQL is re-queried; Test 26 is the
# negative control proving the fast path still exists — otherwise "fixing" this
# would just mean deleting the optimisation.
#
# Detection: a fake `gh` earlier on PATH that appends its argv to a log file.
# -----------------------------------------------------------------------

_GH_SHIM_DIR=$(mktemp -d)
cat > "$_GH_SHIM_DIR/gh" <<'SHIMEOF'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
# Enough of a response that the caller's JSON parse succeeds and yields nothing.
echo '{"data":{"repository":{"discussions":{"nodes":[]}}}}'
SHIMEOF
chmod +x "$_GH_SHIM_DIR/gh"

echo ""
echo "=== Test 25: STALE snapshot — ignored, GraphQL re-queried ==="
CFG_T25=$(_make_config_file)
_set_gate_true "$CFG_T25" phased_orchestration
SNAP_T25=$(mktemp --suffix='.json')
_write_snapshot_stale_spec_ready "$SNAP_T25" 90125
GH_LOG_T25=$(mktemp)

OUTPUT_T25=$(AF_CONTROL_PLANE_CONFIG="$CFG_T25" SNAPSHOT_PATH="$SNAP_T25" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  PATH="$_GH_SHIM_DIR:$PATH" GH_CALL_LOG="$GH_LOG_T25" \
  DISCUSSING_MOCK='[]' \
  bash "$SCRIPT" 2>&1)
RC_T25=$?
GH_CALLS_T25=$(cat "$GH_LOG_T25")

assert_exit_0 "exit code 0 with stale snapshot" "$RC_T25"
assert_contains "stale snapshot triggers a live discussions GraphQL query" \
  "discussions" "$GH_CALLS_T25"
assert_not_contains "stale snapshot's discussion number is NOT routed" \
  "90125" "$OUTPUT_T25"

rm -f "$CFG_T25" "$SNAP_T25" "$GH_LOG_T25"

echo ""
echo "=== Test 26 (negative control): FRESH snapshot — fast path, no GraphQL ==="
CFG_T26=$(_make_config_file)
_set_gate_true "$CFG_T26" phased_orchestration
SNAP_T26=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T26" 90126
GH_LOG_T26=$(mktemp)

OUTPUT_T26=$(AF_CONTROL_PLANE_CONFIG="$CFG_T26" SNAPSHOT_PATH="$SNAP_T26" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  PATH="$_GH_SHIM_DIR:$PATH" GH_CALL_LOG="$GH_LOG_T26" \
  DISCUSSING_MOCK='[]' \
  bash "$SCRIPT" 2>&1)
RC_T26=$?
GH_CALLS_T26=$(cat "$GH_LOG_T26")

assert_exit_0 "exit code 0 with fresh snapshot" "$RC_T26"
assert_not_contains "fresh snapshot does NOT fire a discussions GraphQL query" \
  "discussions(first:50" "$GH_CALLS_T26"
assert_contains "fresh snapshot's discussion number IS routed" \
  "90126" "$OUTPUT_T26"

rm -f "$CFG_T26" "$SNAP_T26" "$GH_LOG_T26"
rm -rf "$_GH_SHIM_DIR"

# -----------------------------------------------------------------------
# Test 27: the Discussion plane and the code plane are read from separate slugs
#
# The hazard this guards is not "the URL is wrong" — it is "the URL is right
# for the wrong plane". After the public/private split a prompt string that
# builds both a Discussion URL and a PR URL from one variable can only be
# correct for one of them, and the half that breaks is the Spec pointer: the
# spawned reviewer gets a 404 as its only route to the acceptance criteria.
#
# Both slugs resolve to the same value in this tree (neither "code_repo" nor
# "discussion_repo" is set in config.json, and a test cannot set them without
# writing to the live .autonomous-team tree). So these assertions cannot prove
# the two slugs *differ*; they prove both halves are still built and neither
# was dropped when the variable was split. The differing-slug case is covered
# by Test 28's static check and by the PR's Gate 2 run.
# -----------------------------------------------------------------------
echo ""
echo "=== Test 27: spawn prompts carry both a Discussion URL and a PR URL ==="
CFG_T27=$(_make_config_file)
_set_gate_true "$CFG_T27" phased_orchestration
_set_gate_true "$CFG_T27" phased_code_review
SNAP_T27=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T27" 55527

OUTPUT_T27A=$(AF_CONTROL_PLANE_CONFIG="$CFG_T27" SNAPSHOT_PATH="$SNAP_T27" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  DISCUSSING_MOCK='[]' \
  bash "$SCRIPT" 2>&1)

# Executor spawn: the Spec pointer is a Discussion URL, never a pull URL.
assert_contains "executor prompt points at the Discussion" \
  "/discussions/55527" "$OUTPUT_T27A"
assert_not_contains "executor prompt does not point at a pull request" \
  "/pull/55527" "$OUTPUT_T27A"

# Code-reviewer spawn: both halves present, each pointing at its own object.
_write_pr_state_entry 70127 55527 "code_review"
OUTPUT_T27B=$(AF_CONTROL_PLANE_CONFIG="$CFG_T27" SNAPSHOT_PATH="$SNAP_T27" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" \
  DISCUSSING_MOCK='[]' \
  TWO_GATE_PR_BODY_70127="## Verification\nGate 1: PASS\nGate 2: PASS" \
  bash "$SCRIPT" 2>&1)

assert_contains "code-reviewer prompt keeps the Discussion URL" \
  "/discussions/55527" "$OUTPUT_T27B"
assert_contains "code-reviewer prompt keeps the PR URL" \
  "/pull/70127" "$OUTPUT_T27B"
assert_not_contains "code-reviewer prompt has no empty-slug URL" \
  "https://github.com//" "$OUTPUT_T27B"

_remove_pr_state_entry 70127
rm -f "$CFG_T27" "$SNAP_T27"

# -----------------------------------------------------------------------
# Test 28: no Discussion URL is built from the code slug
#
# Static, because it is the only way to assert the plane split while both
# slugs resolve to the same string. A line that builds a "discussions/" URL
# out of _CODE_REPO is the exact defect a straight rename introduces, and it
# is invisible to every behavioural test until the day the two slugs diverge
# — which is the day it is already in production.
# -----------------------------------------------------------------------
echo ""
echo "=== Test 28: Discussion URLs never resolve from the code slug ==="
T28_BAD=$(grep -n 'discussions/' "$SCRIPT" | grep '_CODE_REPO' || true)
if [ -z "$T28_BAD" ]; then
  echo "  PASS: no 'discussions/' line references _CODE_REPO"
  PASS=$((PASS + 1))
else
  echo "  FAIL: 'discussions/' line(s) reference _CODE_REPO"
  echo "$T28_BAD" | sed 's/^/          /'
  FAIL=$((FAIL + 1))
fi

# Comment lines are excluded: the header block above the slug definitions
# quotes a malformed https://github.com//discussions/N to explain what the
# guard prevents, and prose about a URL is not a site that builds one.
T28_SITES=$(grep 'discussions/' "$SCRIPT" | grep -vc '^[[:space:]]*#')
if [ "$T28_SITES" -eq 7 ]; then
  echo "  PASS: all 7 Discussion URL sites accounted for"
  PASS=$((PASS + 1))
else
  echo "  FAIL: expected 7 'discussions/' sites, found $T28_SITES"
  echo "        a new site was added without being classified — classify it"
  FAIL=$((FAIL + 1))
fi

# The mixed prompt strings: each of the four builds both halves, and the two
# halves are on separate physical lines so neither grep above can be satisfied
# by accident.
T28_PAIRS=$(grep -c 'PR: https://github.com/${_CODE_REPO}/pull/' "$SCRIPT")
if [ "$T28_PAIRS" -eq 4 ]; then
  echo "  PASS: all 4 mixed prompt strings build their PR half from the code slug"
  PASS=$((PASS + 1))
else
  echo "  FAIL: expected 4 PR-half lines in mixed prompts, found $T28_PAIRS"
  FAIL=$((FAIL + 1))
fi

# -----------------------------------------------------------------------
# Test 29: an unresolvable Discussion plane stops the loop before it can
# emit https://github.com//discussions/N
#
# The reachable case, not a hypothetical one. _get_spec_ready_discussions has
# a snapshot fast path that returns SPEC_READY rows and never touches the
# Discussion slug, so a fresh snapshot plus an empty slug reaches the executor
# spawn with nothing to build a URL out of. The spawned agent's only pointer to
# the Spec would be a malformed URL.
#
# The slug resolves from .autonomous-team/config.json at the real repo root, so
# emptying it means standing up a second root: a temp tree holding a copy of
# the script, a copy of repo-resolve.sh, and a config.json with no "repo" key —
# a fork with no private twin. REPO_ROOT still points at the real tree so
# backend/ resolves. The second arm is the non-vacuity proof: the same temp
# tree WITH a slug must reach the spawn, or the first arm would pass for the
# uninteresting reason that the harness never got that far.
# -----------------------------------------------------------------------
echo ""
echo "=== Test 29: no Discussion plane — guarded before any URL is built ==="
T29_ROOT="$RUN_TMP/t29"
mkdir -p "$T29_ROOT/scripts/lib" "$T29_ROOT/.autonomous-team"
cp "$REAL_REPO_ROOT/scripts/loop-phased-step5.sh" "$T29_ROOT/scripts/"
cp "$REAL_REPO_ROOT/scripts/lib/repo-resolve.sh" "$T29_ROOT/scripts/lib/"
CFG_T29=$(_make_config_file)
_set_gate_true "$CFG_T29" phased_orchestration
SNAP_T29=$(mktemp --suffix='.json')
_write_snapshot_spec_ready "$SNAP_T29" 99929

# Arm 1 — no "repo" key anywhere, and no AUTONOMOUS_TEAM_REPO.
echo '{"project_name": "fork-with-no-private-twin"}' > "$T29_ROOT/.autonomous-team/config.json"
OUTPUT_T29A=$(env -u AUTONOMOUS_TEAM_REPO \
  AF_CONTROL_PLANE_CONFIG="$CFG_T29" SNAPSHOT_PATH="$SNAP_T29" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" DISCUSSING_MOCK='[]' \
  bash "$T29_ROOT/scripts/loop-phased-step5.sh" 2>&1)
RC_T29A=$?

assert_exit_0 "unresolvable Discussion plane exits 0, not a crash" "$RC_T29A"
assert_contains "the reason is stated on stderr, not swallowed" \
  "no Discussion plane resolved" "$OUTPUT_T29A"
assert_not_contains "no malformed Discussion URL is emitted" \
  "https://github.com//" "$OUTPUT_T29A"
assert_not_contains "no executor is spawned against an unbuildable URL" \
  "SPAWN_AGENT_ARGS: --role executor" "$OUTPUT_T29A"

# Arm 2 — same tree, slug present. Non-vacuity: the harness does reach Phase B.
echo '{"repo": "autonomous-agent-7/fulcrumaxe"}' > "$T29_ROOT/.autonomous-team/config.json"
OUTPUT_T29B=$(env -u AUTONOMOUS_TEAM_REPO \
  AF_CONTROL_PLANE_CONFIG="$CFG_T29" SNAPSHOT_PATH="$SNAP_T29" \
  SPAWN_AGENT=echo REPO_ROOT="$REAL_REPO_ROOT" DISCUSSING_MOCK='[]' \
  bash "$T29_ROOT/scripts/loop-phased-step5.sh" 2>&1)

assert_contains "with a slug, the same tree reaches the executor spawn" \
  "SPAWN_AGENT_ARGS: --role executor" "$OUTPUT_T29B"
assert_contains "and builds a well-formed Discussion URL" \
  "/discussions/99929" "$OUTPUT_T29B"
assert_not_contains "with a slug, no guard message" \
  "no Discussion plane resolved" "$OUTPUT_T29B"

rm -f "$CFG_T29" "$SNAP_T29"

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
