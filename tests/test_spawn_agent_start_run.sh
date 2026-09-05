#!/usr/bin/env bash
# tests/test_spawn_agent_start_run.sh — verify spawn-agent.sh emits start_run rows.
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and a temp DuckDB path — no real GitHub API calls.
#
# What is tested:
#   1. spawn-agent.sh calls agent_run_tracker.py start after pre-spawn-check passes,
#      producing a row with end_ts=NULL and a non-null start_ts in DuckDB.
#   2. spawn-agent.sh exit code remains 0 on success.
#   3. spawn-agent.sh exit code remains 1 when pre-spawn-check signals blocked
#      (via allowed=false in JSON).
#   4. spawn-agent.sh succeeds (exit 0) even when DuckDB is unwritable.
#   5. role and event_id are stored correctly in the agent_run row.
#   SR-A..SR-I (D#2089): a rebuild for the same (role, discussion) pair
#      supersedes the prior open row instead of stacking a second cap slot;
#      different discussions/roles still each count; a closed row is never
#      re-closed; a discussion-less spawn or --no-register supersedes nothing;
#      the cap still blocks four genuinely distinct executors; the block
#      message names every counted row plus the recovery command; a
#      supersede is announced on stderr.
#
# Usage:
#   bash tests/test_spawn_agent_start_run.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_SCRIPT="$REPO_ROOT/scripts/spawn-agent.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

# ── Setup ─────────────────────────────────────────────────────────────────────

TEST_DIR=$(mktemp -d)
STATS_DB="$TEST_DIR/stats.duckdb"
export STATS_DB_PATH="$STATS_DB"

# We need to intercept the absolute-path call to pre-spawn-check.sh inside
# spawn-agent.sh.  The script calls "$SCRIPT_DIR/pre-spawn-check.sh" — so we
# write a replacement next to the real spawn-agent.sh and use a temp copy of
# spawn-agent.sh that points to our stub directory.
#
# Strategy: create a temp scripts/ dir that contains:
#   - A copy of spawn-agent.sh (with SCRIPT_DIR resolving to this temp dir)
#   - A stub pre-spawn-check.sh
#   - A stub rotate-team-log.sh
# The tracker binary is still sourced from $REPO_ROOT.

SCRIPTS_DIR="$TEST_DIR/scripts"
mkdir -p "$SCRIPTS_DIR"

# Stub rotate-team-log.sh (non-fatal warn path).
cat > "$SCRIPTS_DIR/rotate-team-log.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRIPTS_DIR/rotate-team-log.sh"

# Helper: write an allowing stub pre-spawn-check.sh into SCRIPTS_DIR.
write_allow_stub() {
  cat > "$SCRIPTS_DIR/pre-spawn-check.sh" <<'STUB'
#!/usr/bin/env bash
# Stub pre-spawn-check: always allowed
for arg in "$@"; do
  if [[ "${PREV:-}" == "--event-id" ]]; then EVID="$arg"; fi
  PREV="$arg"
done
echo "hook_event_id=${EVID:-test-event}"
cat <<JSON
{
  "allowed": true,
  "persona_voice": "",
  "working_principles": "",
  "self_observe_gate": "",
  "gate_context": {"gates": {}}
}
JSON
STUB
  chmod +x "$SCRIPTS_DIR/pre-spawn-check.sh"
}

# Helper: write a blocking stub (exits 1, no valid JSON).
write_block_stub() {
  cat > "$SCRIPTS_DIR/pre-spawn-check.sh" <<'STUB'
#!/usr/bin/env bash
# Stub pre-spawn-check: blocked
echo "hook_event_id=blocked-event"
echo '{"allowed": false, "reason": "budget_exceeded"}'
exit 1
STUB
  chmod +x "$SCRIPTS_DIR/pre-spawn-check.sh"
}

# Also stub gh (called by the PM-gate check in spawn-agent.sh for executor role)
# to return a SPEC_READY body so the PM gate passes when SPAWN_AGENT_ALLOW_NO_SPEC
# is NOT set.  We do set SPAWN_AGENT_ALLOW_NO_SPEC=1 in all tests so this stub is
# mostly a safety net to avoid real network calls.
cat > "$TEST_DIR/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$TEST_DIR/gh"

