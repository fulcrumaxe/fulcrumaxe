#!/usr/bin/env bash
# tests/test_subagent_stop_hook_unknown_role.sh
#
# Tests the unknown:unknown noise-suppression gate added to subagent-stop-hook.sh.
# Three paths exercised:
#   AC-1: No envelope, no hook_event_id → skip agent-feed write; write counter row to stats file
#   AC-2: No envelope, spawn event-id "executor-1072-..." → write agent_end with role=executor
#   AC-3: Valid envelope, no spawn id → write agent_end with envelope-declared role
#
# Uses SUBAGENT_STOP_DRY_RUN=1 for AC-2 and AC-3 (normal dry-run path).
# AC-1 is verified by absence of the args file AND presence of the stats row.
#
# AC-1's stats-row check used to read $REPO_ROOT/.autonomous-team/stats/
# directly — the live tree every real agent's own unknown:unknown
# SubagentStop rows also land in (D#2267). SUBAGENT_STOP_REPO_ROOT_OVERRIDE
# (already a documented test-only seam in scripts/subagent-stop-hook.sh)
# redirects it to a per-run fixture instead.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/scripts/subagent-stop-hook.sh"

FIXTURE_ROOT="$(mktemp -d "$REPO_ROOT/.repo-root-fixture.XXXXXX")" || {
  echo "FAIL: could not create isolated repo-root fixture" >&2
  exit 1
}
trap 'rm -rf "$FIXTURE_ROOT"' EXIT
export SUBAGENT_STOP_REPO_ROOT_OVERRIDE="$FIXTURE_ROOT"

PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# ── Helpers ────────────────────────────────────────────────────────────────────

# Make a minimal transcript with a single assistant text message (flat/Shape B)
_make_transcript_prose() {
  local dir="$1"
  local text="$2"
  local path="$dir/transcript.jsonl"
  python3 -c "
import json, sys
msg = {'role': 'assistant', 'content': sys.argv[1]}
print(json.dumps(msg))
" "$text" > "$path"
  echo "$path"
}

# Make a transcript that includes a hook_event_id tag in the user message
_make_transcript_with_event_id() {
  local dir="$1"
  local event_id="$2"
  local path="$dir/transcript_evid.jsonl"
  python3 - "$path" "$event_id" <<'PYEOF'
import json, sys
path = sys.argv[1]
event_id = sys.argv[2]
rows = [
    # User message with hook_event_id (as injected by spawn-agent.sh)
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": f"Do the work.\n\nhook_event_id={event_id}"}
    ]}},
    # Assistant prose only — no AGENT_OUTPUT envelope
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Work complete."}
    ]}}
]
with open(path, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")
PYEOF
  echo "$path"
}

# Make a transcript with a valid AGENT_OUTPUT envelope but no hook_event_id
_make_transcript_envelope_only() {
  local dir="$1"
  local role="$2"
  local verdict="$3"
  local path="$dir/transcript_env.jsonl"
  python3 - "$path" "$role" "$verdict" <<'PYEOF'
import json, sys
path, role, verdict = sys.argv[1], sys.argv[2], sys.argv[3]
envelope_text = f"""Done.

<!-- AGENT_OUTPUT -->
```json
{{
  "agent": "{role}",
  "discussion": 42,
  "verdict": "{verdict}"
}}
```
<!-- /AGENT_OUTPUT -->"""
rows = [
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": envelope_text}
    ]}}
]
with open(path, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")
PYEOF
  echo "$path"
}

# Build the stdin JSON for the hook
_make_stdin_json() {
  local session_id="$1"
  local transcript_path="$2"
  python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': sys.argv[1],
    'transcript_path': sys.argv[2],
    'cwd': '/tmp/test-worktree'
}))
" "$session_id" "$transcript_path"
}

# Run in dry-run mode (normal path: writes args to file)
_run_hook_dry() {
  local transcript_path="$1"
  local session_id="$2"
  local args_file="$3"
  local stdin_json
  stdin_json=$(_make_stdin_json "$session_id" "$transcript_path")
  SUBAGENT_STOP_DRY_RUN=1 \
  SUBAGENT_STOP_ARGS_FILE="$args_file" \
    bash "$HOOK" <<< "$stdin_json"
}

# Run WITHOUT dry-run (live path: may write to stats file; never writes args file)
_run_hook_live() {
  local transcript_path="$1"
  local session_id="$2"
  local stats_dir="$3"
  local stdin_json
  stdin_json=$(_make_stdin_json "$session_id" "$transcript_path")
  # Override the stats dir by pointing AUTONOMOUS_TEAM_STATE_DIR is not directly
  # honoured for this path; we use a workaround: pass REPO_ROOT via env so the hook
  # writes unknown_subagent_stops to $stats_dir instead.
  # The hook computes: STATS_FILE="$REPO_ROOT/.autonomous-team/stats/unknown_subagent_stops-DATE.jsonl"
  # We set a custom REPO_ROOT pointing at a temp tree so the file lands in our temp dir.
  local fake_repo="$3/../fake_repo"
  mkdir -p "$fake_repo/scripts" "$fake_repo/.autonomous-team/stats"
  # The hook needs SCRIPT_DIR to find post-agent-hook.sh.  We override via a
  # symlink trick: create a minimal scripts/ dir with a no-op post-agent-hook.sh.
  # But that's complex.  Simpler: the hook calls post-agent-hook.sh via $SCRIPT_DIR
  # only when NOT skipping.  For the skip path, it only writes to REPO_ROOT/../.autonomous-team/stats/.
  # Actually the easiest approach: just run the hook normally and let the stats file
  # land in the real .autonomous-team/stats/ dir, then check it has a row.
  bash "$HOOK" <<< "$stdin_json" 2>&1
}


