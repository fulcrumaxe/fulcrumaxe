#!/usr/bin/env bash
# tests/test_append_loop_metrics.sh — unit tests for scripts/append-loop-metrics.sh
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and temp dirs — no real GitHub API calls, no loop triggers.
#
# Usage:
#   bash tests/test_append_loop_metrics.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APPEND_SCRIPT="$REPO_ROOT/scripts/append-loop-metrics.sh"

PASS=0
FAIL=0
ERRORS=()

# ── Helpers ────────────────────────────────────────────────────────────────

pass() { echo "  PASS: $1"; ((PASS++)); }
fail() { echo "  FAIL: $1"; ((FAIL++)); ERRORS+=("$1"); }

# Create a fresh temp dir and export METRICS_FILE pointing into it.
setup() {
  TEST_DIR=$(mktemp -d)
  mkdir -p "$TEST_DIR/.autonomous-team"
  TEST_METRICS="$TEST_DIR/.autonomous-team/loop-metrics.jsonl"
}

teardown() {
  rm -rf "${TEST_DIR:-}"
}

# ── Test 1: dry-run prints valid JSON and does not write file ──────────────

echo ""
echo "Test 1: --dry-run prints JSON row, file not created"
setup

OUTPUT=$(METRICS_FILE="$TEST_METRICS" bash "$APPEND_SCRIPT" \
  --iter-start-iso   "2026-05-11T10:00:00Z" \
  --iter-end-iso     "2026-05-11T10:05:00Z" \
  --duration-seconds 300 \
  --agents-spawned   2 \
  --prs-merged       1 \
  --event-count      15 \
  --queue-depth      0 \
  --discussion-count 3 \
  --pr-count         2 \
  --needs-review     1 \
  --needs-merge      1 \
  --needs-fix        0 \
  --dry-run true 2>/dev/null)

# stdout should be valid JSON
if echo "$OUTPUT" | jq empty 2>/dev/null; then
  pass "dry-run output is valid JSON"
else
  fail "dry-run output is not valid JSON: $OUTPUT"
fi

# --dry-run must NOT write to the metrics file
if [[ -f "$TEST_METRICS" ]]; then
  fail "dry-run created metrics file (should not)"
else
  pass "dry-run did not create metrics file"
fi

teardown

# ── Test 2: appends exactly one line with correct fields ──────────────────

echo ""
echo "Test 2: appends exactly one valid JSON line with required fields"
setup

METRICS_FILE="$TEST_METRICS" bash "$APPEND_SCRIPT" \
  --iter-start-iso   "2026-05-11T10:00:00Z" \
  --iter-end-iso     "2026-05-11T10:05:00Z" \
  --duration-seconds 300 \
  --agents-spawned   3 \
  --prs-merged       2 \
  --event-count      20 \
  --queue-depth      1 \
  --discussion-count 4 \
  --pr-count         3 \
  --needs-review     2 \
  --needs-merge      1 \
  --needs-fix        0 \
  2>/dev/null

if [[ ! -f "$TEST_METRICS" ]]; then
  fail "metrics file not created"
else
  LINE_COUNT=$(wc -l < "$TEST_METRICS" 2>/dev/null || echo 0)
  if [[ "$LINE_COUNT" -eq 1 ]]; then
    pass "exactly one line written"
  else
    fail "expected 1 line, got $LINE_COUNT"
  fi

  ROW=$(tail -1 "$TEST_METRICS")

  # Check all required fields
  REQUIRED_FIELDS=(ts duration_s open_prs needs_review needs_merge needs_fix
    event_count discussion_count queue_depth agents_spawned prs_merged
    budget cost quality)

  ALL_FIELDS_OK=true
  for field in "${REQUIRED_FIELDS[@]}"; do
    if ! echo "$ROW" | jq -e "has(\"$field\")" >/dev/null 2>&1; then
      fail "missing field: $field"
      ALL_FIELDS_OK=false
    fi
  done
  [[ "$ALL_FIELDS_OK" == "true" ]] && pass "all required fields present"

  # Check specific values
  TS=$(echo "$ROW" | jq -r '.ts' 2>/dev/null)
  if [[ "$TS" == "2026-05-11T10:05:00Z" ]]; then
    pass "ts matches iter-end-iso"
  else
    fail "ts mismatch: got $TS"
  fi

  DUR=$(echo "$ROW" | jq '.duration_s' 2>/dev/null)
  if [[ "$DUR" == "300" ]]; then
    pass "duration_s correct"
  else
    fail "duration_s wrong: got $DUR"
  fi

  AS=$(echo "$ROW" | jq '.agents_spawned' 2>/dev/null)
  if [[ "$AS" == "3" ]]; then
    pass "agents_spawned correct"
  else
    fail "agents_spawned wrong: got $AS"
  fi

  PM=$(echo "$ROW" | jq '.prs_merged' 2>/dev/null)
  if [[ "$PM" == "2" ]]; then
    pass "prs_merged correct"
  else
    fail "prs_merged wrong: got $PM"
  fi
fi

teardown

# ── Test 3: appending twice writes two lines ──────────────────────────────

echo ""
echo "Test 3: appending twice writes two lines"
setup

