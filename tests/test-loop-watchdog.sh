#!/usr/bin/env bash
# tests/test-loop-watchdog.sh — fixture-driven tests for scripts/loop-watchdog.sh
#
# Tests:
#   1. Stale file → watchdog logs STALE decision and prints trigger command (dry-run)
#   2. Fresh file → watchdog logs OK, does NOT print trigger command
#   3. Missing file → watchdog logs STALE (missing) and prints trigger command (dry-run)
#   4. Kill switch → LOOP_WATCHDOG_DISABLED=1 exits without firing
#   5. Cooldown → skips trigger when last fire was within cooldown window
#   6. Concurrency guard → second instance exits when first holds the lock
#
# Does NOT invoke trigger.py for real. Always passes --dry-run where applicable.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHDOG="$REPO_ROOT/scripts/loop-watchdog.sh"

PASS=0
FAIL=0
ERRORS=()

# ── Helpers ───────────────────────────────────────────────────────────────

pass() { echo "PASS: $1"; (( PASS++ )) || true; }
fail() { echo "FAIL: $1"; (( FAIL++ )) || true; ERRORS+=("$1"); }

# Create a temp dir that cleans up on exit
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

# ── Test 1: Stale file → STALE logged, trigger printed ───────────────────

METRICS_FILE_1="$TMPDIR_TEST/loop-metrics-stale.jsonl"
LOG_FILE_1="$TMPDIR_TEST/watchdog-stale.log"

# Create file and backdate its mtime to 40 minutes ago
printf '{"timestamp":"2000-01-01T00:00:00Z"}\n' > "$METRICS_FILE_1"
touch -t "$(date -d '40 minutes ago' +%Y%m%d%H%M.%S 2>/dev/null \
  || python3 -c "
from datetime import datetime, timedelta, timezone
t = datetime.now(timezone.utc) - timedelta(minutes=40)
print(t.strftime('%Y%m%d%H%M.%S'))
" 2>/dev/null || echo "200001010000.00")" "$METRICS_FILE_1"

OUTPUT_1=$(LOOP_METRICS_FILE="$METRICS_FILE_1" \
  AUTONOMOUS_TEAM_STATE_DIR="$TMPDIR_TEST" \
  REPO_ROOT="$TMPDIR_TEST" \
  bash "$WATCHDOG" --dry-run 2>&1)

LOG_1="$TMPDIR_TEST/.autonomous-team/loop-watchdog.log"

# Assert log contains STALE
if grep -q "STALE" "$LOG_1" 2>/dev/null; then
  pass "test-1: stale file logged STALE"
else
  fail "test-1: expected STALE in log but got: $(cat "$LOG_1" 2>/dev/null || echo '<no log>')"
fi

# Assert stdout contains DRY-RUN trigger line
if echo "$OUTPUT_1" | grep -q "DRY-RUN trigger:"; then
  pass "test-1: dry-run trigger command printed to stdout"
else
  fail "test-1: expected DRY-RUN trigger in stdout but got: $OUTPUT_1"
fi

# Assert trigger.py NOT actually executed (no process, just printed)
if echo "$OUTPUT_1" | grep -q "trigger.py"; then
  pass "test-1: trigger command contains trigger.py path"
else
  fail "test-1: trigger command missing trigger.py — got: $OUTPUT_1"
fi

# ── Test 2: Fresh file → OK logged, no trigger ───────────────────────────

METRICS_FILE_2="$TMPDIR_TEST/loop-metrics-fresh.jsonl"
LOG_DIR_2="$TMPDIR_TEST/fresh-run"
mkdir -p "$LOG_DIR_2/.autonomous-team"

# Create file with current mtime
printf '{"timestamp":"2000-01-01T00:00:00Z"}\n' > "$METRICS_FILE_2"

OUTPUT_2=$(LOOP_METRICS_FILE="$METRICS_FILE_2" \
  AUTONOMOUS_TEAM_STATE_DIR="$TMPDIR_TEST" \
  REPO_ROOT="$LOG_DIR_2" \
  bash "$WATCHDOG" --dry-run 2>&1)

LOG_2="$LOG_DIR_2/.autonomous-team/loop-watchdog.log"

