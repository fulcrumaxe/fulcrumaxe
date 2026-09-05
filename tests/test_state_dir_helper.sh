#!/usr/bin/env bash
# tests/test_state_dir_helper.sh — unit tests for scripts/lib/state-dir.sh
#
# Tests:
#   1. Exports AUTONOMOUS_TEAM_STATE_DIR when project.json has state_dir set.
#   2. No-op when AUTONOMOUS_TEAM_STATE_DIR is already set.
#   3. No-op when project.json is absent.
#   4. No-op when project.json lacks state_dir key.
#   5. No-op when state_dir is empty string.

set -uo pipefail

PASS=0
FAIL=0

_pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
_fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

# Resolve the helper's absolute path independent of cwd
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HELPER="$REPO_ROOT/scripts/lib/state-dir.sh"

if [[ ! -f "$HELPER" ]]; then
  echo "ERROR: helper not found at $HELPER" >&2
  exit 1
fi

# ── Test 1: project.json has state_dir ──────────────────────────────────────
TMP_REPO=$(mktemp -d /tmp/test-state-dir-XXXXXX)
mkdir -p "$TMP_REPO/.autonomous-team"
printf '{"state_dir": "/tmp/test-state-project-A"}' > "$TMP_REPO/.autonomous-team/project.json"

# Fake a scripts/lib/ structure pointing at the real helper by temporarily
# setting up a wrapper that overrides BASH_SOURCE so the helper resolves REPO_ROOT
# to our tmp dir.  Easier: patch the env by creating a symlink tree.
TMP_LIB="$TMP_REPO/scripts/lib"
mkdir -p "$TMP_LIB"
# The helper derives REPO_ROOT via: dirname(BASH_SOURCE[0]) → scripts/lib → ../.. → repo root.
# So place a copy of the helper inside the fake repo's scripts/lib/.
cp "$HELPER" "$TMP_LIB/state-dir.sh"

