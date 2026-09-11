#!/usr/bin/env bash
# tests/test_post_merge_hook_stats_batch_attempt.sh — D#2524 PR-b, Gate 2.
#
# Executes the ACTUAL stats_metrics python block from
# scripts/post-merge-hook.sh (extracted byte-for-byte from the shipping
# file, never retyped) against a REAL stats.duckdb, with a REAL second OS
# process holding a real DuckDB lock on it — not a mock, not a dry-run
# (D#2149: a preview is not evidence about the guarded path).
#
# Two real-process phases:
#
#   Part A — a single real holder process holds the DB continuously for
#   longer than BOTH mark_batch_attempt's retry budget (2.0s) and
#   record_many's (5.0s). Because the two calls run back-to-back in the
#   same script, a sustained holder that starts before the script does
#   necessarily exhausts the shorter budget (mark_batch_attempt) first —
#   there is no external timing that makes a single continuous holder fail
#   only the second call and not the first; that would need the holder to
#   appear in the sub-millisecond gap between two calls in the same Python
#   process, which cannot be driven from outside without instrumenting the
#   code under test. So Part A proves the REAL, total-loss case: under
#   sustained contention, neither write lands, the hook step still exits 0
#   (item 11), and the failure is surfaced (a WARNING), never silently
#   swallowed.
#
#   Part B — the realistic partial case items 8/9/10 are actually about
#   (a marker written, but the corresponding data batch never lands) is not
#   a lock-contention artifact in production either: PR-a's own measured
#   duty cycle (21.56%, longest contiguous hold 0.7s, D#2524 Implementation
#   Notes) makes it far more likely that a real merge either commits its
#   whole batch or loses it in one shot than that mark_batch_attempt (2.0s
#   budget) survives a hold long enough to also exhaust record_many's 5.0s
#   budget. The scenario the detector exists to catch is a crash or kill
#   between the marker write and the data write — Part B reproduces that
#   directly: a real, uncontended call to the real mark_batch_attempt()
#   against the real DB file from Part A, with no corresponding metric_event
#   rows ever written (exactly what a kill between the two calls leaves
#   behind), then the real CLI detector run against that same real file.
#
# Gate 2 mode (D#2524 item 15): Part A's competing holder opens the DB
# read_only=True. F1 in the frozen Spec measured that a read_only holder
# blocks a read-write opener just as a read-write holder does; this test
# exercises that direction. (The reverse direction — read-write holder
# blocking a read_only opener — is item 5's concern, already covered by
# PR-a's own test suite.)
#
# Run: bash tests/test_post_merge_hook_stats_batch_attempt.sh
# Expects: all assertions pass, exit 0
#
# Note: the wrapper output includes a harmless "by-discussion: command not
# found" line. That comes from a pre-existing backtick (`by-discussion`) in
# a Python *comment* inside the real heredoc — bash performs command
# substitution on backticks even inside an unquoted heredoc, regardless of
# Python syntax. It's a latent quirk of the shipped file (predates this PR,
# inert since it only ever substitutes into a comment), not something this
# test or PR introduces — left alone rather than expanding scope to fix it.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/scripts/post-merge-hook.sh"

PASS=0
FAIL=0
ERRORS=()
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

# ── Extract the real stats_metrics python block (heredoc + RC handling) ─────
# Anchored on literal, unique lines in the shipping file — never a
# hand-retyped copy of the logic.
REGION="$(awk '
  /^  python3 - <<PYEOF$/ { inside = 1 }
  inside                  { print }
  inside && /^  fi$/      { exit }
' "$HOOK")"

if [[ -z "$REGION" ]]; then
  fail "the stats_metrics python block is locatable in the hook"
  echo "Results: $PASS passed, $FAIL failed"
  exit 1
else
  pass "the stats_metrics python block is locatable in the hook"
fi

if grep -qF 'from stats.batch_attempt import mark_batch_attempt' <<<"$REGION"; then
  pass "the extracted block imports mark_batch_attempt"
else
  fail "the extracted block does not import mark_batch_attempt — extraction anchors may be stale"
