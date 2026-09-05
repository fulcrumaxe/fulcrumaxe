#!/usr/bin/env bash
# tests/test_hooks_idempotency.sh — idempotency and crash-safety tests for hook scripts.
#
# Tests scenarios from Discussion #338 AC 1, 2, 3, 6:
#   1. Crash recovery: kill after budget step, resume from KPI
#   2. Concurrent dedup: two parallel calls with same event_id → one side effect
#   3. Pure idempotency: two sequential calls → second is no-op
#   6. Replay: marker with partial steps → replay completes remaining
#
# Run from repo root:
#   bash tests/test_hooks_idempotency.sh
#
# Requires: bash 4+, python3, flock, sha256sum

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK_LIB="$REPO_ROOT/scripts/lib/hook-event.sh"

# D#2267: this used to point HOOK_EVENT_DIR at the live
# $REPO_ROOT/.autonomous-team/hook-events — the marker/lock files every
# real hook_event_init call also writes into. hook_event_init already
# "respect[s] externally-set HOOK_EVENT_DIR (e.g. in tests)"
# (scripts/lib/hook-event.sh), and scripts/replay-hook-event.sh (exercised
# by Test 5 below) now honours the same variable, so redirecting it here is
# enough to isolate both from the live tree.
FIXTURE_ROOT="$(mktemp -d "$REPO_ROOT/.repo-root-fixture.XXXXXX")" || {
  echo "FAIL: could not create isolated repo-root fixture" >&2
  exit 1
}
trap 'rm -rf "$FIXTURE_ROOT"' EXIT
HOOK_EVENTS_DIR="$FIXTURE_ROOT/.autonomous-team/hook-events"

PASS=0
FAIL=0
TEST_NAME=""

pass() { echo "  PASS: $TEST_NAME"; ((PASS++)) || true; }
fail() { echo "  FAIL: $TEST_NAME — $*"; ((FAIL++)) || true; }

# ── Test helpers ──────────────────────────────────────────────────────────────

assert_file_exists() {
  local path="$1"
  if [[ -f "$path" ]]; then
    pass
  else
    fail "expected file to exist: $path"
  fi
}

assert_step_completed() {
  local marker="$1"
  local step="$2"
  if python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if sys.argv[2] in d.get('steps_completed',[]) else 1)
" "$marker" "$step" 2>/dev/null; then
    pass
  else
    fail "step '$step' not in steps_completed in $marker"
  fi
}

assert_step_not_completed() {
  local marker="$1"
  local step="$2"
  if ! python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if sys.argv[2] in d.get('steps_completed',[]) else 1)
" "$marker" "$step" 2>/dev/null; then
    pass
  else
    fail "step '$step' should NOT be in steps_completed in $marker"
  fi
}

assert_marker_in_done() {
  local event_id="$1"
  local done_marker="$HOOK_EVENTS_DIR/done/${event_id}.json"
  if [[ -f "$done_marker" ]]; then
    pass
  else
    fail "expected done marker: $done_marker"
  fi
}

assert_marker_not_in_done() {
  local event_id="$1"
  local done_marker="$HOOK_EVENTS_DIR/done/${event_id}.json"
  if [[ ! -f "$done_marker" ]]; then
    pass
  else
    fail "marker should NOT be in done yet: $done_marker"
  fi
}

cleanup_event() {
  local event_id="$1"
  rm -f "$HOOK_EVENTS_DIR/${event_id}.json" \
        "$HOOK_EVENTS_DIR/${event_id}.json.tmp" \
        "$HOOK_EVENTS_DIR/${event_id}.lock" \
        "$HOOK_EVENTS_DIR/done/${event_id}.json" 2>/dev/null || true
}