# Copy spawn-agent.sh into our temp scripts dir — `pwd` inside the script will
# resolve SCRIPT_DIR to $SCRIPTS_DIR, pointing to our stubs.
cp "$SPAWN_SCRIPT" "$SCRIPTS_DIR/spawn-agent.sh"
SPAWN_COPY="$SCRIPTS_DIR/spawn-agent.sh"

# Patch the copy to use REPO_ROOT from environment (so agent_run_tracker.py
# is still found in the real repo), and to find rotate-team-log.sh in SCRIPTS_DIR.
# The copy inherits REPO_ROOT from the env; no sed needed because spawn-agent.sh
# already computes REPO_ROOT as "$(cd "$SCRIPT_DIR/.." && pwd)" — and since we put
# the copy in $TEST_DIR/scripts/, REPO_ROOT will resolve to $TEST_DIR which is wrong.
# Fix: override REPO_ROOT explicitly via env-var wrapper.

run_spawn_copy() {
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  STATS_DB_PATH="$STATS_DB" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    bash "$SPAWN_COPY" \
      --role executor \
      --discussion 635 \
      --task-prompt "test task" \
      2>/dev/null
}

# Unfortunately spawn-agent.sh computes REPO_ROOT from SCRIPT_DIR at runtime —
# we cannot easily override it via env var without patching the script.
# Patch the copy to accept REPO_ROOT override:
sed -i 's|REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"|REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." \&\& pwd)}"|' \
  "$SPAWN_COPY" 2>/dev/null || true

# Query helper: count rows in agent_run matching a SQL condition.
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
except Exception:
    print("NULL")
PYEOF
}

# ── Test 1: start_run row created after successful spawn ──────────────────────

echo ""
echo "Test 1: start_run row appears in DuckDB after successful spawn"

write_allow_stub
run_spawn_copy > /dev/null 2>&1
RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "spawn exits 0"
else
  fail "spawn exit code" "expected 0, got $RC"
fi

COUNT=$(duckdb_count "role='executor'")
if [[ "$COUNT" -ge 1 ]]; then
  pass "agent_run row created (count=$COUNT)"
else
  fail "agent_run row count" "expected >=1, got $COUNT"
fi

END_TS=$(duckdb_field "end_ts" "role='executor'")
if [[ "$END_TS" == "None" || "$END_TS" == "NULL" || -z "$END_TS" ]]; then
  pass "end_ts is NULL (run open)"
else
  fail "end_ts" "expected NULL, got '$END_TS'"
fi

START_TS=$(duckdb_field "start_ts" "role='executor'")
if [[ "$START_TS" != "NULL" && "$START_TS" != "None" && -n "$START_TS" ]]; then
  pass "start_ts is non-null"
else
  fail "start_ts" "expected non-null, got '$START_TS'"
fi

# ── Test 2: role and event_id stored correctly ────────────────────────────────

echo ""
echo "Test 2: role and event_id stored in the agent_run row"

ROLE_STORED=$(duckdb_field "role" "role='executor'")
if [[ "$ROLE_STORED" == "executor" ]]; then
  pass "role stored as 'executor'"
else
  fail "role field" "expected 'executor', got '$ROLE_STORED'"
fi

EVID_STORED=$(duckdb_field "event_id" "role='executor'")
if [[ -n "$EVID_STORED" && "$EVID_STORED" != "NULL" && "$EVID_STORED" != "None" ]]; then
  pass "event_id is non-null ('$EVID_STORED')"
else
  fail "event_id field" "expected non-null, got '$EVID_STORED'"
fi

# discussion field should be 635
DISC_STORED=$(duckdb_field "discussion" "role='executor'")
if [[ "$DISC_STORED" == "635" ]]; then
  pass "discussion stored as 635"
else
  fail "discussion field" "expected 635, got '$DISC_STORED'"
fi

# ── Test 3: exit code 1 when pre-spawn-check exits non-zero ───────────────────

echo ""
echo "Test 3: exit code 1 when pre-spawn-check blocks"

write_block_stub
run_spawn_copy > /dev/null 2>&1
RC_BLOCK=$?

if [[ "$RC_BLOCK" -ne 0 ]]; then
  pass "exit code non-zero when blocked (got $RC_BLOCK)"
else
  fail "exit code when blocked" "expected non-zero, got 0"
fi

# ── Test 4: spawn exits 0 even when DuckDB is unwritable ─────────────────────

echo ""
echo "Test 4: spawn exits 0 even when DuckDB directory is unwritable"

