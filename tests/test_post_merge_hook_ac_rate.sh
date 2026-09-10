#!/usr/bin/env bash
# tests/test_post_merge_hook_ac_rate.sh — D#2476
#
# acceptance_criteria_pass_rate had 336 recorded points, all "-1" sentinels,
# zero real measurements, since 2026-07-24. Root cause: the heading regex it
# looked for (`### Acceptance Criteria`) matched 0 of 51 real Spec bodies
# (measured on the Discussion plane; 41 of those 51 use `## Spec
# (Acceptance)` instead). Fixing only the heading would NOT have fixed the
# metric — the scorer behind it counted a criterion as satisfied whenever
# any word longer than 3 characters from it appeared anywhere in the PR
# body or its comments, and the PR body is written by the same executor
# working from that same Spec. That is a lexical-overlap check between two
# documents with a common author, not an acceptance measurement. Repointing
# the regex alone would have converted 336 honest "-1" sentinels into a
# stream of values pinned near 1.0 — a metric that looks alive and measures
# nothing, strictly worse than the sentinel it replaced.
#
# The denominator that could actually distinguish a real pass from a real
# fail — the acceptance-tester's own verdict — is not available to
# post-merge-hook.sh at merge time (acceptance-tester is spawned on demand;
# measured 2026-09-10 on the code plane, 1 of the last 100 merged PRs carried
# an acceptance-passed/-failed label). Building that wiring is a materially
# larger change than this PR (see Implementation Notes on D#2476), so the
# writer is retired here, not repaired.
#
# This suite proves two things:
#   1. Static — the retired heading regex, the retired AC_PASS_RATE variable,
#      and the retired row literal are all gone from the shipped script.
#   2. Runtime, binding — the ACTUAL stats_metrics record_many block is
#      extracted verbatim from the shipped script (not retyped) and executed
#      against a scratch DuckDB with merge-shaped inputs:
#        a. no acceptance_criteria_pass_rate row lands, while the other 7
#           known metrics DO land — proving the block genuinely ran rather
#           than silently no-opping;
#        b. mutating the extracted block to reintroduce the row (simulating
#           a future regression that brings the writer back) makes this
#           check fail — proving item 2a is not vacuously true.
#
# AUTONOMOUS_TEAM_STATE_DIR and STATS_DB_PATH are both pointed at a scratch
# dir for the whole run. Nothing here writes the production stats.duckdb.
#
# Run: bash tests/test_post_merge_hook_ac_rate.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/scripts/post-merge-hook.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

echo "acceptance_criteria_pass_rate: retired, not repaired (D#2476)"
echo

# ---------------------------------------------------------------------------
# 1. Static — the retired pieces are actually gone from the shipped script.
# ---------------------------------------------------------------------------

echo "Static checks against $HOOK"

if grep -qE '"metric":\s*"acceptance_criteria_pass_rate"' "$HOOK"; then
  fail "no row literal for acceptance_criteria_pass_rate remains in the hook"
else
  pass "no row literal for acceptance_criteria_pass_rate remains in the hook"
fi

# Checks for the regex LITERAL (as Python source, quoted with r'...'), not the
# bare heading text — the bare text legitimately appears in this file's own
# retirement comment above, explaining what used to be searched for.
if grep -qF "r'### Acceptance Criteria" "$HOOK"; then
  fail "the retired heading regex (r'### Acceptance Criteria') is gone from the hook"
else
  pass "the retired heading regex (r'### Acceptance Criteria') is gone from the hook"
fi

if grep -q 'AC_PASS_RATE' "$HOOK"; then
  fail "the retired AC_PASS_RATE bash variable is gone from the hook"
else
  pass "the retired AC_PASS_RATE bash variable is gone from the hook"
fi

echo

# ---------------------------------------------------------------------------
# 2. Runtime — extract the real record_many block and execute it for real.
# ---------------------------------------------------------------------------
#
# The block is delimited by the ONE unquoted `python3 - <<PYEOF` heredoc in
# the file (the AC-scoring heredoc that used to sit above it used a QUOTED
# delimiter, <<'PYEOF', and is gone). Extracting rather than retyping means
# this test exercises the exact code the hook ships, not a paraphrase of it.

STATE_DIR="$(mktemp -d)"
BLOCK_FILE="$(mktemp)"
STATS_DB="$STATE_DIR/stats.duckdb"

cleanup() {
  rm -rf "$STATE_DIR"
  rm -f "$BLOCK_FILE" "$BLOCK_FILE.mutated"
}
trap cleanup EXIT

sed -n '/^  python3 - <<PYEOF$/,/^PYEOF$/p' "$HOOK" > "$BLOCK_FILE"

if [[ -s "$BLOCK_FILE" ]]; then
  pass "extracted the stats_metrics record_many block from the hook ($(wc -l < "$BLOCK_FILE" | tr -d ' ') lines)"
else
  fail "extracted the stats_metrics record_many block from the hook"
