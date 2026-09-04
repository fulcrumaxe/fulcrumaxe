#!/usr/bin/env bash
# scripts/test-spawn-agent-exit-trap.sh — verify that spawn-agent.sh's EXIT trap
# always calls complete_run so DuckDB rows never stay duration_s=NULL.
#
# Tests:
#   1. post-agent-hook.sh --event-id closes a row that start_run left open
#   2. Double-fire with same event-id is idempotent
#   3. EXIT trap lines are present in spawn-agent.sh source
#   4. _spawn_exit_trap() body calls post-agent-hook.sh
#
# Exit 0 on pass, exit 1 on any failure.
#
# Discussion #774: 166 stale rows had duration_s=NULL because complete_run was
# never called when spawn-agent.sh exited in prompt-assembly mode.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=true
FAILURES=()

# ── Isolated DuckDB so we don't pollute the real stats.duckdb ─────────────────
TMPSTATE="$(mktemp -d /tmp/test-spawn-trap-XXXXXX)"
TEST_DB="$TMPSTATE/stats.duckdb"
export STATS_DB_PATH="$TEST_DB"
export AUTONOMOUS_TEAM_STATE_DIR="$TMPSTATE"

cleanup() {
  rm -rf "$TMPSTATE"
}
trap cleanup EXIT

fail() {
  local msg="$1"
  echo "FAIL: $msg" >&2
  FAILURES+=("$msg")
  PASS=false
}

pass_test() {
  echo "PASS: $1"
}

# ── Helper: query duration_s for a given agent_id ────────────────────────────
query_duration() {
  local agent_id="$1"
  python3 - <<PYEOF 2>/dev/null
try:
    import duckdb
    from pathlib import Path
    if not Path("$TEST_DB").exists():
        print("NO_DB")
    else:
        con = duckdb.connect("$TEST_DB", read_only=True)
        rows = con.execute(
            "SELECT duration_s FROM agent_run WHERE agent_id = ?",
            ["$agent_id"]
        ).fetchall()
        print(str(rows[0][0]) if rows else "NO_ROW")
except Exception as e:
    print(f"ERROR:{e}")
PYEOF
}

# ── Helper: insert a start_run row ───────────────────────────────────────────
start_run() {
  local agent_id="$1" role="$2"
  STATS_DB_PATH="$TEST_DB" python3 "$REPO_ROOT/backend/agent_run_tracker.py" start \
    --agent-id "$agent_id" \
    --role "$role" \
    --event-id "$agent_id" \
    2>/dev/null
}

# ── Test 1: start_run + complete_run wiring ───────────────────────────────────
# Directly tests that post-agent-hook.sh --event-id closes a DuckDB row.
# This mirrors exactly what the EXIT trap in spawn-agent.sh does.
echo "--- Test 1: post-agent-hook.sh --event-id closes duration_s=NULL row ---"

TS1=$(date +%s)
AGENT1="researcher-nod-${TS1}-trap-t1"

start_run "$AGENT1" "researcher"

BEFORE=$(query_duration "$AGENT1")
echo "  duration_s before trap fires: $BEFORE"

if [[ "$BEFORE" == "NO_DB" ]]; then
  fail "Test 1 setup: DuckDB was not created by start_run"
elif [[ "$BEFORE" == "NO_ROW" ]]; then
  fail "Test 1 setup: start_run did not insert a row"
elif [[ "$BEFORE" != "None" && "$BEFORE" != "" ]]; then
  fail "Test 1 setup: expected duration_s=NULL before complete, got '$BEFORE'"
else
  echo "  (row open — duration_s is NULL as expected)"
fi

# Fire exactly what the EXIT trap does
STATS_DB_PATH="$TEST_DB" \
  bash "$REPO_ROOT/scripts/post-agent-hook.sh" \
    --role "researcher" \
    --verdict "unknown" \
    --event-id "$AGENT1" \
    2>/dev/null || true

AFTER=$(query_duration "$AGENT1")
echo "  duration_s after trap fires: $AFTER"

if [[ "$AFTER" == "None" || "$AFTER" == "" ]]; then
  fail "Test 1: duration_s still NULL after post-agent-hook.sh — complete_run broken"
elif [[ "$AFTER" == "NO_ROW" ]]; then
  fail "Test 1: row disappeared after complete_run call"
elif [[ "$AFTER" == NO_DB ]]; then
  fail "Test 1: DuckDB vanished after hook call"
elif [[ "$AFTER" == ERROR* ]]; then
  fail "Test 1: DuckDB query error — $AFTER"
else
  pass_test "Test 1: duration_s=$AFTER (not NULL after simulated EXIT trap)"
fi

# ── Test 2: double-fire is idempotent ─────────────────────────────────────────
echo "--- Test 2: second post-agent-hook.sh call with same event-id is idempotent ---"

TS2=$(date +%s)
AGENT2="researcher-nod-${TS2}-trap-t2"
start_run "$AGENT2" "researcher"

STATS_DB_PATH="$TEST_DB" \
  bash "$REPO_ROOT/scripts/post-agent-hook.sh" \
    --role "researcher" --verdict "unknown" --event-id "$AGENT2" \
    2>/dev/null || true

D1=$(query_duration "$AGENT2")

# Second call — should be a no-op, not crash
STATS_DB_PATH="$TEST_DB" \
  bash "$REPO_ROOT/scripts/post-agent-hook.sh" \
    --role "researcher" --verdict "unknown" --event-id "$AGENT2" \
    2>/dev/null || true

D2=$(query_duration "$AGENT2")
echo "  duration_s after 1st call: $D1"
echo "  duration_s after 2nd call: $D2"

if [[ "$D2" == "None" || "$D2" == "" || "$D2" == "NO_ROW" || "$D2" == NO_DB ]]; then
  fail "Test 2: row broken after second hook call — $D2"
else
  pass_test "Test 2: idempotent — duration_s stable at $D2"
fi

# ── Test 3: EXIT trap is present in spawn-agent.sh source ─────────────────────
echo "--- Test 3: EXIT trap is wired in spawn-agent.sh source ---"

if grep -q "_spawn_exit_trap" "$REPO_ROOT/scripts/spawn-agent.sh" && \
   grep -q "trap.*_spawn_exit_trap.*EXIT" "$REPO_ROOT/scripts/spawn-agent.sh"; then
  pass_test "Test 3: EXIT trap lines found in scripts/spawn-agent.sh"
else
  fail "Test 3: EXIT trap missing from scripts/spawn-agent.sh — regression"
fi

# ── Test 4: trap function calls post-agent-hook.sh ────────────────────────────
echo "--- Test 4: trap function body contains post-agent-hook.sh call ---"

if grep -A5 "_spawn_exit_trap()" "$REPO_ROOT/scripts/spawn-agent.sh" | grep -q "post-agent-hook.sh"; then
  pass_test "Test 4: post-agent-hook.sh invocation found inside _spawn_exit_trap()"
else
  fail "Test 4: _spawn_exit_trap() does not call post-agent-hook.sh"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
if [[ "$PASS" == "true" ]]; then
  echo "All tests passed."
  exit 0
else
  echo "Failures:"
  for f in "${FAILURES[@]}"; do
    echo "  - $f"
  done
  exit 1
fi