write_allow_stub

UNWRITABLE_DIR="$TEST_DIR/locked"
mkdir -p "$UNWRITABLE_DIR"
chmod 000 "$UNWRITABLE_DIR"
UNWRITABLE_DB="$UNWRITABLE_DIR/stats.duckdb"

REPO_ROOT="$REPO_ROOT" \
PATH="$TEST_DIR:$PATH" \
STATS_DB_PATH="$UNWRITABLE_DB" \
SPAWN_AGENT_ALLOW_NO_SPEC=1 \
  bash "$SPAWN_COPY" \
    --role executor \
    --discussion 635 \
    --task-prompt "test task" \
    > /dev/null 2>&1
RC_UNWRITABLE=$?
chmod 755 "$UNWRITABLE_DIR"

if [[ "$RC_UNWRITABLE" -eq 0 ]]; then
  pass "spawn exits 0 with unwritable DuckDB"
else
  fail "spawn with unwritable DuckDB" "expected exit 0, got $RC_UNWRITABLE"
fi

# ── Test 5: multiple spawns create multiple rows ──────────────────────────────

echo ""
echo "Test 5: each spawn creates a separate agent_run row"

write_allow_stub

# Run two more spawns (sleep 1s to guarantee distinct event_ids which embed unix timestamp)
run_spawn_copy > /dev/null 2>&1
sleep 1
run_spawn_copy > /dev/null 2>&1

COUNT_MULTI=$(duckdb_count "role='executor'")
if [[ "$COUNT_MULTI" -ge 3 ]]; then
  pass "multiple spawns create multiple rows (total=$COUNT_MULTI)"
else
  fail "multiple rows" "expected >=3 rows after 3 spawns, got $COUNT_MULTI"
fi

# ── SR tests (D#2089): supersede-on-rebuild ───────────────────────────────────
# Each SR test gets its own fresh DuckDB file so cap counts (default 4
# executors / 8 total) never accumulate across tests and accidentally trip.

SR_DB="$TEST_DIR/stats_sr.duckdb"

run_spawn_sr() {
  local role="$1" disc="$2"
  local args=(--role "$role" --task-prompt "test task")
  [[ -n "$disc" ]] && args+=(--discussion "$disc")
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  STATS_DB_PATH="$SR_DB" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    bash "$SPAWN_COPY" "${args[@]}" 2>/dev/null
}

run_spawn_sr_capture_stderr() {
  local role="$1" disc="$2"
  local args=(--role "$role" --task-prompt "test task")
  [[ -n "$disc" ]] && args+=(--discussion "$disc")
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  STATS_DB_PATH="$SR_DB" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    bash "$SPAWN_COPY" "${args[@]}" 2>&1 1>/dev/null
}

run_spawn_sr_noregister() {
  local role="$1" disc="$2"
  local args=(--role "$role" --task-prompt "test task" --no-register)
  [[ -n "$disc" ]] && args+=(--discussion "$disc")
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  STATS_DB_PATH="$SR_DB" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    bash "$SPAWN_COPY" "${args[@]}" >/dev/null 2>&1
}

sr_seed_open() {
  local agent_id="$1" role="$2" disc="$3"
  local args=(start --agent-id "$agent_id" --role "$role")
  [[ -n "$disc" ]] && args+=(--discussion "$disc")
  STATS_DB_PATH="$SR_DB" python3 "$REPO_ROOT/backend/agent_run_tracker.py" "${args[@]}" >/dev/null 2>&1
}

sr_seed_closed() {
  local agent_id="$1" role="$2" disc="$3" verdict="$4"
  sr_seed_open "$agent_id" "$role" "$disc"
  STATS_DB_PATH="$SR_DB" python3 "$REPO_ROOT/backend/agent_run_tracker.py" complete \
    --agent-id "$agent_id" --verdict "$verdict" >/dev/null 2>&1
}

sr_duckdb_count() {
  local condition="${1:-1=1}"
  STATS_DB_PATH="$SR_DB" python3 - <<PYEOF 2>/dev/null || echo "0"
import os
try:
    import duckdb
    db = os.environ.get("STATS_DB_PATH", "$SR_DB")
    conn = duckdb.connect(db)
    row = conn.execute("SELECT COUNT(*) FROM agent_run WHERE $condition").fetchone()
    print(row[0] if row else 0)
    conn.close()
except Exception:
    print(0)
PYEOF
}