fi

run_block() {
  # $1 = block file, $2 = stats db path
  PR="9001" \
  DISC_TAG="Feature" \
  FIX_CYCLE_COUNT="1" \
  COST_USD="0.01" \
  COST_SOURCE="agent_run" \
  CONFLICT_SCORE="0" \
  PR_CREATED_AT="2026-01-01T00:00:00Z" \
  SPEC_READY_TS="" \
  REVIEWER_ACCEPT_TS="" \
  REPO_ROOT="$REPO_ROOT" \
  AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR" \
  STATS_DB_PATH="$2" \
    bash "$1"
}

db_metric_names() {
  # $1 = stats db path
  python3 - "$1" <<'PYEOF'
import sys
import duckdb

conn = duckdb.connect(sys.argv[1], read_only=True)
try:
    rows = conn.execute("SELECT DISTINCT metric FROM metric_event").fetchall()
finally:
    conn.close()
for (m,) in rows:
    print(m)
PYEOF
}

RUN_OUT="$(run_block "$BLOCK_FILE" "$STATS_DB" 2>&1)"
RUN_RC=$?

if [[ $RUN_RC -eq 0 ]]; then
  pass "the extracted block ran successfully against a scratch DuckDB (exit 0)"
else
  fail "the extracted block ran successfully against a scratch DuckDB (exit $RUN_RC): $RUN_OUT"
fi

NAMES="$(db_metric_names "$STATS_DB" 2>&1 || true)"

if echo "$NAMES" | grep -qx "acceptance_criteria_pass_rate"; then
  fail "no acceptance_criteria_pass_rate row was written by a real merge-shaped invocation"
else
  pass "no acceptance_criteria_pass_rate row was written by a real merge-shaped invocation"
fi

# The other 7 known metrics must still land — proves 2a isn't vacuous (e.g.
# the block silently failing, or writing to the wrong DB, would ALSO show 0
# rows for the retired metric without proving anything).
EXPECTED_OTHER=(
  time_to_merge_seconds
  fix_cycle_count
  pr_file_conflict_score
  spec_to_first_pr_latency_seconds
  reviewer_acceptance_latency_seconds
  fix_rounds_per_pr
  cost_per_merged_pr_usd
)
MISSING=()
for m in "${EXPECTED_OTHER[@]}"; do
  if ! echo "$NAMES" | grep -qx "$m"; then
    MISSING+=("$m")
  fi
done
if [[ ${#MISSING[@]} -eq 0 ]]; then
  pass "all 7 surviving metrics were written by the same invocation (proves the block genuinely ran)"
else
  fail "all 7 surviving metrics were written by the same invocation — missing: ${MISSING[*]}"
fi

echo

# ---------------------------------------------------------------------------
# 2b. Mutation check — a reintroduced row must make this test fail.
# ---------------------------------------------------------------------------
#
# Simulates a future regression that brings the writer back (e.g. a careless
# revert). Injects a "metric": "acceptance_criteria_pass_rate" row into the
# extracted rows = [ ... ] literal and re-runs — this run MUST show the row,
# proving check 2 above is sensitive rather than trivially true (e.g. because
# the DB path was wrong, or record_many() silently swallowed everything).

sed 's/{"metric": "time_to_merge_seconds",              "value": elapsed,           "unit": "seconds", "tags": tags, "source": "post-merge-hook"},/&\n    {"metric": "acceptance_criteria_pass_rate", "value": 1.0, "unit": "ratio", "tags": tags, "source": "post-merge-hook"},/' \
  "$BLOCK_FILE" > "$BLOCK_FILE.mutated"

if diff -q "$BLOCK_FILE" "$BLOCK_FILE.mutated" > /dev/null 2>&1; then
  fail "mutation actually changed the extracted block (sed target line matched)"
else
  pass "mutation actually changed the extracted block (sed target line matched)"
fi

MUT_DB="$STATE_DIR/mutated.duckdb"
MUT_OUT="$(run_block "$BLOCK_FILE.mutated" "$MUT_DB" 2>&1)"
MUT_RC=$?

if [[ $MUT_RC -eq 0 ]]; then
  MUT_NAMES="$(db_metric_names "$MUT_DB" 2>&1 || true)"
  if echo "$MUT_NAMES" | grep -qx "acceptance_criteria_pass_rate"; then
    pass "RED (expected): a reintroduced writer is caught — the row appears when the code regresses"
  else
    fail "RED (expected) did not reproduce: mutated block still wrote no acceptance_criteria_pass_rate row"
  fi
else
  fail "mutated block failed to run at all (exit $MUT_RC), so the mutation check is inconclusive: $MUT_OUT"
fi

echo
echo "acceptance_criteria_pass_rate: $PASS passed, $FAIL failed (host: $(uname -n 2>/dev/null || echo unknown), scope: this file only)"

if [[ $FAIL -gt 0 ]]; then
  echo
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi

exit 0
