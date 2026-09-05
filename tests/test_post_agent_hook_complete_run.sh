#!/usr/bin/env bash
# tests/test_post_agent_hook_complete_run.sh — verify post-agent-hook.sh updates
# the agent_run row via complete_run (Discussion #635 PR-c).
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# All tests use a temporary DuckDB path and stub out GitHub API calls.
#
# What is tested:
#   1. post-agent-hook.sh calls complete_run — end_ts and duration_s are populated.
#   2. verdict is stored correctly in the updated row.
#   3. token counts are stored correctly.
#   4. post-agent-hook.sh exits 0 even when DuckDB is unwritable.
#   5. start_run → complete_run round-trip: a row written by start_run is fully
#      completed by complete_run (end_ts non-NULL, duration_s > 0).
#
# Usage:
#   bash tests/test_post_agent_hook_complete_run.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK_SCRIPT="$REPO_ROOT/scripts/post-agent-hook.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

# ── Setup ─────────────────────────────────────────────────────────────────────

TEST_DIR=$(mktemp -d)
STATS_DB="$TEST_DIR/stats.duckdb"
export STATS_DB_PATH="$STATS_DB"

# Unique event_id prefix for this test run (avoids collisions across test runs)
TS=$(date +%s)

# Stub directory to intercept hook helper calls
STUBS_DIR="$TEST_DIR/stubs"
mkdir -p "$STUBS_DIR"

# Stub scripts that post-agent-hook.sh calls internally (non-fatal helpers)
for stub in rotate-team-log.sh agent-feed-append.sh record-agent-result.sh \
            scan-orphan-worktrees.sh reap-worktrees.sh; do
  cat > "$STUBS_DIR/$stub" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "$STUBS_DIR/$stub"
done

# Stub Python helpers invoked via python3 -m / direct module calls.
# We can't replace python3 globally, but we can set STATS_DB_PATH so the real
# tracker writes to our temp DB.  All other python3 calls in the hook are
# non-fatal and swallowed via 2>/dev/null || true.

# Override PATH to find our stubs before real scripts
export PATH="$STUBS_DIR:$PATH"

# post-agent-hook.sh looks for sibling scripts via SCRIPT_DIR (absolute at
# runtime).  We need to intercept rotate-team-log.sh and record-agent-result.sh
# which live in the same directory.  Strategy: copy the hook into our stubs dir
# (so SCRIPT_DIR resolves there), then patch REPO_ROOT back to the real root.

HOOK_COPY="$STUBS_DIR/post-agent-hook.sh"
cp "$HOOK_SCRIPT" "$HOOK_COPY"
chmod +x "$HOOK_COPY"

# Also copy lib/ helpers that the hook sources
mkdir -p "$STUBS_DIR/lib"
cp -r "$REPO_ROOT/scripts/lib/"* "$STUBS_DIR/lib/"

# Patch REPO_ROOT in the copy to point at the real repo so backend/ is found
sed -i \
  's|REPO_ROOT="\$(cd "\$SCRIPT_DIR/.." && pwd)"|REPO_ROOT="${REPO_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." \&\& pwd)}"|' \
  "$HOOK_COPY" 2>/dev/null || true

# Helper: run the hook copy with common args
run_hook() {
  local event_id="$1"
  local verdict="${2:-done}"
  local role="${3:-executor}"
  local disc="${4:-635}"
  local in_tok="${5:-1000}"
  local out_tok="${6:-500}"

  REPO_ROOT_OVERRIDE="$REPO_ROOT" \
  STATS_DB_PATH="$STATS_DB" \
  AUTONOMOUS_TEAM_STATE_DIR="$TEST_DIR/state" \
    bash "$HOOK_COPY" \
      --role "$role" \
      --discussion "$disc" \
      --verdict "$verdict" \
      --input-tokens "$in_tok" \
      --output-tokens "$out_tok" \
      --event-id "$event_id" \
      2>/dev/null
}

# Query helpers
duckdb_field() {
  local field="$1"
  local condition="${2:-1=1}"
  STATS_DB_PATH="$STATS_DB" python3 - <<PYEOF 2>/dev/null || echo "NULL"
import os
try:
    import duckdb
    db = os.environ.get("STATS_DB_PATH", "$STATS_DB")
    conn = duckdb.connect(db)
    row = conn.execute("SELECT $field FROM agent_run WHERE $condition LIMIT 1").fetchone()
    val = row[0] if row else None
    print(val if val is not None else "NULL")
    conn.close()
except Exception as e:
    print("NULL")
PYEOF
}

duckdb_count() {
  local condition="${1:-1=1}"
  STATS_DB_PATH="$STATS_DB" python3 - <<PYEOF 2>/dev/null || echo "0"
import os
try:
    import duckdb
    db = os.environ.get("STATS_DB_PATH", "$STATS_DB")
    conn = duckdb.connect(db)
    row = conn.execute("SELECT COUNT(*) FROM agent_run WHERE $condition").fetchone()
    print(row[0] if row else 0)
    conn.close()
except Exception:
    print(0)
PYEOF
}

# Insert a start_run row directly (simulating spawn-agent.sh PR-b)
insert_start_run() {
  local event_id="$1"
  local role="${2:-executor}"
  local disc="${3:-635}"

  STATS_DB_PATH="$STATS_DB" python3 - "$event_id" "$role" "$disc" <<'PYEOF' 2>/dev/null
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(os.environ.get("REPO_ROOT_OVERRIDE", ".")).resolve()))
from backend.agent_run_tracker import start_run
start_run(agent_id=sys.argv[1], role=sys.argv[2], discussion=int(sys.argv[3]))
PYEOF
}

export REPO_ROOT_OVERRIDE="$REPO_ROOT"

# ── Test 1: complete_run updates end_ts and duration_s ───────────────────────