sr_duckdb_field() {
  local field="$1" condition="${2:-1=1}"
  STATS_DB_PATH="$SR_DB" python3 - <<PYEOF 2>/dev/null || echo "NULL"
import os
try:
    import duckdb
    db = os.environ.get("STATS_DB_PATH", "$SR_DB")
    conn = duckdb.connect(db)
    row = conn.execute("SELECT $field FROM agent_run WHERE $condition LIMIT 1").fetchone()
    val = row[0] if row else None
    print(val if val is not None else "NULL")
    conn.close()
except Exception:
    print("NULL")
PYEOF
}

write_allow_stub

echo ""
echo "Test SR-A: a rebuild does not stack"

rm -f "$SR_DB"
run_spawn_sr executor 9001 > /dev/null 2>&1
sleep 1  # guarantee distinct event_ids (role-discussion-unix_ts) across the rebuild
run_spawn_sr executor 9001 > /dev/null 2>&1

OPEN_A=$(sr_duckdb_count "role='executor' AND discussion=9001 AND end_ts IS NULL")
if [[ "$OPEN_A" -eq 1 ]]; then
  pass "SR-A: exactly one open row after rebuild (got $OPEN_A)"
else
  fail "SR-A: open row count" "expected 1, got $OPEN_A"
fi

SUPERSEDED_A=$(sr_duckdb_count "role='executor' AND discussion=9001 AND verdict='superseded' AND end_ts IS NOT NULL")
if [[ "$SUPERSEDED_A" -eq 1 ]]; then
  pass "SR-A: first row closed with verdict=superseded"
else
  fail "SR-A: superseded row count" "expected 1, got $SUPERSEDED_A"
fi

echo ""
echo "Test SR-B: different discussions both count"

rm -f "$SR_DB"
run_spawn_sr executor 9002 > /dev/null 2>&1
run_spawn_sr executor 9003 > /dev/null 2>&1

OPEN_B=$(sr_duckdb_count "role='executor' AND discussion IN (9002,9003) AND end_ts IS NULL")
if [[ "$OPEN_B" -eq 2 ]]; then
  pass "SR-B: both distinct-discussion rows stay open (got $OPEN_B)"
else
  fail "SR-B: open row count" "expected 2, got $OPEN_B"
fi

echo ""
echo "Test SR-C: different roles both count"

rm -f "$SR_DB"
run_spawn_sr executor 9004 > /dev/null 2>&1
run_spawn_sr code-reviewer 9004 > /dev/null 2>&1

OPEN_C=$(sr_duckdb_count "discussion=9004 AND end_ts IS NULL")
if [[ "$OPEN_C" -eq 2 ]]; then
  pass "SR-C: both distinct-role rows stay open (got $OPEN_C)"
else
  fail "SR-C: open row count" "expected 2, got $OPEN_C"
fi

echo ""
echo "Test SR-D: a closed row is never re-closed"

rm -f "$SR_DB"
sr_seed_closed seed-sr-d executor 9005 pass
BEFORE_VERDICT_D=$(sr_duckdb_field "verdict" "agent_id='seed-sr-d'")
BEFORE_END_D=$(sr_duckdb_field "end_ts" "agent_id='seed-sr-d'")

run_spawn_sr executor 9005 > /dev/null 2>&1

AFTER_VERDICT_D=$(sr_duckdb_field "verdict" "agent_id='seed-sr-d'")
AFTER_END_D=$(sr_duckdb_field "end_ts" "agent_id='seed-sr-d'")

if [[ "$AFTER_VERDICT_D" == "pass" && "$AFTER_VERDICT_D" == "$BEFORE_VERDICT_D" ]]; then
  pass "SR-D: seeded row's verdict unchanged ('$AFTER_VERDICT_D')"
else
  fail "SR-D: verdict" "expected unchanged 'pass', got before='$BEFORE_VERDICT_D' after='$AFTER_VERDICT_D'"
fi

if [[ "$AFTER_END_D" == "$BEFORE_END_D" && "$AFTER_END_D" != "NULL" && "$AFTER_END_D" != "None" ]]; then
  pass "SR-D: seeded row's end_ts unchanged"
else
  fail "SR-D: end_ts" "expected unchanged non-null, got before='$BEFORE_END_D' after='$AFTER_END_D'"
fi

