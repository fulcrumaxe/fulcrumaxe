#!/usr/bin/env bash
# tests/test_subagent_stop_hook.sh — Unit tests for scripts/subagent-stop-hook.sh
#
# Tests 4 envelope-parsing cases:
#   1. Well-formed envelope — all fields extracted correctly
#   2. Malformed JSON envelope — falls back to verdict=unknown, exits 0
#   3. Missing envelope — no AGENT_OUTPUT block in transcript, exits 0
#   4. Multi-block transcript — uses the LAST envelope when multiple exist
#
# Tests use a SUBAGENT_STOP_TEST_ARGS_FILE env var: when set, the hook
# writes its resolved args to that file instead of calling post-agent-hook.sh.
# This keeps the tests fast, but SUBAGENT_STOP_DRY_RUN does NOT make every
# path isolated from live state (D#2267): the unknown:unknown noise-gate
# branch in scripts/subagent-stop-hook.sh (Cases 9/10 below) writes a real
# counter row to $REPO_ROOT/.autonomous-team/stats/unknown_subagent_stops-
# <date>.jsonl unconditionally, BEFORE the dry-run branch is ever reached —
# exercised by every run of this suite. subagent-stop-hook.sh already ships
# a test-only seam for exactly this ("SUBAGENT_STOP_REPO_ROOT_OVERRIDE is a
# test-only seam (unset in production)"); use it so this suite's own writes
# (and Case 11's stray-file scan below) land in a fixture instead of the
# live tree.

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

# Create a minimal JSONL transcript with a single assistant message (flat/Shape B format)
_make_transcript() {
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

# Create a JSONL transcript in real Claude Code format (Shape A: type=message wrapper)
_make_transcript_real() {
  local dir="$1"
  local text="$2"
  local path="$dir/transcript_real.jsonl"
  python3 -c "
import json, sys
# Mimic real Claude Code transcript format:
#   {\"type\": \"message\", \"message\": {\"role\": \"assistant\", \"content\": [{\"type\": \"text\", \"text\": \"...\"}]}}
msg = {
    'type': 'message',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': sys.argv[1]}]
    }
}
print(json.dumps(msg))
" "$text" > "$path"
  echo "$path"
}

# Run the hook in test mode: sets SUBAGENT_STOP_DRY_RUN=1 so the hook
# skips the actual post-agent-hook.sh call and writes resolved args to a file.
_run_hook_dry() {
  local transcript_path="$1"
  local session_id="${2:-test-session-abc123}"
  local args_file="$3"

  local stdin_json
  stdin_json=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': sys.argv[1],
    'transcript_path': sys.argv[2],
    'cwd': '/tmp/test-worktree'
}))
" "$session_id" "$transcript_path")

  SUBAGENT_STOP_DRY_RUN=1 \
  SUBAGENT_STOP_ARGS_FILE="$args_file" \
    bash "$HOOK" <<< "$stdin_json"
}

# ── Case 1: Well-formed envelope ───────────────────────────────────────────────
echo "Case 1: well-formed envelope"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"

  ENVELOPE_TEXT='Some work done here.

<!-- AGENT_OUTPUT -->
```json
{
  "agent": "executor",
  "discussion": 42,
  "pr": 99,
  "verdict": "done",
  "files_touched": ["src/foo.ts", "src/bar.ts"],
  "tokens_used": {"input": 12000, "output": 3400}
}
```
<!-- /AGENT_OUTPUT -->'

  TRANSCRIPT=$(_make_transcript "$TMP" "$ENVELOPE_TEXT")
  _run_hook_dry "$TRANSCRIPT" "sess-001" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("role") == "executor",       f"role wrong: {d}"
assert str(d.get("discussion")) == "42",  f"discussion wrong: {d}"
assert str(d.get("pr")) == "99",          f"pr wrong: {d}"
assert d.get("verdict") == "done",        f"verdict wrong: {d}"
assert d.get("input_tokens") == 12000,    f"input_tokens wrong: {d}"
assert d.get("output_tokens") == 3400,    f"output_tokens wrong: {d}"
assert "src/foo.ts" in d.get("files",""), f"files wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "all fields parsed correctly" || _fail "field parse mismatch"
  else
    _fail "args file not created ($ARGS_FILE)"
  fi

  rm -rf "$TMP"
}

