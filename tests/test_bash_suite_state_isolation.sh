#!/usr/bin/env bash
# tests/test_bash_suite_state_isolation.sh — D#2148: prove that a bash suite
# shelling out to a state-writing backend/*.py tool leaves the real
# ~/.autonomous-forever-state/ byte-identical.
#
# backend/state_paths.py's production guard (UnsandboxedStatePathError) only
# fires when PYTEST_CURRENT_TEST is set. A bash suite that shells out to
# `python3 backend/<tool>.py` never sets that, so an un-isolated suite falls
# straight through to the real state dir. The fix is NOT another heuristic in
# state_paths.py (a wrong guess there blocks a real production write, the
# dangerous direction) — it's the suite exporting AUTONOMOUS_TEAM_STATE_DIR
# itself before any such call, via tests/lib/blackboard-fixture.sh's
# blackboard_scratch_state_dir (CLAUDE.md, D#2283).
#
# This is a real run + a before/after diff on the real directory, not a stub
# check. Asserting that a stub was invoked instead is exactly the weaker
# claim that let 33 synthetic rows land in production flaky-history.jsonl
# (see the D#2148 filing).
#
# Covers both halves of the gap measured for D#2148:
#   1. the python-tool path — tests/test_hooks_idempotency.sh's AC9 test
#      shells out to backend/budget.py (fixed in this same PR to export a
#      scratch state dir before that call).
#   2. the bash-hook path — tests/smoke-3spawn-d984.sh drives
#      scripts/subagent-stop-hook.sh directly; its noise-drop path used to
#      write unknown_subagent_stops-*.jsonl with no isolation (the
#      counter-example measured while speccing D#2168, which fixed it via
#      SUBAGENT_STOP_REPO_ROOT_OVERRIDE). Re-run here as a regression guard
#      so a future edit that reintroduces an un-isolated write is caught.
#
# Usage: bash tests/test_bash_suite_state_isolation.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# The real, production state dir — resolved the same way backend/state_paths.py
# resolves it (AUTONOMOUS_TEAM_STATE_DIR unset falls back to
# ~/.autonomous-forever-state/), read through the module itself so this file
# never hardcodes that path a second time. Explicitly unset both env vars
# this process may have inherited, so the snapshot always targets the real
# directory regardless of how this suite itself was invoked.
REAL_STATE_DIR="$(env -u AUTONOMOUS_TEAM_STATE_DIR -u PYTEST_CURRENT_TEST \
  python3 "$REPO_ROOT/backend/state_paths.py")"

PASS=0
FAIL=0
TEST_NAME=""
pass() { echo "  PASS: $TEST_NAME"; PASS=$((PASS + 1)); }
fail() {
  echo "  FAIL: $TEST_NAME — $*"
  FAIL=$((FAIL + 1))
}

# snapshot_dir DIR — file list (relative paths, sorted) plus a per-file line
# count. wc -l works on binary files too (it counts newline bytes), so this
# is a cheap content-change detector across the whole tree, not just the two
# append-only logs the filing named explicitly.
snapshot_dir() {
  local dir="$1"
  if [[ ! -d "$dir" ]]; then
    echo "__MISSING__:$dir"
    return
  fi
  (
    cd "$dir" && find . -type f | LC_ALL=C sort | while IFS= read -r f; do
      printf '%s %s\n' "$f" "$(wc -l <"$f" 2>/dev/null || echo ERR)"
    done
  )
}

assert_snapshot_unchanged() {
  local before="$1" after="$2"
  if diff -q "$before" "$after" >/dev/null 2>&1; then
    pass
  else
    fail "$REAL_STATE_DIR changed — see diff below"
    diff "$before" "$after" >&2 | head -20 >&2 || true
  fi
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ── Scenario 1: python-tool path (backend/budget.py via a bash suite) ──────

TEST_NAME="scenario 1: test_hooks_idempotency.sh (backend/budget.py) leaves the real state dir untouched"

snapshot_dir "$REAL_STATE_DIR" >"$TMP/before-1.txt"
env -u AUTONOMOUS_TEAM_STATE_DIR -u PYTEST_CURRENT_TEST \
  timeout --kill-after=5s 90 bash "$REPO_ROOT/tests/test_hooks_idempotency.sh" \
  >"$TMP/suite-1.log" 2>&1
SUITE1_RC=$?
snapshot_dir "$REAL_STATE_DIR" >"$TMP/after-1.txt"

if [[ "$SUITE1_RC" -ne 0 ]]; then
  fail "test_hooks_idempotency.sh itself failed (exit $SUITE1_RC)"
  cat "$TMP/suite-1.log" >&2
else
  assert_snapshot_unchanged "$TMP/before-1.txt" "$TMP/after-1.txt"
fi

# ── Scenario 2: bash-hook path (scripts/subagent-stop-hook.sh via a smoke suite) ──

TEST_NAME="scenario 2: smoke-3spawn-d984.sh (scripts/subagent-stop-hook.sh) leaves the real state dir untouched"

snapshot_dir "$REAL_STATE_DIR" >"$TMP/before-2.txt"
env -u AUTONOMOUS_TEAM_STATE_DIR -u PYTEST_CURRENT_TEST \
  timeout --kill-after=5s 60 bash "$REPO_ROOT/tests/smoke-3spawn-d984.sh" \
  >"$TMP/suite-2.log" 2>&1
SUITE2_RC=$?
snapshot_dir "$REAL_STATE_DIR" >"$TMP/after-2.txt"

if [[ "$SUITE2_RC" -ne 0 ]]; then
  fail "smoke-3spawn-d984.sh itself failed (exit $SUITE2_RC)"
  cat "$TMP/suite-2.log" >&2
else
  assert_snapshot_unchanged "$TMP/before-2.txt" "$TMP/after-2.txt"
fi

echo ""
echo "=== Results ==="
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [[ "$FAIL" -gt 0 ]]; then
  echo "FAILED" >&2
  exit 1
fi
echo "ALL TESTS PASSED"
exit 0