OPEN_D=$(sr_duckdb_count "role='executor' AND discussion=9005 AND end_ts IS NULL")
if [[ "$OPEN_D" -eq 1 ]]; then
  pass "SR-D: the new spawn still creates its own open row"
else
  fail "SR-D: open row count" "expected 1, got $OPEN_D"
fi

echo ""
echo "Test SR-E: no discussion, no supersede"

rm -f "$SR_DB"
run_spawn_sr executor "" > /dev/null 2>&1
sleep 1  # guarantee distinct event_ids (role-nod-unix_ts) across the two builds
run_spawn_sr executor "" > /dev/null 2>&1

OPEN_E=$(sr_duckdb_count "role='executor' AND discussion IS NULL AND end_ts IS NULL")
if [[ "$OPEN_E" -eq 2 ]]; then
  pass "SR-E: both discussion-less rows stay open (got $OPEN_E)"
else
  fail "SR-E: open row count" "expected 2, got $OPEN_E"
fi

echo ""
echo "Test SR-F: --no-register supersedes nothing"

rm -f "$SR_DB"
sr_seed_open seed-sr-f executor 9006
run_spawn_sr_noregister executor 9006

OPEN_F=$(sr_duckdb_count "discussion=9006 AND end_ts IS NULL")
TOTAL_F=$(sr_duckdb_count "discussion=9006")
if [[ "$OPEN_F" -eq 1 && "$TOTAL_F" -eq 1 ]]; then
  pass "SR-F: seeded row untouched, no new row created (open=$OPEN_F total=$TOTAL_F)"
else
  fail "SR-F: row counts" "expected open=1 total=1, got open=$OPEN_F total=$TOTAL_F"
fi

SEED_VERDICT_F=$(sr_duckdb_field "verdict" "agent_id='seed-sr-f'")
if [[ "$SEED_VERDICT_F" == "NULL" || "$SEED_VERDICT_F" == "None" ]]; then
  pass "SR-F: seeded row's verdict still unset (not superseded)"
else
  fail "SR-F: seeded verdict" "expected NULL/unset, got '$SEED_VERDICT_F'"
fi

echo ""
echo "Test SR-G: the cap still blocks four genuinely distinct executors"

rm -f "$SR_DB"
sr_seed_open sr-g-1 executor 9101
sr_seed_open sr-g-2 executor 9102
sr_seed_open sr-g-3 executor 9103
sr_seed_open sr-g-4 executor 9104

run_spawn_sr executor 9105 > /dev/null 2>&1
RC_G=$?
if [[ "$RC_G" -ne 0 ]]; then
  pass "SR-G: fifth distinct-discussion executor is blocked (exit $RC_G)"
else
  fail "SR-G: exit code" "expected non-zero, got 0"
fi

echo ""
echo "Test SR-H: block message names every counted row and the recovery command"

rm -f "$SR_DB"
sr_seed_open sr-h-1 executor 9201
sr_seed_open sr-h-2 executor 9202
sr_seed_open sr-h-3 executor 9203
sr_seed_open sr-h-4 executor 9204

OUT_H=$(run_spawn_sr_capture_stderr executor 9205)

MISSING_H=""
for id in sr-h-1 sr-h-2 sr-h-3 sr-h-4; do
  echo "$OUT_H" | grep -q "$id" || MISSING_H="$MISSING_H $id"
done
if [[ -z "$MISSING_H" ]]; then
  pass "SR-H: all four counted agent_ids appear in the block message"
else
  fail "SR-H: missing agent_ids" "$MISSING_H"
fi

if echo "$OUT_H" | grep -q "agent_run_tracker.py reconcile --live-ids --stale-after-min 1"; then
  pass "SR-H: literal recovery command present"
else
  fail "SR-H: recovery command" "not found in: $OUT_H"
fi

if echo "$OUT_H" | grep -qE "ago\)"; then
  pass "SR-H: an age indicator is present"
else
  fail "SR-H: age indicator" "not found in: $OUT_H"
fi

echo ""
echo "Test SR-I: supersede is loud"

rm -f "$SR_DB"
sr_seed_open sr-i-1 executor 9302

OUT_I=$(run_spawn_sr_capture_stderr executor 9302)
if echo "$OUT_I" | grep -q "sr-i-1"; then
  pass "SR-I: superseded agent_id named on stderr"
else
  fail "SR-I: supersede notice" "'sr-i-1' not found in: $OUT_I"
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
