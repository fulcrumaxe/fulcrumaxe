#!/usr/bin/env bash
# tests/test_pre_spawn_check_budget_guard.sh
#
# D#2063: `backend/budget.py check` PRINTS its JSON verdict to stdout AND
# separately signals exhaustion via exit 1 -- exit 1 means "the read
# succeeded and the budget is exhausted", not "the read failed". The old
# `scripts/pre-spawn-check.sh` line
#
#   BUDGET_JSON=$(python3 backend/budget.py check 2>/dev/null || echo '{"allowed":true,"remaining":0}')
#
# treated that exit 1 as a command-substitution failure, so on a real
# exhausted budget the fallback JSON got appended *after* the real JSON that
# had already printed. Two concatenated JSON objects don't parse, so the
# next line's own `|| echo "true"` fired and silently approved the spawn at
# the exact moment the budget said no.
#
# This suite runs the REAL scripts/pre-spawn-check.sh against the REAL
# backend/budget.py (not a mock) with a scratch AUTONOMOUS_TEAM_STATE_DIR
# per check, mirroring the exact commands the frozen Spec's Acceptance
# section specifies verbatim. See tests/test_pre_spawn_check_block_events.sh
# for the mocked-budget.py coverage of the block-event/feed-schema surface.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0

ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

TMPDIR_BASE=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_BASE"; }
trap cleanup EXIT

echo "=== test_pre_spawn_check_budget_guard ==="
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Check 1 (Spec item 1): exhausted budget -> non-zero exit AND stderr
# contains "budget exceeded". Both halves are required -- omitting
# --event-id would exit 2 at argument validation before the budget check
# ever runs, which is a non-zero exit for the wrong reason.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- Check 1: exhausted budget blocks the real spawn ---"

STATE1=$(mktemp -d "$TMPDIR_BASE/state1-XXXXXX")
AUTONOMOUS_TEAM_STATE_DIR="$STATE1" python3 "$REPO_ROOT/backend/budget.py" init --ceiling 1 >/dev/null

OUT1="$TMPDIR_BASE/out1.log"
ERR1="$TMPDIR_BASE/err1.log"
AUTONOMOUS_TEAM_STATE_DIR="$STATE1" bash "$REPO_ROOT/scripts/pre-spawn-check.sh" \
  --role executor --discussion 2063 --event-id "budget-guard-check1-$$" \
  >"$OUT1" 2>"$ERR1"
RC1=$?

if [[ "$RC1" -ne 0 ]]; then
  ok "check1a: exits non-zero (rc=$RC1)"
else
  fail "check1a: expected non-zero exit, got 0"
fi

if grep -q "budget exceeded" "$ERR1"; then
  ok "check1b: stderr contains 'budget exceeded'"
else
  fail "check1b: stderr missing 'budget exceeded': $(cat "$ERR1")"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Check 2 (Spec item 2): healthy budget, --dry-run -> exits 0. No over-block.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- Check 2: healthy budget does not over-block ---"

STATE2=$(mktemp -d "$TMPDIR_BASE/state2-XXXXXX")
AUTONOMOUS_TEAM_STATE_DIR="$STATE2" python3 "$REPO_ROOT/backend/budget.py" init --ceiling 5000000 >/dev/null

ERR2="$TMPDIR_BASE/err2.log"
AUTONOMOUS_TEAM_STATE_DIR="$STATE2" bash "$REPO_ROOT/scripts/pre-spawn-check.sh" \
  --role executor --discussion 2063 --dry-run \
  >"$TMPDIR_BASE/out2.log" 2>"$ERR2"
RC2=$?

if [[ "$RC2" -eq 0 ]]; then
  ok "check2: healthy budget + dry-run exits 0 (rc=$RC2)"
else
  fail "check2: expected exit 0, got $RC2. stderr: $(cat "$ERR2")"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Check 3 (Spec item 3): BUDGET_JSON is valid JSON in the exhausted case.
# This is exactly the value scripts/pre-spawn-check.sh captures internally
# (python3 backend/budget.py check, stdout only) -- reproduced directly here
# against the same exhausted state dir from Check 1.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- Check 3: BUDGET_JSON parses as valid JSON in the exhausted case ---"