fi

# ── Real holder process: opens the real DB read_only=True and holds it ─────
WORKDIR="$(mktemp -d)"
STATE_DIR="$WORKDIR/state"
mkdir -p "$STATE_DIR"
DB_FILE="$STATE_DIR/stats.duckdb"

# The DB file must exist before a read_only holder can open it.
python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
import duckdb
duckdb.connect('$DB_FILE').close()
"

HOLD_S=8.5   # exceeds _MARK_RETRY_BUDGET_S (2.0s) PLUS record_many's default
             # retry budget (5.0s) run back-to-back after it (7.0s total),
             # with margin — guarantees BOTH calls exhaust their retry and
             # fail (Part A: total loss under sustained contention).
READY_FILE="$WORKDIR/holder_ready.flag"
cat > "$WORKDIR/holder.py" <<PYEOF
import sys, time
sys.path.insert(0, "$REPO_ROOT")
import duckdb
conn = duckdb.connect("$DB_FILE", read_only=True)
with open("$READY_FILE", "w") as fh:
    fh.write("acquired")
time.sleep($HOLD_S)
conn.close()
PYEOF

python3 "$WORKDIR/holder.py" &
HOLDER_PID=$!

DEADLINE=$((SECONDS + 10))
while [[ ! -f "$READY_FILE" ]]; do
  if ! kill -0 "$HOLDER_PID" 2>/dev/null; then
    fail "holder process exited before acquiring the lock"
    break
  fi
  if [[ $SECONDS -gt $DEADLINE ]]; then
    kill "$HOLDER_PID" 2>/dev/null || true
    fail "holder process never signaled that it acquired the lock"
    break
  fi
  sleep 0.05
done
if [[ -f "$READY_FILE" ]]; then
  pass "real holder process acquired a read_only=True lock on the real DB"
fi

# ── Run the extracted block for real, against the contended DB ─────────────
MARK_LOG="$WORKDIR/marked_steps.log"
: > "$MARK_LOG"

WRAPPER="$WORKDIR/run_block.sh"
{
  echo '#!/usr/bin/env bash'
  echo 'set -uo pipefail'
  # Stub hook_event_mark_step so we can observe, without any hook-event
  # marker-file machinery, whether the step actually got marked complete.
  echo "hook_event_mark_step() { echo \"MARKED:\$1\" >> \"$MARK_LOG\"; }"
  echo "REPO_ROOT=\"$REPO_ROOT\""
  echo "PR=\"99001\""
  echo "DISC_TAG=\"Bug\""
  echo "FIX_CYCLE_COUNT=\"0\""
  echo "COST_USD=\"0\""
  echo "COST_SOURCE=\"none\""
  echo "CONFLICT_SCORE=\"0\""
  echo "PR_CREATED_AT=\"2026-09-11T00:00:00Z\""
  echo "SPEC_READY_TS=\"\""
  echo "REVIEWER_ACCEPT_TS=\"\""
  echo "AUTONOMOUS_TEAM_STATE_DIR=\"$STATE_DIR\""
  echo "export AUTONOMOUS_TEAM_STATE_DIR"
  printf '%s\n' "$REGION"
  echo 'echo "WRAPPER_REACHED_END=1"'
} > "$WRAPPER"

WRAPPER_OUT="$WORKDIR/wrapper_out.txt"
bash "$WRAPPER" > "$WRAPPER_OUT" 2>&1
WRAPPER_RC=$?

echo "--- wrapper output ---"
cat "$WRAPPER_OUT"
echo "--- end wrapper output ---"

# item 11: the step body itself never exits the process — it degrades and
# continues. Assert the exit status directly.
if [[ $WRAPPER_RC -eq 0 ]]; then
  pass "item 11: the stats_metrics step body exits 0 even though the python block failed under contention"
else
  fail "item 11: the stats_metrics step body exited $WRAPPER_RC — it must degrade, not fail the hook"
fi

if grep -qF "WRAPPER_REACHED_END=1" "$WRAPPER_OUT"; then
  pass "the step body ran to completion (no early exit)"
