#!/usr/bin/env bash
# tests/test_fixture_cleanup.sh
#
# Guards against tests/test_coldstart_backlog_template.sh leaking the temp
# dirs its mkfixture() helper creates. mkfixture used to append to a global
# FIXTURES array from inside `D="$(mkfixture)"` -- command substitution runs
# the function in a subshell, so the append mutated a copy that died with the
# subshell. FIXTURES stayed empty and the EXIT trap's cleanup loop iterated
# over nothing. Every run leaked its fixture dirs permanently.
#
# This points TMPDIR at an empty scratch dir, shims `rm` onto PATH so every
# deletion the suite makes is logged before it happens, runs the real
# backlog-template suite against that TMPDIR, and asserts:
#   1. the suite actually created (and tried to clean up) fixture dirs --
#      otherwise "nothing leaked" would be vacuously true for a suite that
#      never ran
#   2. nothing is left under the scratch TMPDIR once the suite exits
#
# Run: bash tests/test_fixture_cleanup.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUITE="$REPO_ROOT/tests/test_coldstart_backlog_template.sh"

PASS=0
FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; [[ $# -gt 1 ]] && echo "        $2"; FAIL=$((FAIL + 1)); }

# A dedicated scratch TMPDIR -- the host's real /tmp is shared with other
# concurrent processes, which would make a before/after delta assertion flaky.
SCRATCH_ROOT="$(mktemp -d)"
SCRATCH_TMPDIR="$SCRATCH_ROOT/tmp"
SHIM_BIN="$SCRATCH_ROOT/bin"
RM_LOG="$SCRATCH_ROOT/rm.log"
SUITE_LOG="$SCRATCH_ROOT/suite.log"
mkdir -p "$SCRATCH_TMPDIR" "$SHIM_BIN"
: > "$RM_LOG"

cleanup() { rm -rf -- "$SCRATCH_ROOT"; }
trap cleanup EXIT

# Shim `rm` onto PATH ahead of the real one so we can observe, after the
# fact, every path the suite under test tried to delete -- without changing
# what it actually does (the shim still calls through to the real rm).
REAL_RM="$(command -v rm)"
{
  echo '#!/usr/bin/env bash'
  printf 'printf '\''%%s\n'\'' "$*" >> %q\n' "$RM_LOG"
  printf 'exec %q "$@"\n' "$REAL_RM"
} > "$SHIM_BIN/rm"
chmod +x "$SHIM_BIN/rm"

PATH="$SHIM_BIN:$PATH" TMPDIR="$SCRATCH_TMPDIR" bash "$SUITE" > "$SUITE_LOG" 2>&1
SUITE_RC=$?

echo ""
echo "=== the suite under test ran to completion ==="
if [[ "$SUITE_RC" -eq 0 ]]; then
  ok "test_coldstart_backlog_template.sh exited 0"
else
  bad "test_coldstart_backlog_template.sh exited 0" "got exit $SUITE_RC -- see $SUITE_LOG"
fi

echo ""
echo "=== the leak test can actually observe a leak (fixtures were created) ==="
FIXTURE_DELETIONS="$(grep -c -- "-- $SCRATCH_TMPDIR/" "$RM_LOG" 2>/dev/null || true)"
FIXTURE_DELETIONS="${FIXTURE_DELETIONS:-0}"
if [[ "$FIXTURE_DELETIONS" -ge 19 ]]; then
  ok "the suite created (and tried to clean up) at least 19 fixture dirs (saw $FIXTURE_DELETIONS)"
else
  bad "the suite created (and tried to clean up) at least 19 fixture dirs" "saw $FIXTURE_DELETIONS -- log: $RM_LOG"
fi

echo ""
echo "=== no fixture directories survive the suite's EXIT trap ==="
LEFTOVER="$(find "$SCRATCH_TMPDIR" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$LEFTOVER" -eq 0 ]]; then
  ok "scratch TMPDIR is empty after the suite exits"
else
  bad "scratch TMPDIR is empty after the suite exits" "found $LEFTOVER leftover entries"
  find "$SCRATCH_TMPDIR" -mindepth 1 -maxdepth 1
fi

echo ""
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
