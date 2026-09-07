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
# (f) + (g) Resolver-value mutations (D#2138).
#
# Both cases mutate what the helper resolves WITHOUT touching backend/ — the
# helper runs `cd "$repo_root" && python3 -c 'sys.path.insert(0, ".")'`, so
# handing it a repo root whose own backend/state_paths.py shadows the real
# one is enough to control the resolved value. backend/state_paths.py and
# backend/blackboard.py stay unmodified, per D#2119's settled decision.
#
# Case A (attribute renamed away) already failed loudly before D#2138; it is
# pinned here so it cannot quietly stop doing so.
# Case B (attribute exists, value stops being a path) did NOT fail before
# D#2138 — python3 exited 0, `|| return 1` never fired, and the helper
# returned "<function <lambda> at 0x...>/pr_state", a legal directory name
# that mkdir -p happily creates. That is the fixture-invisible-to-the-reader
# failure D#2119 measured at 52 false failures: silent, and differently named
# on every run because the repr embeds a memory address.
# -----------------------------------------------------------------------
MUTANT_ROOT="$(mktemp -d)"
# Replaces the (c) trap rather than adding a second one — a second `trap ...
# EXIT` would silently discard the first, leaving TEST_STATE_DIR behind.
trap 'rm -rf "$TEST_STATE_DIR" "$MUTANT_ROOT"' EXIT

echo ""
echo "=== (f) Case A: resolver attribute renamed away -> loud, non-zero ==="
CASE_A_ROOT="$MUTANT_ROOT/renamed-attribute"
mkdir -p "$CASE_A_ROOT/backend"
: > "$CASE_A_ROOT/backend/__init__.py"
cat > "$CASE_A_ROOT/backend/state_paths.py" <<'PY'
# Mutation: the name the helper imports has been renamed away.
BLACKBOARD_ROOT_RENAMED = "/renamed/away"
PY
CASE_A_OUT="$(blackboard_pr_state_dir "$CASE_A_ROOT" 2>"$MUTANT_ROOT/case-a.stderr")"
CASE_A_RC=$?
assert_nonzero_exit "renamed attribute returns non-zero" "$CASE_A_RC"
assert_eq "renamed attribute prints nothing on stdout" "" "$CASE_A_OUT"
assert_contains "stderr names the helper" "$(cat "$MUTANT_ROOT/case-a.stderr")" \
  "tests/lib/blackboard-fixture.sh"

# The caller idiom every sourcing suite uses. This is the part worth pinning:
# the non-zero return has to reach the caller's FATAL guard, not merely exist.
CASE_A_CALLER_OUT="$( (
  BB="$(blackboard_pr_state_dir "$CASE_A_ROOT" 2>/dev/null)" \
    || { echo "FATAL: could not resolve blackboard pr_state dir"; exit 1; }
  echo "caller continued with BB=$BB"
) 2>&1 )"
CASE_A_CALLER_RC=$?
assert_nonzero_exit "caller's FATAL guard fires (renamed attribute)" "$CASE_A_CALLER_RC"
assert_contains "caller printed FATAL" "$CASE_A_CALLER_OUT" "FATAL:"

echo ""
echo "=== (g) Case B: resolver value is not a path -> loud, non-zero ==="
CASE_B_ROOT="$MUTANT_ROOT/non-path-value"
mkdir -p "$CASE_B_ROOT/backend"
: > "$CASE_B_ROOT/backend/__init__.py"
cat > "$CASE_B_ROOT/backend/state_paths.py" <<'PY'
# Mutation: the name still resolves, but to a callable rather than a path —
# the lazy-accessor refactor D#2138 describes. print() succeeds and python3
# exits 0, so nothing upstream of the helper's own validation notices.
BLACKBOARD_DIR = lambda: "/some/path"
PY
CASE_B_OUT="$(blackboard_pr_state_dir "$CASE_B_ROOT" 2>"$MUTANT_ROOT/case-b.stderr")"
CASE_B_RC=$?
CASE_B_ERR="$(cat "$MUTANT_ROOT/case-b.stderr")"
assert_nonzero_exit "non-path resolver value returns non-zero" "$CASE_B_RC"
assert_eq "non-path resolver value prints nothing on stdout" "" "$CASE_B_OUT"
assert_contains "stderr names the helper" "$CASE_B_ERR" "tests/lib/blackboard-fixture.sh"
# "names the value it got" — the message has to show the offending value, or
# the reader cannot tell a bad resolver from a bad repo root.
assert_contains "stderr names the value it got" "$CASE_B_ERR" "<function"
# The pre-fix behaviour, asserted as absent: a fixture directory built by
# concatenation from a value that is not a path.
case "$CASE_B_OUT" in
  */pr_state)
    echo "  FAIL: helper returned a fixture directory built from a non-path value: '$CASE_B_OUT'"
    FAIL=$((FAIL + 1));;
  *)
    echo "  PASS: helper returned no fixture directory for a non-path value"
    PASS=$((PASS + 1));;
esac

CASE_B_CALLER_OUT="$( (
  BB="$(blackboard_pr_state_dir "$CASE_B_ROOT" 2>/dev/null)" \
    || { echo "FATAL: could not resolve blackboard pr_state dir"; exit 1; }
  echo "caller continued with BB=$BB"
) 2>&1 )"
CASE_B_CALLER_RC=$?
assert_nonzero_exit "caller's FATAL guard fires (non-path value)" "$CASE_B_CALLER_RC"
assert_contains "caller printed FATAL" "$CASE_B_CALLER_OUT" "FATAL:"

# -----------------------------------------------------------------------
# (h) The accepted value is checked as a PROPERTY, and the expected value
# comes from the resolver rather than from a copy of its precedence.
#
# No literal prefix is asserted: under blackboard_scratch_state_dir the root
# is a `mktemp -d` path whose prefix varies by host and by TMPDIR, so a test
# pinning /tmp or /home is green on one machine and red on the next.
# -----------------------------------------------------------------------
echo ""
echo "=== (h) accepted value is non-empty and absolute, matching the resolver ==="
unset AUTONOMOUS_TEAM_STATE_DIR
blackboard_scratch_state_dir   # direct call — never via $( ), or the export is lost
PROP_SCRATCH="${AUTONOMOUS_TEAM_STATE_DIR:-}"
PROP_OUT="$(blackboard_pr_state_dir "$REAL_REPO_ROOT")"
PROP_RC=$?
assert_exit_0 "happy path under a scratch state dir exits 0" "$PROP_RC"
case "$PROP_OUT" in
  /*) echo "  PASS: returned value is non-empty and absolute"; PASS=$((PASS + 1));;
  *)  echo "  FAIL: returned value '$PROP_OUT' is not a non-empty absolute path"; FAIL=$((FAIL + 1));;
esac
# Same expression the helper evaluates — not a third copy of the precedence.
PROP_EXPECTED="$(cd "$REAL_REPO_ROOT" && python3 -c '
import sys
sys.path.insert(0, ".")
from backend.state_paths import BLACKBOARD_DIR
print(BLACKBOARD_DIR)
')/pr_state"
assert_eq "returned value equals the resolver's own answer" "$PROP_EXPECTED" "$PROP_OUT"
rm -rf "$PROP_SCRATCH"
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