echo ""
echo "Test 1: complete_run populates end_ts and duration_s"

EID="hook-test-1-$TS"
insert_start_run "$EID" executor 635
sleep 0.1
run_hook "$EID" done executor 635 1000 500
RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "hook exits 0"
else
  fail "hook exit code" "expected 0, got $RC"
fi

END_TS=$(duckdb_field "end_ts" "agent_id='$EID'")
if [[ "$END_TS" != "NULL" && "$END_TS" != "None" && -n "$END_TS" ]]; then
  pass "end_ts is non-null after hook ('${END_TS:0:30}...')"
else
  fail "end_ts" "expected non-null, got '$END_TS'"
fi

DUR=$(duckdb_field "duration_s" "agent_id='$EID'")
DUR_IS_POSITIVE=$(python3 -c "
v='$DUR'
try:
    f=float(v)
    print('yes' if f > 0 else 'no')
except:
    print('no')
" 2>/dev/null)
if [[ "$DUR_IS_POSITIVE" == "yes" ]]; then
  pass "duration_s > 0 (got $DUR)"
else
  fail "duration_s" "expected >0, got '$DUR'"
fi

# ── Test 2: verdict stored correctly ─────────────────────────────────────────

echo ""
echo "Test 2: verdict stored in agent_run row"

EID2="hook-test-2-$TS"
insert_start_run "$EID2" code-reviewer 635

run_hook "$EID2" pass code-reviewer 635 2000 300
VERDICT_STORED=$(duckdb_field "verdict" "agent_id='$EID2'")
if [[ "$VERDICT_STORED" == "pass" ]]; then
  pass "verdict stored as 'pass'"
else
  fail "verdict field" "expected 'pass', got '$VERDICT_STORED'"
fi

# ── Test 3: token counts stored correctly ────────────────────────────────────

echo ""
echo "Test 3: input_tok and output_tok stored correctly"

EID3="hook-test-3-$TS"
insert_start_run "$EID3" executor 635

run_hook "$EID3" done executor 635 62000 8400

IN_TOK=$(duckdb_field "input_tok" "agent_id='$EID3'")
OUT_TOK=$(duckdb_field "output_tok" "agent_id='$EID3'")

if [[ "$IN_TOK" == "62000" ]]; then
  pass "input_tok stored as 62000"
else
  fail "input_tok" "expected 62000, got '$IN_TOK'"
fi

if [[ "$OUT_TOK" == "8400" ]]; then
  pass "output_tok stored as 8400"
else
  fail "output_tok" "expected 8400, got '$OUT_TOK'"
fi

# ── Test 4: hook exits 0 even when DuckDB is unwritable ──────────────────────

echo ""
echo "Test 4: hook exits 0 even when DuckDB directory is unwritable"

LOCKED_DIR="$TEST_DIR/locked"
mkdir -p "$LOCKED_DIR"
chmod 000 "$LOCKED_DIR"
LOCKED_DB="$LOCKED_DIR/stats.duckdb"

EID4="hook-test-4-$TS"
REPO_ROOT_OVERRIDE="$REPO_ROOT" \
STATS_DB_PATH="$LOCKED_DB" \
AUTONOMOUS_TEAM_STATE_DIR="$TEST_DIR/state" \
  bash "$HOOK_COPY" \
    --role executor \
    --discussion 635 \
    --verdict done \
    --input-tokens 100 \
    --output-tokens 50 \
    --event-id "$EID4" \
    2>/dev/null
RC_LOCKED=$?
chmod 755 "$LOCKED_DIR"

if [[ "$RC_LOCKED" -eq 0 ]]; then
  pass "hook exits 0 with unwritable DuckDB"
else
  fail "hook with unwritable DuckDB" "expected exit 0, got $RC_LOCKED"
fi

# ── Test 5: round-trip — start_run then complete_run ─────────────────────────

echo ""
echo "Test 5: start_run / complete_run round-trip (end_ts non-NULL, duration > 0)"

EID5="hook-test-5-$TS"
# Insert start via the Python API directly
STATS_DB_PATH="$STATS_DB" REPO_ROOT_OVERRIDE="$REPO_ROOT" python3 - "$EID5" <<'PYEOF' 2>/dev/null
import sys, os, time
from pathlib import Path
sys.path.insert(0, str(Path(os.environ.get("REPO_ROOT_OVERRIDE", ".")).resolve()))
from backend.agent_run_tracker import start_run
start_run(agent_id=sys.argv[1], role="executor", discussion=635, event_id=sys.argv[1])
time.sleep(0.1)
PYEOF

# Fire the hook to call complete_run
run_hook "$EID5" done executor 635 5000 1200
RC5=$?

if [[ "$RC5" -eq 0 ]]; then
  pass "round-trip hook exits 0"
else
  fail "round-trip hook exit code" "expected 0, got $RC5"
fi

END5=$(duckdb_field "end_ts" "agent_id='$EID5'")
DUR5=$(duckdb_field "duration_s" "agent_id='$EID5'")

if [[ "$END5" != "NULL" && "$END5" != "None" && -n "$END5" ]]; then
  pass "round-trip end_ts non-NULL"
else
  fail "round-trip end_ts" "expected non-NULL, got '$END5'"
fi

DUR5_POS=$(python3 -c "
v='$DUR5'
try:
    print('yes' if float(v) > 0 else 'no')
except:
    print('no')
" 2>/dev/null)
if [[ "$DUR5_POS" == "yes" ]]; then
  pass "round-trip duration_s > 0 (got $DUR5)"
else
  fail "round-trip duration_s" "expected >0, got '$DUR5'"
fi

# ── Teardown ──────────────────────────────────────────────────────────────────

rm -rf "$TEST_DIR"

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
exit 0
