#!/usr/bin/env bash
# tests/test_blackboard_fixture_helper.sh — regression suite for
# tests/lib/blackboard-fixture.sh (D#2279).
#
# The helper used to read backend.blackboard._DEFAULT_ROOT, a private
# module attribute deleted by ae080c8a (PR #2182, 2026-08-23). That broke
# every suite that sources it without a single assertion running. This
# suite exists so the next time someone refactors a private attribute the
# helper depends on, this fails loudly instead of three unrelated suites
# failing silently at setup.
#
# Run: bash tests/test_blackboard_fixture_helper.sh

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/blackboard-fixture.sh
source "$REAL_REPO_ROOT/tests/lib/blackboard-fixture.sh"

PASS=0
FAIL=0

assert_exit_0() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then echo "  PASS: $label (exit 0)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 0, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_nonzero_exit() {
  local label="$1" rc="$2"
  if [ "$rc" -ne 0 ]; then echo "  PASS: $label (exit $rc)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected non-zero exit, got 0)"; FAIL=$((FAIL + 1)); fi
}

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $label"; PASS=$((PASS + 1));
  else
    echo "  FAIL: $label (expected '$expected', got '$actual')"; FAIL=$((FAIL + 1));
  fi
}

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "  PASS: $label"; PASS=$((PASS + 1));
  else
    echo "  FAIL: $label (expected output to contain '$needle')"; FAIL=$((FAIL + 1));
  fi
}

# -----------------------------------------------------------------------
# (a) Happy path: exits 0, prints a non-empty path ending in pr_state
# -----------------------------------------------------------------------
echo "=== (a) resolves successfully against the real repo ==="
OUT_A="$(blackboard_pr_state_dir "$REAL_REPO_ROOT")"
RC_A=$?
assert_exit_0 "blackboard_pr_state_dir exits 0" "$RC_A"
if [ -n "$OUT_A" ]; then echo "  PASS: output is non-empty ($OUT_A)"; PASS=$((PASS + 1));
else echo "  FAIL: output is empty"; FAIL=$((FAIL + 1)); fi
case "$OUT_A" in
  */blackboard/pr_state) echo "  PASS: output ends in /blackboard/pr_state"; PASS=$((PASS + 1));;
  *) echo "  FAIL: output '$OUT_A' does not end in /blackboard/pr_state"; FAIL=$((FAIL + 1));;
esac

# -----------------------------------------------------------------------
# (b) Equality with backend.blackboard's own resolution — this is the
# structural guarantee: the helper evaluates the same expression the code
# under test evaluates, so it cannot drift from it silently.
# -----------------------------------------------------------------------
echo ""
echo "=== (b) matches backend.blackboard._resolve_default_root() ==="
PY_OUT="$(cd "$REAL_REPO_ROOT" && python3 -c '
import sys
sys.path.insert(0, ".")
import backend.blackboard as b
print(b._resolve_default_root())
')/pr_state"
assert_eq "helper output equals backend.blackboard's own resolution" "$PY_OUT" "$OUT_A"

# -----------------------------------------------------------------------
# (c) AUTONOMOUS_TEAM_STATE_DIR override is honoured at call time, not
# frozen from a prior call in the same process.
# -----------------------------------------------------------------------
echo ""
echo "=== (c) AUTONOMOUS_TEAM_STATE_DIR override honoured at call time ==="
TEST_STATE_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_STATE_DIR"' EXIT
export AUTONOMOUS_TEAM_STATE_DIR="$TEST_STATE_DIR"
OUT_SET="$(blackboard_pr_state_dir "$REAL_REPO_ROOT")"
assert_eq "override reflected in output" "$AUTONOMOUS_TEAM_STATE_DIR/blackboard/pr_state" "$OUT_SET"
unset AUTONOMOUS_TEAM_STATE_DIR
OUT_UNSET="$(blackboard_pr_state_dir "$REAL_REPO_ROOT")"
if [ "$OUT_UNSET" != "$OUT_SET" ]; then
  echo "  PASS: unsetting the override changes the resolved path (no freeze)"; PASS=$((PASS + 1));
else
  echo "  FAIL: output unchanged after unsetting override — looks frozen"; FAIL=$((FAIL + 1));
fi
assert_eq "unset case matches backend.blackboard's own resolution" "$PY_OUT" "$OUT_UNSET"

