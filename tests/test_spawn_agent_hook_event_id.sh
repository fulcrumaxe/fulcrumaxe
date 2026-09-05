#!/usr/bin/env bash
# tests/test_spawn_agent_hook_event_id.sh — verify spawn-agent.sh appends
# hook_event_id=<role>-<disc>-<timestamp> as the last line of the assembled prompt.
#
# Without this tag the subagent-stop-hook.sh falls back to {role}-{disc}-{session_id}
# which never matches start_run's {role}-{disc}-{timestamp} key, so the UPSERT
# never merges token telemetry into the correct row.
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and a temp DuckDB path — no real GitHub API calls.
#
# Usage:
#   bash tests/test_spawn_agent_hook_event_id.sh
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

SCRIPTS_DIR="$TEST_DIR/scripts"
mkdir -p "$SCRIPTS_DIR"

# Stub rotate-team-log.sh (non-fatal warn path).
cat > "$SCRIPTS_DIR/rotate-team-log.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRIPTS_DIR/rotate-team-log.sh"

# Stub pre-spawn-check.sh — always allowed, echoes the event_id back.
write_allow_stub() {
  cat > "$SCRIPTS_DIR/pre-spawn-check.sh" <<'STUB'
#!/usr/bin/env bash
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

# Stub gh to avoid real network calls.
cat > "$TEST_DIR/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$TEST_DIR/gh"

# Copy spawn-agent.sh into temp scripts dir.
cp "$SPAWN_SCRIPT" "$SCRIPTS_DIR/spawn-agent.sh"
SPAWN_COPY="$SCRIPTS_DIR/spawn-agent.sh"

# Patch the copy so REPO_ROOT can be supplied via env.
sed -i 's|REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"|REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." \&\& pwd)}"|' \
  "$SPAWN_COPY" 2>/dev/null || true

run_spawn_copy() {
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  STATS_DB_PATH="$STATS_DB" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    bash "$SPAWN_COPY" \
      --role executor \
      --discussion 834 \
      --task-prompt "test task" \
      2>/dev/null
}

# ── Test 1: assembled prompt ends with hook_event_id=<role>-<disc>-<ts> ───────

echo ""
echo "Test 1: assembled prompt contains hook_event_id tag"

write_allow_stub
PROMPT_OUT=$(run_spawn_copy)
RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "spawn exits 0"
else
  fail "spawn exit code" "expected 0, got $RC"
fi

if echo "$PROMPT_OUT" | grep -q "^hook_event_id="; then
  pass "prompt contains hook_event_id line"
else
  fail "hook_event_id absent" "prompt does not contain 'hook_event_id=...' line"
fi

# ── Test 2: hook_event_id is the last non-empty line ──────────────────────────

echo ""
echo "Test 2: hook_event_id is the last line of the assembled prompt"

write_allow_stub
PROMPT_OUT=$(run_spawn_copy)

LAST_LINE=$(printf '%s' "$PROMPT_OUT" | grep -v '^$' | tail -1)
if echo "$LAST_LINE" | grep -q "^hook_event_id="; then
  pass "hook_event_id is the last non-empty line ('$LAST_LINE')"
else
  fail "hook_event_id not last" "last non-empty line was: '$LAST_LINE'"
fi

# ── Test 3: hook_event_id value matches expected format <role>-<disc>-<ts> ────

echo ""
echo "Test 3: hook_event_id value has format executor-834-<timestamp>"

write_allow_stub
PROMPT_OUT=$(run_spawn_copy)

EVID_LINE=$(printf '%s' "$PROMPT_OUT" | grep "^hook_event_id=" | tail -1)
EVID_VAL="${EVID_LINE#hook_event_id=}"

if echo "$EVID_VAL" | grep -qE "^executor-834-[0-9]+$"; then
  pass "hook_event_id format correct ('$EVID_VAL')"
else
  fail "hook_event_id format" "expected executor-834-<timestamp>, got '$EVID_VAL'"
fi

# ── Test 4: hook_event_id appears even when TEMPLATE_BODY is empty ────────────

echo ""
echo "Test 4: hook_event_id present when no --template flag is passed (TEMPLATE_BODY empty)"

write_allow_stub
PROMPT_OUT=$(run_spawn_copy)

if echo "$PROMPT_OUT" | grep -q "^hook_event_id="; then
  pass "hook_event_id present with no template"
else
  fail "hook_event_id with no template" "hook_event_id line missing"
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