BUDGET_JSON_RAW=$(AUTONOMOUS_TEAM_STATE_DIR="$STATE1" python3 "$REPO_ROOT/backend/budget.py" check 2>/dev/null)
echo "$BUDGET_JSON_RAW" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>"$TMPDIR_BASE/check3.err"
RC3=$?

if [[ "$RC3" -eq 0 ]]; then
  ok "check3: captured BUDGET_JSON parses as valid JSON"
else
  fail "check3: BUDGET_JSON failed to parse: $(cat "$TMPDIR_BASE/check3.err") -- raw: $BUDGET_JSON_RAW"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Check 4 (Spec item 4, mutation proof, required): revert the guard to the
# original `... || echo '{"allowed":true,"remaining":0}'` form and re-run
# Check 1 -- it must fail. Proves the fix, not just the code shape, is what
# makes Check 1 pass. Restores the real file when done, even on failure.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- Check 4: mutation proof -- reverting the fix makes Check 1 fail ---"

TARGET="$REPO_ROOT/scripts/pre-spawn-check.sh"
BACKUP="$TMPDIR_BASE/pre-spawn-check.sh.orig"
cp "$TARGET" "$BACKUP"
restore_target() { cp "$BACKUP" "$TARGET"; }
trap 'restore_target; cleanup' EXIT

python3 - "$TARGET" << 'MUTATE_PY'
import re, sys

path = sys.argv[1]
src = open(path).read()

old_block_start = src.index("_budget_err=$(mktemp)")
old_block_end = src.index('rm -f "$_budget_err"') + len('rm -f "$_budget_err"')

mutant = (
    'BUDGET_JSON=$(python3 "$REPO_ROOT/backend/budget.py" check 2>/dev/null '
    '|| echo \'{"allowed":true,"remaining":0}\')\n'
    'BUDGET_ALLOWED=$(echo "$BUDGET_JSON" | python3 -c "import sys,json; '
    'd=json.load(sys.stdin); print(str(d.get(\'allowed\',\'true\')).lower())" '
    '2>/dev/null || echo "true")\n'
    'BUDGET_REMAINING=$(echo "$BUDGET_JSON" | python3 -c "import sys,json; '
    'd=json.load(sys.stdin); print(d.get(\'remaining\',0))" 2>/dev/null || echo "0")'
)

new_src = src[:old_block_start] + mutant + src[old_block_end:]
if new_src == src:
    sys.exit("mutation: no change applied -- marker text not found")
open(path, "w").write(new_src)
MUTATE_PY
MUTATE_RC=$?

if [[ "$MUTATE_RC" -ne 0 ]]; then
  fail "check4: could not apply mutation (marker text not found) -- treating as unfalsifiable, NOT restoring is skipped since nothing changed"
else
  bash -n "$TARGET" 2>"$TMPDIR_BASE/mutant-syntax.err"
  if [[ $? -ne 0 ]]; then
    fail "check4: mutant script has a syntax error: $(cat "$TMPDIR_BASE/mutant-syntax.err")"
  else
    ERR4="$TMPDIR_BASE/err4.log"
    AUTONOMOUS_TEAM_STATE_DIR="$STATE1" bash "$TARGET" \
      --role executor --discussion 2063 --event-id "budget-guard-check4-$$" \
      >"$TMPDIR_BASE/out4.log" 2>"$ERR4"
    RC4=$?

    # The original bug: exits 0 (not blocked) with no "budget exceeded" text,
    # on the SAME exhausted state dir that correctly blocked in Check 1.
    if [[ "$RC4" -eq 0 ]] && ! grep -q "budget exceeded" "$ERR4"; then
      ok "check4: mutation reproduces the original bug -- rc=$RC4, no 'budget exceeded' in stderr (proves Check 1 is falsifiable)"
    else
      fail "check4: expected the mutant to silently approve (rc=0, no 'budget exceeded'); got rc=$RC4 stderr=$(cat "$ERR4")"
    fi
  fi
fi

restore_target
trap cleanup EXIT

bash -n "$TARGET"
if [[ $? -eq 0 ]]; then
  ok "check4-restore: original fixed file restored and parses cleanly"
else
  fail "check4-restore: restored file failed bash -n -- CHECK scripts/pre-spawn-check.sh MANUALLY"
fi

echo ""
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
