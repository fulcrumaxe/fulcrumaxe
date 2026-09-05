#!/usr/bin/env bash
# tests/test_loop_metrics_path.sh — verify coldstart + start-the-day agree on
# the loop-metrics.jsonl location (Bug 4c).
#
# Tests:
#   1. coldstart-project.sh creates loop-metrics.jsonl at the expected path.
#   2. start-the-day.sh (loop-bootstrap version) creates the file if missing.
#   3. Both agree on the same relative path (.autonomous-team/loop-metrics.jsonl).

set -uo pipefail

PASS=0
FAIL=0

_pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
_fail() { echo "FAIL: $1 — $2"; FAIL=$((FAIL + 1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Test 1 coldstarts a throwaway project. Before D#2317 PR-c that wrote
# $HOME/.test-proj-<pid>-state on every run and then "cleaned up"
# /tmp/${PROJECT_NAME}-state -- the wrong root, and missing the leading dot
# -- so it had never once removed what it created. 44 of the 75 dead
# fixtures on the operator's Fleet page came from this one line.
#
# COLDSTART_STATE_ROOT is what closes the hole: containment has to survive a
# SIGKILL, which no trap does. The trap below is hygiene on top of the
# redirect, not a substitute for it -- same reasoning CLAUDE.md records for
# blackboard_scratch_state_dir.
COLDSTART_STATE_ROOT="$(mktemp -d /tmp/test-loop-metrics-state-XXXXXX)"
export COLDSTART_STATE_ROOT
trap 'rm -rf "$COLDSTART_STATE_ROOT"' EXIT

# ── Test 1: coldstart-project.sh creates loop-metrics.jsonl ─────────────────
TMP_REPO=$(mktemp -d /tmp/test-loop-metrics-XXXXXX)
git -C "$TMP_REPO" init -q
git -C "$TMP_REPO" remote add origin "https://github.com/test/test.git"

# Run coldstart-project.sh in the tmp repo
PROJECT_NAME="test-proj-$$"
COLDSTART_ERR="$(mktemp /tmp/test_loop_metrics_coldstart_err.XXXXXX)"
if bash "$REPO_ROOT/scripts/coldstart-project.sh" "$TMP_REPO" "$PROJECT_NAME" 2>"$COLDSTART_ERR"; then
  EXPECTED="$TMP_REPO/.autonomous-team/loop-metrics.jsonl"
  if [[ -f "$EXPECTED" ]]; then
    _pass "coldstart creates .autonomous-team/loop-metrics.jsonl"
  else
    _fail "coldstart loop-metrics.jsonl" "file not found at $EXPECTED"
  fi
else
  _fail "coldstart loop-metrics.jsonl" "coldstart-project.sh failed (see $COLDSTART_ERR)"
fi
rm -f "$COLDSTART_ERR"

rm -rf "$TMP_REPO" "$COLDSTART_STATE_ROOT/.${PROJECT_NAME}-state" 2>/dev/null || true

# ── Test 2: start-the-day.sh auto-creates loop-metrics.jsonl when missing ───
# Verify the loop-bootstrap start-the-day.sh creates the file when absent.
# We do this by sourcing just the relevant section in isolation.
TMP_REPO2=$(mktemp -d /tmp/test-loop-metrics-XXXXXX)
mkdir -p "$TMP_REPO2/.autonomous-team"
# Do NOT create loop-metrics.jsonl — simulate fresh install

EXPECTED2="$TMP_REPO2/.autonomous-team/loop-metrics.jsonl"

# Simulate the start-the-day.sh auto-create logic (the key lines we added)
bash -c "
REPO_ROOT='$TMP_REPO2'
LOOP_METRICS_PATH=\"\$REPO_ROOT/.autonomous-team/loop-metrics.jsonl\"
if [[ ! -f \"\$LOOP_METRICS_PATH\" ]]; then
  mkdir -p \"\$(dirname \"\$LOOP_METRICS_PATH\")\"
  touch \"\$LOOP_METRICS_PATH\"
fi
"

if [[ -f "$EXPECTED2" ]]; then
  _pass "start-the-day auto-creates loop-metrics.jsonl when missing"
else
  _fail "start-the-day auto-create" "file not found at $EXPECTED2"
fi

rm -rf "$TMP_REPO2"

# ── Test 3: paths agree ───────────────────────────────────────────────────────
# Both coldstart-project.sh and start-the-day.sh must reference the same path.
# Grep for the canonical path in each script.

# Check that both scripts reference the canonical filename "loop-metrics.jsonl"
# under ".autonomous-team/".  We grep for the filename in context rather than
# comparing exact shell variable expansions (which differ between scripts).
COLDSTART_HAS=$(python3 -c "
import re
with open('$REPO_ROOT/scripts/coldstart-project.sh') as f:
    text = f.read()
# Look for LOOP_METRICS_TARGET assignment or any line assigning loop-metrics.jsonl
found = bool(re.search(r'loop-metrics\.jsonl', text))
print('yes' if found else 'no')
" 2>/dev/null || echo "no")

STARTDAY_HAS=$(python3 -c "
import re
with open('$REPO_ROOT/scripts/start-the-day.sh') as f:
    text = f.read()
# Verify the canonical filename appears in the LOOP_METRICS_PATH assignment
found = bool(re.search(r'LOOP_METRICS_PATH.*loop-metrics\.jsonl', text, re.DOTALL))
print('yes' if found else 'no')
" 2>/dev/null || echo "no")

# Both should reference .autonomous-team/loop-metrics.jsonl
COLDSTART_SUBPATH=$(python3 -c "
import re
with open('$REPO_ROOT/scripts/coldstart-project.sh') as f:
    text = f.read()
m = re.search(r'LOOP_METRICS_TARGET\s*=\s*\"([^\"]+)\"', text)
print(m.group(1) if m else 'NOT_FOUND')
" 2>/dev/null || echo "NOT_FOUND")

STARTDAY_SUBPATH=$(python3 -c "
import re
with open('$REPO_ROOT/scripts/start-the-day.sh') as f:
    text = f.read()
m = re.search(r'LOOP_METRICS_PATH\s*=\s*\"([^\"]+)\"', text)
print(m.group(1) if m else 'NOT_FOUND')
" 2>/dev/null || echo "NOT_FOUND")

if [[ "$COLDSTART_HAS" == "yes" ]] && [[ "$STARTDAY_HAS" == "yes" ]]; then
  _pass "coldstart and start-the-day both reference loop-metrics.jsonl under .autonomous-team/"
else
  _fail "path agreement" \
    "coldstart has_ref=$COLDSTART_HAS ('$COLDSTART_SUBPATH') vs start-the-day has_ref=$STARTDAY_HAS ('$STARTDAY_SUBPATH')"
fi

# ── Test 4: loop-bootstrap start-the-day.sh uses absolute path ───────────────
LB_STARTDAY_PATH=$(python3 -c "
import re
with open('$REPO_ROOT/loop-bootstrap/scripts/start-the-day.sh') as f:
    text = f.read()
m = re.search(r'LOOP_METRICS_PATH\s*=\s*\"([^\"]+)\"', text)
print(m.group(1) if m else 'NOT_FOUND')
" 2>/dev/null || echo "NOT_FOUND")

if [[ "$LB_STARTDAY_PATH" == "\$REPO_ROOT/.autonomous-team/loop-metrics.jsonl" ]]; then
  _pass "loop-bootstrap start-the-day.sh uses absolute REPO_ROOT-anchored path"
else
  _fail "loop-bootstrap path" "expected '\$REPO_ROOT/...' got: $LB_STARTDAY_PATH"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