for i in 1 2; do
  METRICS_FILE="$TEST_METRICS" bash "$APPEND_SCRIPT" \
    --iter-start-iso   "2026-05-11T10:0${i}:00Z" \
    --iter-end-iso     "2026-05-11T10:0${i}:30Z" \
    --duration-seconds 30 \
    --agents-spawned   1 \
    --prs-merged       0 \
    --event-count      5 \
    --queue-depth      0 \
    --discussion-count 2 \
    --pr-count         1 \
    --needs-review     0 \
    --needs-merge      0 \
    --needs-fix        0 \
    2>/dev/null
done

LINE_COUNT=$(wc -l < "$TEST_METRICS" 2>/dev/null || echo 0)
if [[ "$LINE_COUNT" -eq 2 ]]; then
  pass "two appends produce two lines"
else
  fail "expected 2 lines, got $LINE_COUNT"
fi

teardown

# ── Test 4: defaults work (no required args, dry-run) ────────────────────

echo ""
echo "Test 4: script runs with no required args (all defaults, dry-run)"
setup

OUTPUT=$(METRICS_FILE="$TEST_METRICS" bash "$APPEND_SCRIPT" --dry-run true 2>/dev/null)

if echo "$OUTPUT" | jq -e '.ts' >/dev/null 2>&1; then
  pass "no-arg dry-run produces row with .ts"
else
  fail "no-arg dry-run failed or missing .ts: ${OUTPUT:-<empty>}"
fi

teardown

# ── Test 5: team-lead-iteration.sh refactor — calls append-loop-metrics.sh ─

echo ""
echo "Test 5: team-lead-iteration.sh references append-loop-metrics.sh"

if grep -q 'append-loop-metrics.sh' "$REPO_ROOT/scripts/team-lead-iteration.sh"; then
  pass "team-lead-iteration.sh calls append-loop-metrics.sh"
else
  fail "team-lead-iteration.sh does not call append-loop-metrics.sh"
fi

# Old inline jq block should no longer be present
if grep -q '"ts":\$ts,"duration_s":\$dur' "$REPO_ROOT/scripts/team-lead-iteration.sh"; then
  fail "old inline jq metrics block still present in team-lead-iteration.sh"
else
  pass "old inline jq metrics block removed from team-lead-iteration.sh"
fi

# ── Test 6: AC-8 — valid inputs produce a well-formed single-line JSON row ─

echo ""
echo "Test 6 (AC-8): valid inputs produce a single-line valid JSON row"
setup

EXIT_CODE=0
METRICS_FILE="$TEST_METRICS" bash "$APPEND_SCRIPT" \
  --iter-start-iso   "2026-05-12T09:00:00Z" \
  --iter-end-iso     "2026-05-12T09:05:00Z" \
  --duration-seconds 300 \
  --agents-spawned   1 \
  --prs-merged       0 \
  --event-count      10 \
  --queue-depth      0 \
  --discussion-count 2 \
  --pr-count         1 \
  --needs-review     0 \
  --needs-merge      0 \
  --needs-fix        0 \
  2>/dev/null || EXIT_CODE=$?

if [[ "$EXIT_CODE" -eq 0 ]]; then
  pass "valid inputs exit 0 (not exit 2)"
else
  fail "valid inputs exited $EXIT_CODE (expected 0)"
fi

if [[ -f "$TEST_METRICS" ]]; then
  LINE=$(tail -1 "$TEST_METRICS")
  if printf '%s' "$LINE" | jq -e 'has("timestamp")' >/dev/null 2>&1; then
    pass "appended line is valid JSON with timestamp field"
  else
    fail "appended line is not valid JSON or missing timestamp: ${LINE:0:80}"
  fi
  FILE_LINES=$(wc -l < "$TEST_METRICS" 2>/dev/null || echo 0)
  if [[ "$FILE_LINES" -eq 1 ]]; then
    pass "appended file has exactly 1 line (single-line JSON, not multi-line)"
  else
    fail "appended file has $FILE_LINES lines (expected 1)"
  fi
else
  fail "metrics file not created"
fi

teardown

# ── Test 7: AC-8 — dry-run also produces single-line valid JSON ───────────

echo ""
echo "Test 7 (AC-8): dry-run output is a single-line valid JSON row"
setup

DRY_OUTPUT=$(METRICS_FILE="$TEST_METRICS" bash "$APPEND_SCRIPT" \
  --iter-start-iso   "2026-05-12T10:00:00Z" \
  --iter-end-iso     "2026-05-12T10:05:00Z" \
  --duration-seconds 300 \
  --agents-spawned   2 \
  --prs-merged       1 \
  --event-count      20 \
  --queue-depth      0 \
  --discussion-count 3 \
  --pr-count         2 \
  --needs-review     1 \
  --needs-merge      1 \
  --needs-fix        0 \
  --dry-run true 2>/dev/null)

if printf '%s' "$DRY_OUTPUT" | jq -e . >/dev/null 2>&1; then
  pass "dry-run output is valid JSON"
else
  fail "dry-run output is not valid JSON: ${DRY_OUTPUT:0:80}"
fi

DRY_LINES=$(printf '%s' "$DRY_OUTPUT" | wc -l)
if [[ "$DRY_LINES" -le 1 ]]; then
  pass "dry-run output is single-line JSON"
else
  fail "dry-run output has $DRY_LINES lines (expected 1)"
fi

teardown

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failed tests:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
fi

[[ "$FAIL" -eq 0 ]]