# Assert log contains OK
if grep -q "^.*OK" "$LOG_2" 2>/dev/null; then
  pass "test-2: fresh file logged OK"
else
  fail "test-2: expected OK in log but got: $(cat "$LOG_2" 2>/dev/null || echo '<no log>')"
fi

# Assert stdout does NOT contain DRY-RUN trigger
if echo "$OUTPUT_2" | grep -q "DRY-RUN trigger:"; then
  fail "test-2: unexpected DRY-RUN trigger in stdout for fresh file"
else
  pass "test-2: no trigger for fresh file"
fi

# ── Test 3: Missing file → STALE (not found) logged, trigger printed ─────

LOG_DIR_3="$TMPDIR_TEST/missing-run"
mkdir -p "$LOG_DIR_3"

OUTPUT_3=$(LOOP_METRICS_FILE="$TMPDIR_TEST/does-not-exist.jsonl" \
  AUTONOMOUS_TEAM_STATE_DIR="$TMPDIR_TEST" \
  REPO_ROOT="$LOG_DIR_3" \
  bash "$WATCHDOG" --dry-run 2>&1)

LOG_3="$LOG_DIR_3/.autonomous-team/loop-watchdog.log"

if grep -q "STALE" "$LOG_3" 2>/dev/null; then
  pass "test-3: missing file logged STALE"
else
  fail "test-3: expected STALE in log for missing file but got: $(cat "$LOG_3" 2>/dev/null || echo '<no log>')"
fi

if echo "$OUTPUT_3" | grep -q "DRY-RUN trigger:"; then
  pass "test-3: dry-run trigger printed for missing file"
else
  fail "test-3: expected DRY-RUN trigger for missing file but got: $OUTPUT_3"
fi

# ── Test 4: Kill switch — LOOP_WATCHDOG_DISABLED=1 exits without firing ──

LOG_DIR_4="$TMPDIR_TEST/killswitch-run"
mkdir -p "$LOG_DIR_4"
METRICS_FILE_4="$TMPDIR_TEST/loop-metrics-ks.jsonl"
printf '{"timestamp":"2000-01-01T00:00:00Z"}\n' > "$METRICS_FILE_4"
touch -t "200001010000.00" "$METRICS_FILE_4"  # very old — would normally fire

OUTPUT_4=$(LOOP_WATCHDOG_DISABLED=1 \
  LOOP_METRICS_FILE="$METRICS_FILE_4" \
  AUTONOMOUS_TEAM_STATE_DIR="$TMPDIR_TEST" \
  REPO_ROOT="$LOG_DIR_4" \
  bash "$WATCHDOG" 2>&1)

LOG_4="$LOG_DIR_4/.autonomous-team/loop-watchdog.log"

if grep -q "DISABLED" "$LOG_4" 2>/dev/null; then
  pass "test-4: kill switch logged DISABLED"
else
  fail "test-4: expected DISABLED in log but got: $(cat "$LOG_4" 2>/dev/null || echo '<no log>')"
fi

if echo "$OUTPUT_4" | grep -q "DRY-RUN trigger:\|trigger.py"; then
  fail "test-4: kill switch should suppress trigger but got: $OUTPUT_4"
else
  pass "test-4: kill switch suppressed trigger"
fi

# ── Test 5: Cooldown — skip if last fire was within cooldown window ───────

LOG_DIR_5="$TMPDIR_TEST/cooldown-run"
mkdir -p "$LOG_DIR_5/.autonomous-team"
METRICS_FILE_5="$TMPDIR_TEST/loop-metrics-cd.jsonl"
printf '{"timestamp":"2000-01-01T00:00:00Z"}\n' > "$METRICS_FILE_5"
touch -t "200001010000.00" "$METRICS_FILE_5"  # very old — would normally fire

# Write a last-fire timestamp of 2 minutes ago (well within 15m cooldown)
RECENT_EPOCH=$(( $(date +%s) - 120 ))
printf '%d\n' "$RECENT_EPOCH" > "$LOG_DIR_5/.autonomous-team/loop-watchdog.last-fire"