# Minimal hook that uses hook-event.sh for testing without real subsystem calls
make_test_hook() {
  local hook_name="$1"
  local steps="$2"
  local script_path="$3"

  cat > "$script_path" << HOOK_SCRIPT
#!/usr/bin/env bash
set -uo pipefail
REPO_ROOT="$REPO_ROOT"
export HOOK_ROLE="\${HOOK_ROLE:-test-role}"
export HOOK_DISCUSSION="\${HOOK_DISCUSSION:-999}"
export HOOK_PR="\${HOOK_PR:-}"
export HOOK_VERDICT="\${HOOK_VERDICT:-done}"
export HOOK_CALLER="test-hook"
# Explicitly set event dir so hooks running from /tmp find the right .autonomous-team/
export HOOK_EVENT_DIR="$HOOK_EVENTS_DIR"

source "$HOOK_LIB"

EVENT_ID_ARG="\${1:-}"
RESUME_FLAG="\${2:-}"
INIT_ARGS=()
[[ -n "\$EVENT_ID_ARG" ]] && INIT_ARGS+=(--event-id "\$EVENT_ID_ARG")
[[ "\$RESUME_FLAG" == "--resume" ]] && INIT_ARGS+=(--resume)

hook_event_init "$hook_name" "$steps" "\${INIT_ARGS[@]:-}"

IFS=',' read -ra STEPS_ARR <<< "$steps"
for step in "\${STEPS_ARR[@]}"; do
  step="\${step// /}"  # trim spaces
  if ! hook_event_has_step "\$step"; then
    # Simulate step work
    sleep 0.05
    hook_event_mark_step "\$step"
    echo "ran step: \$step"
  else
    echo "skipped step: \$step (already done)"
  fi
done

hook_event_finish
echo "hook done"
HOOK_SCRIPT
  chmod +x "$script_path"
}

# ── Setup ─────────────────────────────────────────────────────────────────────

mkdir -p "$HOOK_EVENTS_DIR/done"
TMP_HOOK=$(mktemp "/tmp/test-hook-XXXXXX.sh")
# Captured hook stdout for the concurrency/idempotency tests below lives
# under one mktemp'd dir rather than fixed /tmp/hook-out-N-$$.txt names
# (D#2254).
RUN_TMP="$(mktemp -d /tmp/test_hooks_idempotency.XXXXXX)"
# Bash traps replace rather than stack — re-declare to also clean up
# $FIXTURE_ROOT (registered near the top of this file) instead of silently
# dropping that cleanup.
trap 'rm -f "$TMP_HOOK"; rm -rf "$RUN_TMP" "$FIXTURE_ROOT"' EXIT

echo "=== Hook Idempotency Tests ==="
echo "HOOK_EVENTS_DIR: $HOOK_EVENTS_DIR"

# ── Test 1: Crash recovery (AC 1) ────────────────────────────────────────────
# Simulate: run hook, complete only first 2 steps manually (budget, circuit_breaker),
# then verify that re-run with same event_id only runs remaining steps.

TEST_NAME="AC1: crash recovery — resume from partial marker"

STEPS="step_a,step_b,step_c,step_d"
EVENT_ID="test-crash-$(date +%s%N | sha256sum | cut -c1-16)"
cleanup_event "$EVENT_ID"

# Source lib to write a partial marker directly
(
  export HOOK_ROLE="executor"
  export HOOK_DISCUSSION="999"
  export HOOK_PR=""
  export HOOK_VERDICT="done"
  export HOOK_CALLER="test"
  export HOOK_EVENT_DIR="$HOOK_EVENTS_DIR"
  source "$HOOK_LIB"
  hook_event_init "test-hook" "$STEPS" --event-id "$EVENT_ID" > /dev/null
  hook_event_mark_step "step_a"
  hook_event_mark_step "step_b"
  # Exit WITHOUT calling hook_event_finish (crash simulation)
  flock -u ${HOOK_EVENT_FD:-200} 2>/dev/null || true
  exec ${HOOK_EVENT_FD:-200}>&- 2>/dev/null || true
  trap - EXIT INT TERM 2>/dev/null || true
) 2>/dev/null || true

# Verify partial marker exists with 2 completed steps
MARKER="$HOOK_EVENTS_DIR/${EVENT_ID}.json"
if [[ -f "$MARKER" ]]; then
  COMPLETED=$(python3 -c "import json; d=json.load(open('$MARKER')); print(','.join(d.get('steps_completed',[])))" 2>/dev/null || echo "")
  if [[ "$COMPLETED" == "step_a,step_b" ]]; then
    pass
  else
    fail "expected 'step_a,step_b' completed, got '$COMPLETED'"
  fi