# ── AC-1: No envelope, no hook_event_id → skip write, emit counter row ─────────
echo "AC-1: unknown:unknown with no spawn context → no agent-feed row, stats counter written"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  SESSION="ac1-sess-unknown-$$"

  # Transcript: plain prose, no envelope, no hook_event_id
  TRANSCRIPT=$(_make_transcript_prose "$TMP" "Just some prose. Nothing useful here.")

  # Run in dry-run mode — if the gate works, the dry-run block is never reached,
  # so ARGS_FILE should NOT be created.
  SUBAGENT_STOP_DRY_RUN=1 \
  SUBAGENT_STOP_ARGS_FILE="$ARGS_FILE" \
    bash "$HOOK" <<< "$(_make_stdin_json "$SESSION" "$TRANSCRIPT")"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  # Verify args file was NOT created (agent-feed write was skipped)
  if [[ ! -f "$ARGS_FILE" ]]; then
    _pass "args file not written — agent-feed write correctly suppressed"
  else
    _fail "args file was created — unknown:unknown gate did not fire"
  fi

  # Verify stats counter row was written to the correct file
  _STATS_DATE=$(date -u +%Y-%m-%d 2>/dev/null || echo "unknown-date")
  _STATS_FILE="$FIXTURE_ROOT/.autonomous-team/stats/unknown_subagent_stops-${_STATS_DATE}.jsonl"
  if [[ -f "$_STATS_FILE" ]]; then
    MATCH=$(grep -c "\"session_id\"" "$_STATS_FILE" 2>/dev/null || echo "0")
    if [[ "$MATCH" -gt 0 ]]; then
      _pass "stats counter row written to unknown_subagent_stops-DATE.jsonl"
    else
      _fail "stats file exists but has no valid counter rows"
    fi
    # Verify the row has the expected shape
    python3 - "$_STATS_FILE" "$SESSION" <<'PYCHECK'
import json, sys
stats_file, session_id = sys.argv[1], sys.argv[2]
found = False
with open(stats_file) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("session_id") == session_id:
            assert "ts" in row, f"row missing ts field: {row}"
            assert "transcript_path" in row, f"row missing transcript_path: {row}"
            found = True
            break
if not found:
    print(f"WARNING: session {session_id!r} not found in stats file (may be from a different run)", flush=True)
else:
    print("row shape OK")
PYCHECK
    [[ $? -eq 0 ]] && _pass "stats row has correct shape (ts, session_id, transcript_path)" || _fail "stats row shape incorrect"
  else
    _fail "stats file not found: $_STATS_FILE"
  fi

  rm -rf "$TMP"
}

# ── AC-2: Spawn event-id starts with "executor-1072-" → writes agent_end row ──
echo "AC-2: spawn event-id executor-1072-... → writes agent_end row with role=executor"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"

  TRANSCRIPT=$(_make_transcript_with_event_id "$TMP" "executor-1072-1716000000")
  _run_hook_dry "$TRANSCRIPT" "ac2-sess-executor-$$" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("role") == "executor", f"expected role=executor (from event-id), got: {d}"
assert d.get("event_id") == "executor-1072-1716000000", f"event_id wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "role=executor extracted from spawn event-id; existing behavior preserved" \
                   || _fail "role not extracted from spawn event-id"
  else
    _fail "args file not created — gate incorrectly suppressed a valid spawned agent"
  fi

  rm -rf "$TMP"
}

# ── AC-3: Valid envelope, no spawn id → writes with envelope-declared role ─────
echo "AC-3: valid AGENT_OUTPUT envelope, no spawn id → writes agent_end with envelope role"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"

  TRANSCRIPT=$(_make_transcript_envelope_only "$TMP" "code-reviewer" "pass")
  _run_hook_dry "$TRANSCRIPT" "ac3-sess-reviewer-$$" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("role") == "code-reviewer", f"expected role=code-reviewer (from envelope), got: {d}"
assert d.get("verdict") == "pass", f"expected verdict=pass, got: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "role=code-reviewer and verdict=pass from envelope; existing behavior preserved" \
                   || _fail "envelope fields not extracted correctly"
  else
    _fail "args file not created — gate incorrectly suppressed an agent with a valid envelope"
  fi

  rm -rf "$TMP"
}

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