OUTPUT_5=$(LOOP_METRICS_FILE="$METRICS_FILE_5" \
  AUTONOMOUS_TEAM_STATE_DIR="$TMPDIR_TEST" \
  REPO_ROOT="$LOG_DIR_5" \
  WATCHDOG_COOLDOWN_MINUTES=15 \
  bash "$WATCHDOG" 2>&1)

LOG_5="$LOG_DIR_5/.autonomous-team/loop-watchdog.log"

if grep -q "COOLDOWN" "$LOG_5" 2>/dev/null; then
  pass "test-5: cooldown skip logged COOLDOWN"
else
  fail "test-5: expected COOLDOWN in log but got: $(cat "$LOG_5" 2>/dev/null || echo '<no log>')"
fi

# ── Test 6: Concurrency guard — second instance exits when first holds lock ──

LOG_DIR_6="$TMPDIR_TEST/concurrency-run"
mkdir -p "$LOG_DIR_6/.autonomous-team"
METRICS_FILE_6="$TMPDIR_TEST/loop-metrics-cc.jsonl"
LOCK_6="$TMPDIR_TEST/loop-watchdog-test.lock"

printf '{"timestamp":"2000-01-01T00:00:00Z"}\n' > "$METRICS_FILE_6"

# Hold the lock in a background subshell, then run the watchdog
# The watchdog must exit 0 with a SKIPPED log line.
(
  exec 9>"$LOCK_6"
  flock -n 9 || exit 1
  sleep 3
) &
LOCK_BG_PID=$!
# Give the background shell a moment to acquire
sleep 0.3

OUTPUT_6=$(LOOP_METRICS_FILE="$METRICS_FILE_6" \
  AUTONOMOUS_TEAM_STATE_DIR="$TMPDIR_TEST" \
  REPO_ROOT="$LOG_DIR_6" \
  bash "$WATCHDOG" --dry-run 2>&1) || true
  # Note: this block doesn't override WATCHDOG_LOCK_FILE, so it isn't
  # actually exercising a held lock against this watchdog invocation — see
  # the CC block below for the real concurrency-guard assertion.

# Clean up background lock holder
wait "$LOCK_BG_PID" 2>/dev/null || true
rm -f "$LOCK_6"

# For this test we verify the watchdog handles a pre-existing lock on its own lock file.
# Simulate: hold the watchdog's lock file and run a second instance against
# the SAME path via WATCHDOG_LOCK_FILE — a suite-scoped path rather than the
# real default /tmp/loop-watchdog.lock, so two concurrently-running copies
# of this suite (e.g. two reviewers) don't race each other on it (D#2254).
SECOND_LOG_DIR="$TMPDIR_TEST/concurrency-second"
mkdir -p "$SECOND_LOG_DIR/.autonomous-team"
METRICS_FILE_CC="$TMPDIR_TEST/loop-metrics-cc2.jsonl"
LOCK_CC="$TMPDIR_TEST/loop-watchdog-cc.lock"
printf '{"timestamp":"2000-01-01T00:00:00Z"}\n' > "$METRICS_FILE_CC"
touch -t "200001010000.00" "$METRICS_FILE_CC"

# Hold the watchdog's lock file from a background process
(
  exec 9>"$LOCK_CC"
  flock -n 9 || exit 1
  sleep 5
) &
HOLDER_PID=$!
sleep 0.3

OUTPUT_CC=$(LOOP_METRICS_FILE="$METRICS_FILE_CC" \
  AUTONOMOUS_TEAM_STATE_DIR="$TMPDIR_TEST" \
  REPO_ROOT="$SECOND_LOG_DIR" \
  WATCHDOG_LOCK_FILE="$LOCK_CC" \
  bash "$WATCHDOG" 2>&1) || true

wait "$HOLDER_PID" 2>/dev/null || true

LOG_CC="$SECOND_LOG_DIR/.autonomous-team/loop-watchdog.log"

if grep -q "SKIPPED" "$LOG_CC" 2>/dev/null; then
  pass "test-6: concurrent second instance logged SKIPPED"
else
  fail "test-6: expected SKIPPED for locked instance but got: $(cat "$LOG_CC" 2>/dev/null || echo '<no log>')"
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ "$FAIL" -gt 0 ]]; then
  echo "Failed tests:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi

exit 0