# ── Case 2: Malformed JSON envelope ───────────────────────────────────────────
# A malformed envelope (broken JSON) still means no envelope was parsed
# (PARSE_OK=false). With no spawn event-id either, this is an unknown:unknown
# row that gets suppressed by the noise gate — no args file should be written.
echo "Case 2: malformed JSON envelope — suppressed by unknown:unknown gate"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"

  ENVELOPE_TEXT='<!-- AGENT_OUTPUT -->
```json
{ "agent": "executor", "verdict": "done" BROKEN JSON HERE
```
<!-- /AGENT_OUTPUT -->'

  TRANSCRIPT=$(_make_transcript "$TMP" "$ENVELOPE_TEXT")
  _run_hook_dry "$TRANSCRIPT" "sess-002" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0 on malformed JSON" || _fail "expected exit 0, got $EXIT_CODE"

  # Malformed envelope + no spawn event-id = unknown:unknown → gate suppresses write
  if [[ ! -f "$ARGS_FILE" ]]; then
    _pass "args file not written — malformed envelope with no spawn id is correctly suppressed"
  else
    _fail "args file was written — unknown:unknown gate should have suppressed this row"
  fi

  rm -rf "$TMP"
}

# ── Case 3: Missing envelope ───────────────────────────────────────────────────
# Pure prose with no AGENT_OUTPUT and no spawn event-id = unknown:unknown row.
# The noise gate suppresses the write entirely — no args file should be created.
echo "Case 3: missing envelope (no AGENT_OUTPUT block) — suppressed by unknown:unknown gate"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"

  ENVELOPE_TEXT='This is the final message from the agent.
No structured output here. Just prose.'

  TRANSCRIPT=$(_make_transcript "$TMP" "$ENVELOPE_TEXT")
  _run_hook_dry "$TRANSCRIPT" "sess-003" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0 on missing envelope" || _fail "expected exit 0, got $EXIT_CODE"

  # No envelope + no spawn event-id = unknown:unknown → gate suppresses write
  if [[ ! -f "$ARGS_FILE" ]]; then
    _pass "args file not written — pure-prose agent with no spawn id is correctly suppressed"
  else
    _fail "args file was written — unknown:unknown gate should have suppressed this row"
  fi

  rm -rf "$TMP"
}

# ── Case 4: Multi-block transcript — uses the LAST envelope ───────────────────
echo "Case 4: multi-block transcript — last envelope wins"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"

  # Two envelopes in the same message; the last one has pr=777 and verdict=done
  ENVELOPE_TEXT='First attempt:

<!-- AGENT_OUTPUT -->
```json
{
  "agent": "executor",
  "discussion": 10,
  "pr": 111,
  "verdict": "fail"
}
```
<!-- /AGENT_OUTPUT -->

After fixing, final output:

<!-- AGENT_OUTPUT -->
```json
{
  "agent": "executor",
  "discussion": 10,
  "pr": 777,
  "verdict": "done",
  "tokens_used": {"input": 5000, "output": 1000}
}
```
<!-- /AGENT_OUTPUT -->'

  TRANSCRIPT=$(_make_transcript "$TMP" "$ENVELOPE_TEXT")
  _run_hook_dry "$TRANSCRIPT" "sess-004" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0 on multi-block" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert str(d.get("pr")) == "777",      f"expected pr=777 (last envelope), got: {d}"
assert d.get("verdict") == "done",     f"expected verdict=done (last envelope), got: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "last envelope used (pr=777, verdict=done)" || _fail "last envelope not used"
  else
    _fail "args file not created"
  fi

  rm -rf "$TMP"
}

# ── Case 5: Real Claude Code transcript format (Shape A) — wraps in type=message ──
echo "Case 5: real Claude Code transcript format (type=message wrapper)"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"

  # This is what real Claude Code subagents actually write to their transcript files.
  # The JSONL row looks like: {"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"..."}]}}
  ENVELOPE_TEXT='Some work was done here.

<!-- AGENT_OUTPUT -->
```json
{
  "agent": "executor",
  "discussion": 99,
  "pr": 123,
  "verdict": "done",
  "files_touched": ["backend/foo.py"],
  "tokens_used": {"input": 55000, "output": 4200}
}
```
<!-- /AGENT_OUTPUT -->'

  TRANSCRIPT=$(_make_transcript_real "$TMP" "$ENVELOPE_TEXT")
  _run_hook_dry "$TRANSCRIPT" "sess-005" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0 on real-format transcript" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("role") == "executor",        f"role wrong: {d}"