else
  fail "the step body did not run to completion"
fi

if grep -qF "WARNING: batch-attempt marker write failed" "$WRAPPER_OUT" \
  && grep -qF "WARNING: stats_metrics python block exited" "$WRAPPER_OUT"; then
  pass "Part A: both failures are surfaced with a WARNING (not silently swallowed)"
else
  fail "Part A: expected both WARNING lines — the python block may not have actually failed under contention; check HOLD_S sizing"
fi

if grep -qF "MARKED:stats_metrics" "$MARK_LOG"; then
  fail "item 11 contradiction: the step was marked complete despite the python block failing"
else
  pass "item 11: the step is correctly NOT marked complete when the python block fails"
fi

wait "$HOLDER_PID" 2>/dev/null || true

# Part A leaves no attributable state for PR 99001 (neither call landed) —
# confirm the detector correctly reports NOTHING for it, matching the
# Spec's own F4 caveat: a total loss with no marker cannot be told apart
# from "hook never ran", and the detector must not guess.
DETECT_A_OUT="$WORKDIR/detect_a_out.txt"
STATS_DB_PATH="$DB_FILE" PYTHONPATH="$REPO_ROOT" \
  python3 -m backend.stats.batch_attempt --pr 99001 \
  > "$DETECT_A_OUT" 2>&1
DETECT_A_RC=$?
if [[ $DETECT_A_RC -eq 0 ]] && grep -qF '[]' "$DETECT_A_OUT"; then
  pass "Part A: with no marker at all for PR 99001, the detector correctly reports nothing (not a guess)"
else
  fail "Part A: detector should report [] for PR 99001 (no marker survived total loss) — got rc=$DETECT_A_RC: $(cat "$DETECT_A_OUT")"
fi

# ── Part B: real mark_batch_attempt() + real CLI detector, uncontended,
# against the same real DB file — reproduces a crash between the marker
# write and the data write (see header comment for why this is split from
# Part A's contention run).
echo ""
echo "Part B: real marker write, no corresponding data (crash-between-writes shape)"

PYTHONPATH="$REPO_ROOT" STATS_DB_PATH="$DB_FILE" python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from backend.stats.batch_attempt import mark_batch_attempt
from pathlib import Path
mark_batch_attempt('99002', ['time_to_merge_seconds', 'fix_cycle_count'], source='post-merge-hook', db_path=Path('$DB_FILE'))
print('marker written for PR 99002')
"
MARK_B_RC=$?
if [[ $MARK_B_RC -eq 0 ]]; then
  pass "Part B: a real, uncontended mark_batch_attempt() call succeeds against the real DB"
else
  fail "Part B: mark_batch_attempt() failed uncontended — rc=$MARK_B_RC"
fi
# Deliberately no metric_event rows written for PR 99002 — the crash shape.

DETECT_B_OUT="$WORKDIR/detect_b_out.txt"
STATS_DB_PATH="$DB_FILE" PYTHONPATH="$REPO_ROOT" \
  python3 -m backend.stats.batch_attempt --pr 99002 \
  > "$DETECT_B_OUT" 2>&1
DETECT_B_RC=$?

echo "--- detector output (python3 -m backend.stats.batch_attempt --pr 99002) ---"
cat "$DETECT_B_OUT"
echo "--- end detector output ---"

if [[ $DETECT_B_RC -eq 1 ]]; then
  pass "item 10: the detector, run directly (no merge log read), reports the lost batch and exits 1"
else
  fail "item 10: expected the detector to exit 1 (incomplete batch found), got $DETECT_B_RC"
fi

if grep -qF '"pr": "99002"' "$DETECT_B_OUT" && grep -qF '"status": "lost_zero"' "$DETECT_B_OUT"; then
  pass "item 9: the report distinguishes this as an attempted-and-lost (lost_zero) batch, naming the PR"
else
  fail "item 9: detector output did not report pr=99002 as lost_zero — see output above"
fi

rm -rf "$WORKDIR"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