else
  fail "partial marker not created at $MARKER"
fi

TEST_NAME="AC1: resume skips completed steps"

make_test_hook "test-hook" "$STEPS" "$TMP_HOOK"
OUTPUT=$(bash "$TMP_HOOK" "$EVENT_ID" --resume 2>/dev/null)

# Should have skipped step_a and step_b, run step_c and step_d
if echo "$OUTPUT" | grep -q "skipped step: step_a" && \
   echo "$OUTPUT" | grep -q "skipped step: step_b" && \
   echo "$OUTPUT" | grep -q "ran step: step_c" && \
   echo "$OUTPUT" | grep -q "ran step: step_d"; then
  pass
else
  fail "unexpected step execution pattern. Output: $OUTPUT"
fi

TEST_NAME="AC1: marker in done after resume"
assert_marker_in_done "$EVENT_ID"
cleanup_event "$EVENT_ID"

# ── Test 2: Concurrent dedup (AC 2) ──────────────────────────────────────────

TEST_NAME="AC2: concurrent invocations — only one runs side effects"

STEPS="step_a,step_b,step_c"
EVENT_ID="test-conc-$(date +%s%N | sha256sum | cut -c1-16)"
cleanup_event "$EVENT_ID"

make_test_hook "test-hook" "$STEPS" "$TMP_HOOK"

# Run two hooks in parallel with same event_id
bash "$TMP_HOOK" "$EVENT_ID" > $RUN_TMP/hook-out-1.txt 2>/dev/null &
PID1=$!
bash "$TMP_HOOK" "$EVENT_ID" > $RUN_TMP/hook-out-2.txt 2>/dev/null &
PID2=$!
wait $PID1 || true
wait $PID2 || true

OUT1=$(cat $RUN_TMP/hook-out-1.txt 2>/dev/null || echo "")
OUT2=$(cat $RUN_TMP/hook-out-2.txt 2>/dev/null || echo "")
rm -f $RUN_TMP/hook-out-1.txt $RUN_TMP/hook-out-2.txt

# Count how many "ran step:" lines appear across BOTH outputs
TOTAL_RAN=$(echo -e "$OUT1\n$OUT2" | grep -c "ran step:" 2>/dev/null || echo "0")

# Should have exactly 3 "ran step:" lines total (one set, not doubled)
if [[ "$TOTAL_RAN" -le 3 ]]; then
  pass
else
  fail "expected <= 3 'ran step:' lines (one set), got $TOTAL_RAN"
fi

TEST_NAME="AC2: marker in done after concurrent run"
assert_marker_in_done "$EVENT_ID"
cleanup_event "$EVENT_ID"

# ── Test 3: Pure idempotency (AC 3) ──────────────────────────────────────────

TEST_NAME="AC3: sequential idempotency — second call is no-op"

STEPS="step_a,step_b"
EVENT_ID="test-idem-$(date +%s%N | sha256sum | cut -c1-16)"
cleanup_event "$EVENT_ID"

make_test_hook "test-hook" "$STEPS" "$TMP_HOOK"

# First call
bash "$TMP_HOOK" "$EVENT_ID" > $RUN_TMP/hook-out-3.txt 2>/dev/null
OUT1=$(cat $RUN_TMP/hook-out-3.txt)
rm -f $RUN_TMP/hook-out-3.txt

# Second call (should be no-op exit 0)
SECOND_EXIT=0
bash "$TMP_HOOK" "$EVENT_ID" > $RUN_TMP/hook-out-4.txt 2>/dev/null || SECOND_EXIT=$?
OUT2=$(cat $RUN_TMP/hook-out-4.txt 2>/dev/null || echo "")
rm -f $RUN_TMP/hook-out-4.txt

if [[ "$SECOND_EXIT" -eq 0 ]]; then
  pass
else
  fail "second call should exit 0, got $SECOND_EXIT"
fi