assert str(d.get("discussion")) == "99",   f"discussion wrong: {d}"
assert str(d.get("pr")) == "123",          f"pr wrong: {d}"
assert d.get("verdict") == "done",         f"verdict wrong: {d}"
assert d.get("input_tokens") == 55000,     f"input_tokens wrong: {d}"
assert d.get("output_tokens") == 4200,     f"output_tokens wrong: {d}"
assert "backend/foo.py" in d.get("files",""), f"files wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "all fields extracted from real-format transcript" || _fail "field parse mismatch on real-format"
  else
    _fail "args file not created for real-format transcript"
  fi

  rm -rf "$TMP"
}

# ── Case 6: Real format with mixed message types (system, tool_use, tool_result) ──
echo "Case 6: real format — mixed message types, assistant message is last"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  TRANSCRIPT="$TMP/transcript_mixed.jsonl"

  # A realistic multi-turn transcript including system/user/assistant/tool messages
  python3 - "$TRANSCRIPT" <<'PYEOF'
import json, sys
path = sys.argv[1]
rows = [
    # System message
    {"type": "system", "message": "Loaded session", "timestamp": "2026-05-14T00:00:00Z"},
    # User turn
    {"type": "message", "message": {"role": "user", "content": "Implement the fix for D#813."}},
    # Assistant tool_use turn
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git status"}}
    ]}},
    # User tool_result turn
    {"type": "message", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "On branch fix-813\nnothing to commit", "is_error": False}
    ]}},
    # Final assistant message with AGENT_OUTPUT envelope
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Done fixing the parser.\n\n<!-- AGENT_OUTPUT -->\n```json\n{\"agent\": \"executor\", \"discussion\": 813, \"pr\": 900, \"verdict\": \"done\", \"tokens_used\": {\"input\": 80000, \"output\": 6000}}\n```\n<!-- /AGENT_OUTPUT -->"}
    ]}}
]
with open(path, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")
PYEOF

  _run_hook_dry "$TRANSCRIPT" "sess-006" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0 on mixed-turn transcript" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("role") == "executor",         f"role wrong: {d}"
assert str(d.get("discussion")) == "813",   f"discussion wrong: {d}"
assert str(d.get("pr")) == "900",           f"pr wrong: {d}"
assert d.get("verdict") == "done",          f"verdict wrong: {d}"
assert d.get("input_tokens") == 80000,      f"input_tokens wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "all fields extracted from mixed-turn transcript" || _fail "field parse mismatch on mixed-turn"
  else
    _fail "args file not created for mixed-turn transcript"
  fi

  rm -rf "$TMP"
}

# ── Case 7: Shape A with type="user" outer wrapper ────────────────────────────
echo "Case 7: Shape A with type=\"user\" outer wrapper"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  TRANSCRIPT="$TMP/transcript_type_user.jsonl"

  # Newer Claude Code builds emit type="user" or type="assistant" instead of
  # type="message" for the outer wrapper.  This was the root cause of D#824:
  # the old discriminator only checked for "message", so real transcripts
  # fell through to Shape B (flat) parsing and always returned role=unknown.
  python3 - "$TRANSCRIPT" <<'PYEOF'
import json, sys
path = sys.argv[1]
rows = [
    # User turn with type="user" (newer Claude Code format)
    {"type": "user", "message": {"role": "user", "content": "Fix the bug."}},
    # Assistant turn with type="assistant" — envelope is in this message
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Fixed it.\n\n<!-- AGENT_OUTPUT -->\n```json\n{\"agent\": \"executor\", \"discussion\": 824, \"pr\": 825, \"verdict\": \"done\", \"tokens_used\": {\"input\": 30000, \"output\": 2500}}\n```\n<!-- /AGENT_OUTPUT -->"}
    ]}}
]
with open(path, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")
PYEOF

  _run_hook_dry "$TRANSCRIPT" "sess-007" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0 on type=user/assistant transcript" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("role") == "executor",         f"role wrong (type=user/assistant): {d}"
assert str(d.get("discussion")) == "824",   f"discussion wrong: {d}"
assert str(d.get("pr")) == "825",           f"pr wrong: {d}"
assert d.get("verdict") == "done",          f"verdict wrong: {d}"
assert d.get("input_tokens") == 30000,      f"input_tokens wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "role/discussion/pr/verdict extracted correctly from type=user/assistant transcript" || _fail "field parse mismatch on type=user/assistant"
  else
    _fail "args file not created for type=user/assistant transcript"
  fi

  rm -rf "$TMP"
}