RESULT=$(bash -c "
unset AUTONOMOUS_TEAM_STATE_DIR
source '$TMP_LIB/state-dir.sh'
echo \"\${AUTONOMOUS_TEAM_STATE_DIR:-__unset__}\"
" 2>/dev/null)

if [[ "$RESULT" == "/tmp/test-state-project-A" ]]; then
  _pass "exports AUTONOMOUS_TEAM_STATE_DIR from project.json"
else
  _fail "expected /tmp/test-state-project-A, got: $RESULT"
fi

rm -rf "$TMP_REPO"

# ── Test 2: no-op when var already set ───────────────────────────────────────
TMP_REPO=$(mktemp -d /tmp/test-state-dir-XXXXXX)
mkdir -p "$TMP_REPO/.autonomous-team"
printf '{"state_dir": "/tmp/should-not-use-this"}' > "$TMP_REPO/.autonomous-team/project.json"
TMP_LIB="$TMP_REPO/scripts/lib"
mkdir -p "$TMP_LIB"
cp "$HELPER" "$TMP_LIB/state-dir.sh"

RESULT=$(bash -c "
export AUTONOMOUS_TEAM_STATE_DIR='/tmp/already-set'
source '$TMP_LIB/state-dir.sh'
echo \"\${AUTONOMOUS_TEAM_STATE_DIR:-__unset__}\"
" 2>/dev/null)

if [[ "$RESULT" == "/tmp/already-set" ]]; then
  _pass "no-op when AUTONOMOUS_TEAM_STATE_DIR already exported"
else
  _fail "expected /tmp/already-set (no-op), got: $RESULT"
fi

rm -rf "$TMP_REPO"

# ── Test 3: no-op when project.json absent ───────────────────────────────────
TMP_REPO=$(mktemp -d /tmp/test-state-dir-XXXXXX)
mkdir -p "$TMP_REPO/scripts/lib"
cp "$HELPER" "$TMP_REPO/scripts/lib/state-dir.sh"
# No .autonomous-team/project.json

RESULT=$(bash -c "
unset AUTONOMOUS_TEAM_STATE_DIR
source '$TMP_REPO/scripts/lib/state-dir.sh'
echo \"\${AUTONOMOUS_TEAM_STATE_DIR:-__unset__}\"
" 2>/dev/null)

if [[ "$RESULT" == "__unset__" ]]; then
  _pass "no-op when project.json absent"
else
  _fail "expected __unset__, got: $RESULT"
fi

rm -rf "$TMP_REPO"

# ── Test 4: no-op when project.json lacks state_dir key ──────────────────────
TMP_REPO=$(mktemp -d /tmp/test-state-dir-XXXXXX)
mkdir -p "$TMP_REPO/.autonomous-team" "$TMP_REPO/scripts/lib"
printf '{"repo": "owner/name"}' > "$TMP_REPO/.autonomous-team/project.json"
cp "$HELPER" "$TMP_REPO/scripts/lib/state-dir.sh"

RESULT=$(bash -c "
unset AUTONOMOUS_TEAM_STATE_DIR
source '$TMP_REPO/scripts/lib/state-dir.sh'
echo \"\${AUTONOMOUS_TEAM_STATE_DIR:-__unset__}\"
" 2>/dev/null)

if [[ "$RESULT" == "__unset__" ]]; then
  _pass "no-op when project.json lacks state_dir key"
else
  _fail "expected __unset__, got: $RESULT"
fi

rm -rf "$TMP_REPO"

# ── Test 5: no-op when state_dir is empty string ─────────────────────────────
TMP_REPO=$(mktemp -d /tmp/test-state-dir-XXXXXX)
mkdir -p "$TMP_REPO/.autonomous-team" "$TMP_REPO/scripts/lib"
printf '{"state_dir": ""}' > "$TMP_REPO/.autonomous-team/project.json"
cp "$HELPER" "$TMP_REPO/scripts/lib/state-dir.sh"

RESULT=$(bash -c "
unset AUTONOMOUS_TEAM_STATE_DIR
source '$TMP_REPO/scripts/lib/state-dir.sh'
echo \"\${AUTONOMOUS_TEAM_STATE_DIR:-__unset__}\"
" 2>/dev/null)

if [[ "$RESULT" == "__unset__" ]]; then
  _pass "no-op when state_dir is empty string"
else
  _fail "expected __unset__, got: $RESULT"
fi

rm -rf "$TMP_REPO"

# ── Test 6: refuses a relative state_dir, naming project.json ────────────────
# A relative value resolves against the caller's cwd, which is how runtime
# state ended up written into the repo root (D#1967).
TMP_REPO=$(mktemp -d /tmp/test-state-dir-XXXXXX)
mkdir -p "$TMP_REPO/.autonomous-team" "$TMP_REPO/scripts/lib"
printf '{"state_dir": "relstate"}' > "$TMP_REPO/.autonomous-team/project.json"
cp "$HELPER" "$TMP_REPO/scripts/lib/state-dir.sh"

STDERR=$(bash -c "
unset AUTONOMOUS_TEAM_STATE_DIR
source '$TMP_REPO/scripts/lib/state-dir.sh'
echo \"\${AUTONOMOUS_TEAM_STATE_DIR:-__unset__}\"
" 2>&1 >/dev/null)
RESULT=$(bash -c "
unset AUTONOMOUS_TEAM_STATE_DIR
source '$TMP_REPO/scripts/lib/state-dir.sh'
echo \"\${AUTONOMOUS_TEAM_STATE_DIR:-__unset__}\"
" 2>/dev/null)

if [[ "$RESULT" != *"relstate"* ]] && [[ "$STDERR" == *"project.json"* ]]; then
  _pass "refuses relative state_dir and names project.json"
else
  _fail "expected refusal naming project.json; stdout='$RESULT' stderr='$STDERR'"
fi

rm -rf "$TMP_REPO"

# ── Test 7: expands a ~-relative state_dir rather than refusing it ───────────
TMP_REPO=$(mktemp -d /tmp/test-state-dir-XXXXXX)
mkdir -p "$TMP_REPO/.autonomous-team" "$TMP_REPO/scripts/lib"
printf '{"state_dir": "~/some-state-dir"}' > "$TMP_REPO/.autonomous-team/project.json"
cp "$HELPER" "$TMP_REPO/scripts/lib/state-dir.sh"

RESULT=$(bash -c "
unset AUTONOMOUS_TEAM_STATE_DIR
source '$TMP_REPO/scripts/lib/state-dir.sh'
echo \"\${AUTONOMOUS_TEAM_STATE_DIR:-__unset__}\"
" 2>/dev/null)

if [[ "$RESULT" == "$HOME/some-state-dir" ]]; then
  _pass "expands ~-relative state_dir"
else
  _fail "expected $HOME/some-state-dir, got: $RESULT"
fi

rm -rf "$TMP_REPO"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