TEST_NAME="AC3: second call produces no 'ran step:' output"
if echo "$OUT2" | grep -q "ran step:"; then
  fail "second call re-ran steps — not idempotent. Output: $OUT2"
else
  pass
fi

cleanup_event "$EVENT_ID"

# ── Test 4: Marker lifecycle (AC 4) ──────────────────────────────────────────

TEST_NAME="AC4: marker moves to done/ after completion"

STEPS="step_a"
EVENT_ID="test-lifecycle-$(date +%s%N | sha256sum | cut -c1-16)"
cleanup_event "$EVENT_ID"

make_test_hook "test-hook" "$STEPS" "$TMP_HOOK"
bash "$TMP_HOOK" "$EVENT_ID" > /dev/null 2>/dev/null

# Active marker should NOT exist (moved to done/)
if [[ -f "$HOOK_EVENTS_DIR/${EVENT_ID}.json" ]]; then
  fail "active marker should be gone after completion"
else
  pass
fi

TEST_NAME="AC4: done marker exists after completion"
assert_marker_in_done "$EVENT_ID"
cleanup_event "$EVENT_ID"

# ── Test 5: Replay tool (AC 6) ────────────────────────────────────────────────

TEST_NAME="AC6: replay-hook-event.sh identifies incomplete steps"

STEPS="step_a,step_b,step_c"
EVENT_ID="test-replay-$(date +%s%N | sha256sum | cut -c1-16)"
cleanup_event "$EVENT_ID"

# Write partial marker (step_a + step_b done, step_c not yet)
(
  export HOOK_ROLE="executor"
  export HOOK_DISCUSSION="999"
  export HOOK_PR=""
  export HOOK_VERDICT="done"
  export HOOK_CALLER="test"
  export HOOK_EVENT_DIR="$HOOK_EVENTS_DIR"
  source "$HOOK_LIB"
  hook_event_init "test-hook" "$STEPS" --event-id "$EVENT_ID" > /dev/null
  hook_event_mark_step "step_a"
  hook_event_mark_step "step_b"
  # Simulate partial — release flock without finish
  flock -u ${HOOK_EVENT_FD:-200} 2>/dev/null || true
  exec ${HOOK_EVENT_FD:-200}>&- 2>/dev/null || true
  trap - EXIT INT TERM 2>/dev/null || true
) 2>/dev/null || true

# Verify replay script can read the marker and detect incomplete steps.
# HOOK_EVENT_DIR must be exported here too, not just inside the subshell
# above — a subshell's exports don't survive past its own `)`, and
# replay-hook-event.sh is a separate subprocess that only sees this
# fixture's marker if it inherits the same override.
export HOOK_EVENT_DIR="$HOOK_EVENTS_DIR"
REPLAY_OUT=$(bash "$REPO_ROOT/scripts/replay-hook-event.sh" "$EVENT_ID" 2>&1 || true)
if echo "$REPLAY_OUT" | grep -qE "Incomplete steps:.*step_c"; then
  pass
else
  fail "replay should detect step_c as incomplete. Output: $REPLAY_OUT"
fi

cleanup_event "$EVENT_ID"

# ── Test 6: SQLite dedup (AC 9) ──────────────────────────────────────────────

TEST_NAME="AC9: budget.py record --event-id dedup — 100 tokens once not twice"