# ── Case 8: Shape A with type="user" only — role must not fall through to unknown ─
echo "Case 8: Shape A type=\"user\" wrapper — role must not be unknown"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  TRANSCRIPT="$TMP/transcript_type_user_only.jsonl"

  # Single-message transcript using type="user" wrapper.
  # Before the fix this would produce role=unknown because the discriminator
  # skipped the message and found no flat "role" field at top level.
  python3 - "$TRANSCRIPT" <<'PYEOF'
import json, sys
path = sys.argv[1]
rows = [
    {"type": "user", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Completed task.\n\n<!-- AGENT_OUTPUT -->\n```json\n{\"agent\": \"code-reviewer\", \"discussion\": 200, \"verdict\": \"pass\"}\n```\n<!-- /AGENT_OUTPUT -->"}
    ]}}
]
with open(path, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")
PYEOF

  _run_hook_dry "$TRANSCRIPT" "sess-008" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("role") == "code-reviewer",  f"expected role=code-reviewer, got: {d}"
assert d.get("verdict") == "pass",        f"expected verdict=pass, got: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "role=code-reviewer correctly extracted (not unknown)" || _fail "role fell through to unknown — discriminator bug still present"
  else
    _fail "args file not created"
  fi

  rm -rf "$TMP"
}

# ── Case 9: Bug A fix — role extracted from hook_event_id when envelope absent ─
# When no AGENT_OUTPUT envelope exists but a hook_event_id tag is in the
# transcript (injected by spawn-agent.sh), the role must be parsed from that
# event-id rather than defaulting to "unknown".
echo "Case 9: Bug A fix — role extracted from hook_event_id in transcript user message"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  TRANSCRIPT="$TMP/transcript_no_envelope.jsonl"

  # Transcript: user message with hook_event_id tag (as injected by spawn-agent.sh),
  # then assistant message with prose only (no AGENT_OUTPUT envelope).
  # NOTE (D#1807): the tag prefix below is split into adjacent string literals
  # so this line never plants a canonical-shaped id in a reading agent's
  # transcript. Concatenation makes the generated fixture byte-identical.
  python3 - "$TRANSCRIPT" <<'PYEOF'
import json, sys
path = sys.argv[1]
rows = [
    # User message containing the hook_event_id tag (spawn-agent.sh injects this)
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "Implement the fix.\n\n" "hook_event_" "id=project-manager-92-1715800000"}
    ]}},
    # Assistant message with prose only — no AGENT_OUTPUT envelope
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Done! The plan has been written to the Discussion."}
    ]}}
]
with open(path, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")
PYEOF

  _run_hook_dry "$TRANSCRIPT" "sess-009" "$ARGS_FILE"
  EXIT_CODE=$?

  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
# Role must be extracted from hook_event_id ("project-manager-92-..."),
# not left as "unknown" just because no AGENT_OUTPUT envelope was emitted.
assert d.get("role") == "project-manager", f"expected role=project-manager (from hook_event_id), got: {d}"
# Event-id must be the canonical spawn event-id, not the fallback formula.
assert d.get("event_id") == "project-manager-92-1715800000", f"event_id wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "role=project-manager extracted from hook_event_id (Bug A fix)" || _fail "role not extracted from hook_event_id"
  else
    _fail "args file not created"
  fi

  rm -rf "$TMP"
}