# -----------------------------------------------------------------------
# (d) Guard against cross-module private attribute access ever coming
# back. This is the regression the whole suite exists to catch: the next
# time someone refactors a private attribute of another module, this
# fails instead of three unrelated suites failing at setup.
# -----------------------------------------------------------------------
echo ""
echo "=== (d) no cross-module private attribute access in the helper source ==="
HELPER_SRC="$REAL_REPO_ROOT/tests/lib/blackboard-fixture.sh"
if grep -nE '\b[A-Za-z_]+\._[A-Za-z_]' "$HELPER_SRC" >/dev/null; then
  echo "  FAIL: found a private cross-module attribute reference in $HELPER_SRC"; FAIL=$((FAIL + 1));
else
  echo "  PASS: no private cross-module attribute reference found"; PASS=$((PASS + 1));
fi
DEFAULT_ROOT_COUNT="$(grep -c '_DEFAULT_ROOT' "$HELPER_SRC")"
if [ "$DEFAULT_ROOT_COUNT" -eq 0 ]; then
  echo "  PASS: _DEFAULT_ROOT is not referenced"; PASS=$((PASS + 1));
else
  echo "  FAIL: _DEFAULT_ROOT is referenced in $HELPER_SRC"; FAIL=$((FAIL + 1));
fi

# -----------------------------------------------------------------------
# (e) blackboard_scratch_state_dir (D#2283) — must be called directly, not
# via command substitution, since `export` inside a function invoked as
# `x=$(fn)` runs in a subshell and is lost on return. Verify it leaves
# AUTONOMOUS_TEAM_STATE_DIR pointed at a fresh scratch dir in THIS shell,
# and that blackboard_pr_state_dir then resolves under it — never under
# ~/.autonomous-forever-state.
# -----------------------------------------------------------------------
echo ""
echo "=== (e) blackboard_scratch_state_dir redirects the resolver, never to production ==="
unset AUTONOMOUS_TEAM_STATE_DIR
blackboard_scratch_state_dir
SCRATCH_RC=$?
assert_exit_0 "blackboard_scratch_state_dir exits 0" "$SCRATCH_RC"
if [ -n "${AUTONOMOUS_TEAM_STATE_DIR:-}" ] && [ -d "$AUTONOMOUS_TEAM_STATE_DIR" ]; then
  echo "  PASS: AUTONOMOUS_TEAM_STATE_DIR is set to an existing directory in this shell"; PASS=$((PASS + 1));
else
  echo "  FAIL: AUTONOMOUS_TEAM_STATE_DIR is unset or not a directory after a direct call"; FAIL=$((FAIL + 1));
fi
SCRATCH_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-}"
OUT_SCRATCH="$(blackboard_pr_state_dir "$REAL_REPO_ROOT")"
case "$OUT_SCRATCH" in
  "$SCRATCH_DIR"/*) echo "  PASS: resolved pr_state dir is under the scratch dir"; PASS=$((PASS + 1));;
  *) echo "  FAIL: resolved pr_state dir '$OUT_SCRATCH' is not under scratch dir '$SCRATCH_DIR'"; FAIL=$((FAIL + 1));;
esac
case "$OUT_SCRATCH" in
  "$HOME"/.autonomous-forever-state/*) echo "  FAIL: resolved pr_state dir is under the production state dir"; FAIL=$((FAIL + 1));;
  *) echo "  PASS: resolved pr_state dir is never under ~/.autonomous-forever-state"; PASS=$((PASS + 1));;
esac
rm -rf "$SCRATCH_DIR"
unset AUTONOMOUS_TEAM_STATE_DIR

# -----------------------------------------------------------------------
# Bonus: the self-naming diagnostic on failure (Spec item 10) — force a
# resolution failure with a repo_root that has no backend/ package, and
# confirm the helper names itself in stderr rather than surfacing a bare
# traceback, and that it still returns non-zero so callers' `|| exit 1`
# guards keep firing.
# -----------------------------------------------------------------------
echo ""
echo "=== (bonus) failure path names the helper and returns non-zero ==="
FAIL_STDERR="$(blackboard_pr_state_dir /tmp 2>&1 1>/dev/null)"
FAIL_RC=$?
assert_nonzero_exit "resolution failure returns non-zero" "$FAIL_RC"
assert_contains "stderr names the helper" "$FAIL_STDERR" "tests/lib/blackboard-fixture.sh"

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  echo "PRESUM: fail step=test_blackboard_fixture_helper exit=1 checks=$((PASS + FAIL))"
  exit 1
fi
echo "PRESUM: pass"
exit 0