EVENT_ID="test-dedup-$(date +%s%N | sha256sum | cut -c1-16)"
# Remove from seen db if exists
(cd "$REPO_ROOT" && python3 -c "
import sqlite3
from pathlib import Path
db=Path('.autonomous-team/hook-events/seen.sqlite')
if db.exists():
  conn=sqlite3.connect(str(db))
  conn.execute(\"DELETE FROM seen_events WHERE event_id=?\",('$EVENT_ID',))
  conn.commit()
  conn.close()
" 2>/dev/null || true)

# Ensure budget session is initialized (must init to reset spent counter for clean test)
(cd "$REPO_ROOT" && python3 backend/budget.py init 2>/dev/null) || true

BEFORE=$(cd "$REPO_ROOT" && python3 backend/budget.py status 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('spent',0))" 2>/dev/null || echo "0")

# First call — should record 100 tokens
(cd "$REPO_ROOT" && python3 backend/budget.py record \
  --input-tokens 100 --output-tokens 0 \
  --role test-role \
  --event-id "$EVENT_ID") 2>/dev/null || true

# Second call — should be a no-op (duplicate event_id)
(cd "$REPO_ROOT" && python3 backend/budget.py record \
  --input-tokens 100 --output-tokens 0 \
  --role test-role \
  --event-id "$EVENT_ID") 2>/dev/null || true

AFTER=$(cd "$REPO_ROOT" && python3 backend/budget.py status 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('spent',0))" 2>/dev/null || echo "0")
DELTA=$(( AFTER - BEFORE ))

if [[ "$DELTA" -eq 100 ]]; then
  pass
else
  fail "expected delta=100, got delta=$DELTA (before=$BEFORE after=$AFTER)"
fi

# ── Test 7: Query-mode — no no-op exit on duplicate (Discussion #354 AC 4) ────

TEST_NAME="AC-354-1: query-mode hook — first call produces output"

STEPS_QUERY="step_a,step_b"
EVENT_ID_QUERY="test-query-$(date +%s%N | sha256sum | cut -c1-16)"
cleanup_event "$EVENT_ID_QUERY"

# Build a stub query hook (uses --query-mode)
TMP_QUERY_HOOK=$(mktemp "/tmp/test-query-hook-XXXXXX.sh")

cat > "$TMP_QUERY_HOOK" << QUERY_HOOK_SCRIPT
#!/usr/bin/env bash
set -uo pipefail
REPO_ROOT_INNER="$REPO_ROOT"
export HOOK_ROLE="\${HOOK_ROLE:-test-role}"
export HOOK_DISCUSSION="\${HOOK_DISCUSSION:-999}"
export HOOK_PR="\${HOOK_PR:-}"
export HOOK_VERDICT="\${HOOK_VERDICT:-spawn-check}"
export HOOK_CALLER="test-query-hook"
export HOOK_EVENT_DIR="$HOOK_EVENTS_DIR"

source "$HOOK_LIB"

EVENT_ID_ARG="\${1:-}"
INIT_ARGS=()
[[ -n "\$EVENT_ID_ARG" ]] && INIT_ARGS+=(--event-id "\$EVENT_ID_ARG")

hook_event_init "test-query-hook" "step_a,step_b" "\${INIT_ARGS[@]:-}" --query-mode

for step in step_a step_b; do
  if ! hook_event_has_step "\$step"; then
    hook_event_mark_step "\$step"
  fi
done

hook_event_finish
echo '{"result":"ok"}'
QUERY_HOOK_SCRIPT
chmod +x "$TMP_QUERY_HOOK"

# First call
QUERY_OUT1=$(bash "$TMP_QUERY_HOOK" "$EVENT_ID_QUERY" 2>/dev/null)
if echo "$QUERY_OUT1" | grep -q '"result":"ok"'; then
  pass
else
  fail "first call did not produce expected output. Got: $QUERY_OUT1"
fi

TEST_NAME="AC-354-2: query-mode hook — second call (same event_id) also produces output"

# Second call — in query-mode this must NOT be a no-op exit
QUERY_OUT2=$(bash "$TMP_QUERY_HOOK" "$EVENT_ID_QUERY" 2>/dev/null)
if echo "$QUERY_OUT2" | grep -q '"result":"ok"'; then
  pass
else
  fail "second call produced no output (no-op exit fired despite --query-mode). Got: $QUERY_OUT2"
fi

TEST_NAME="AC-354-3: query-mode — marker file appears in done/ after each call"

# Marker from the most recent call should still end up in done/ (observability preserved)
assert_marker_in_done "$EVENT_ID_QUERY"

rm -f "$TMP_QUERY_HOOK"
cleanup_event "$EVENT_ID_QUERY"

# ── Summary ────────────────────────────────────────────────────────────────────

echo ""
echo "=== Results ==="
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [[ "$FAIL" -gt 0 ]]; then
  echo "FAILED" >&2
  exit 1
else
  echo "ALL TESTS PASSED"
  exit 0
fi