# ── Case 10: Concurrent unknown agents — both suppressed by noise gate ──────────
# Two subagents with no hook_event_id and no AGENT_OUTPUT envelope are pure noise.
# The noise gate introduced in D#1075 suppresses both entirely — neither should
# write an args file.  The old Bug B (dedup on shared event-id) is now moot
# because these rows never reach post-agent-hook.sh in the first place.
echo "Case 10: concurrent unknown:unknown agents — both suppressed by noise gate"
{
  TMP=$(mktemp -d)
  ARGS_FILE_A="$TMP/args_a.json"
  ARGS_FILE_B="$TMP/args_b.json"

  # Transcript with no hook_event_id and no AGENT_OUTPUT envelope
  TRANSCRIPT_A="$TMP/transcript_a.jsonl"
  TRANSCRIPT_B="$TMP/transcript_b.jsonl"
  python3 - "$TRANSCRIPT_A" "$TRANSCRIPT_B" <<'PYEOF'
import json, sys
for path in sys.argv[1:]:
    with open(path, "w") as f:
        f.write(json.dumps({"role": "assistant", "content": "Prose only. No envelope."}) + "\n")
PYEOF

  SAME_SESSION="parent-sess-xyz789"
  SUBAGENT_STOP_DRY_RUN=1 SUBAGENT_STOP_ARGS_FILE="$ARGS_FILE_A" \
    bash "$HOOK" <<< "$(python3 -c "
import json, sys
print(json.dumps({'hook_event_name': 'SubagentStop', 'session_id': sys.argv[1], 'transcript_path': sys.argv[2], 'cwd': '/tmp/wt'}))
" "$SAME_SESSION" "$TRANSCRIPT_A")"
  SUBAGENT_STOP_DRY_RUN=1 SUBAGENT_STOP_ARGS_FILE="$ARGS_FILE_B" \
    bash "$HOOK" <<< "$(python3 -c "
import json, sys
print(json.dumps({'hook_event_name': 'SubagentStop', 'session_id': sys.argv[1], 'transcript_path': sys.argv[2], 'cwd': '/tmp/wt'}))
" "$SAME_SESSION" "$TRANSCRIPT_B")"

  if [[ ! -f "$ARGS_FILE_A" && ! -f "$ARGS_FILE_B" ]]; then
    _pass "both concurrent unknown:unknown agents suppressed — no args files written"
  else
    _fail "one or both unknown:unknown agents wrote args files — noise gate did not fire"
  fi

  rm -rf "$TMP"
}

# ── Case 11: EVENT_ID guard — empty EVENT_ID must exit non-zero ───────────────
# Reproduces the original D#1078 bug: if EVENT_ID were somehow empty (e.g. an
# earlier refactor broke the if/else that sets it), the hook must fail loudly
# instead of silently passing an empty string to --event-id and potentially
# creating literal '${EVENT_ID}*.lock' files in hook-events/.
#
# This test directly exercises the `: "${EVENT_ID:?...}"` guard by sourcing a
# minimal bash snippet that matches the exact guard line in the hook.
echo "Case 11: EVENT_ID guard — empty EVENT_ID must exit non-zero (D#1078)"
{
  # Verify the guard is present in the hook source (regression guard)
  if grep -q 'EVENT_ID:?.*subagent-stop-hook.*EVENT_ID is unset' "$HOOK"; then
    _pass "set-or-die guard present in hook source"
  else
    _fail "set-or-die guard missing from hook source — D#1078 regression"
  fi

  # Exercise the guard: run a bash subshell that sets EVENT_ID="" then hits the guard
  # Expect exit code 1 (set -u + :? expansion fails)
  GUARD_EXIT=0
  bash -c '
    set -uo pipefail
    EVENT_ID=""
    : "${EVENT_ID:?subagent-stop-hook: EVENT_ID is unset or empty — this is a bug}"
    echo "SHOULD NOT REACH HERE"
  ' 2>/dev/null && GUARD_EXIT=0 || GUARD_EXIT=$?

  if [[ "$GUARD_EXIT" -ne 0 ]]; then
    _pass "empty EVENT_ID causes non-zero exit (guard fires correctly)"
  else
    _fail "empty EVENT_ID silently accepted — guard not working"
  fi

  # Verify no literal '${EVENT_ID}' files remain in hook-events/. Scoped to
  # this suite's own fixture (FIXTURE_ROOT), not the live tree: with the
  # guard above working, this suite's own invocations of $HOOK never reach
  # the code path that could create one, so this is a regression guard on
  # *this run*, not a scan of accumulated production history (D#2267).
  HOOK_EVENTS_DIR="$FIXTURE_ROOT/.autonomous-team/hook-events"
  LITERAL_FILES=0
  if [[ -d "$HOOK_EVENTS_DIR" ]]; then
    # Look for files whose names literally contain the string ${EVENT_ID}
    while IFS= read -r -d '' f; do
      LITERAL_FILES=$((LITERAL_FILES + 1))
    done < <(find "$HOOK_EVENTS_DIR" -name '*${EVENT_ID}*' -print0 2>/dev/null)
  fi

  if [[ "$LITERAL_FILES" -eq 0 ]]; then
    _pass "no literal '\${EVENT_ID}' files in hook-events/ (cleanup confirmed)"
  else
    _fail "found $LITERAL_FILES literal '\${EVENT_ID}' file(s) in hook-events/ — D#1078 not fully resolved"
  fi
}

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
