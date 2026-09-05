#!/usr/bin/env bash
# tests/test_worktree_ground_check.sh — both-directions proof for
# scripts/lib/worktree-ground-check.sh (D#1809 Lane B).
#
# wt_ground_intact <dir> must tell a runner whether the directory a command
# just ran in is still a resolvable git working tree — the gap this closes
# is a run whose cwd vanished mid-run once reading as 489 ordinary test
# failures on a clean branch (a corrupted run, not a real one).
#
#   B1 (block):  a command that deletes its own working directory mid-run
#                flips the predicate from intact to not-intact.
#   B2 (permit): a live, undisturbed worktree reads intact both before and
#                after — the false-positive check, so a healthy run is
#                never flagged.
#
# Entirely self-contained: every directory this test creates or destroys is
# a fresh mktemp dir under /tmp. Never touches this checkout or any real
# worktree under .claude/worktrees/.
#
# Exit code: 0 = all tests passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUND_CHECK_LIB="${REPO_ROOT}/scripts/lib/worktree-ground-check.sh"

PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

# shellcheck source=scripts/lib/worktree-ground-check.sh
source "$GROUND_CHECK_LIB"

# ---------------------------------------------------------------------------
# B1 (block) — a command that deletes its own working directory mid-run.
# ---------------------------------------------------------------------------
echo ""
echo "=== B1: predicate flips when the working directory vanishes mid-run ==="

B1_DIR=$(mktemp -d /tmp/test-wt-ground-b1-XXXXXX)
git init --quiet "$B1_DIR"

if wt_ground_intact "$B1_DIR"; then
  _pass "B1: intact before the command runs"
else
  _fail "B1: intact before the command runs"
fi

# The harness deletes its own $PWD and exits 0 — a clean exit from a command
# whose ground disappeared out from under it, exactly the corrupting case
# this predicate exists to catch.
( cd "$B1_DIR" && bash -c 'rm -rf "$PWD"; exit 0' )

if wt_ground_intact "$B1_DIR"; then
  _fail "B1: not intact after the directory was removed mid-run"
else
  _pass "B1: not intact after the directory was removed mid-run"
fi

# ---------------------------------------------------------------------------
# B2 (permit) — a live worktree that is not deleted stays intact.
# ---------------------------------------------------------------------------
echo ""
echo "=== B2: predicate does not false-positive on a healthy, undisturbed dir ==="

B2_DIR=$(mktemp -d /tmp/test-wt-ground-b2-XXXXXX)
trap 'rm -rf "$B2_DIR"' EXIT
git init --quiet "$B2_DIR"

if wt_ground_intact "$B2_DIR"; then
  _pass "B2: intact before"
else
  _fail "B2: intact before"
fi

# A harmless command that touches nothing about the directory's existence.
( cd "$B2_DIR" && bash -c 'echo hello > /dev/null' )

if wt_ground_intact "$B2_DIR"; then
  _pass "B2: intact after"
else
  _fail "B2: intact after"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "==========================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "==========================================="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
