#!/usr/bin/env bash
# tests/test_subagent_tool_use_count.sh — D#1791 PR 1 acceptance items 1-6.
#
# Counting and persisting tool_uses as a TRI-STATE is the entire point of
# this PR: a fabricated evidence envelope from a run that made zero tool
# calls must be detectable as impossible (D#1791 PR 2's job), which requires
# `tool_uses == 0` to never mean the same thing as "we don't know". This
# suite pins all three states end to end: a real count, a genuine zero, and
# absent/unreadable.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/scripts/subagent-stop-hook.sh"
TRACKER="$REPO_ROOT/backend/agent_run_tracker.py"

# blackboard_scratch_state_dir must be called directly (not via command
# substitution) so its `export` lands in this shell, per CLAUDE.md.
source "$SCRIPT_DIR/lib/blackboard-fixture.sh"
blackboard_scratch_state_dir || { echo "FAIL: could not create scratch state dir" >&2; exit 1; }
SCRATCH_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
trap 'rm -rf "$SCRATCH_STATE_DIR"' EXIT

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

_run_hook_dry() {
  local stdin_json="$1"
  local args_file="$2"
  SUBAGENT_STOP_DRY_RUN=1 \
  SUBAGENT_STOP_ARGS_FILE="$args_file" \
    bash "$HOOK" <<< "$stdin_json"
}

# Builds a SubagentStop payload whose files_touched[0] is "a<char>b", where
# <char> is the raw byte at the given ordinal (10 = newline, 31 = \x1f) —
# constructed via chr(ordv) at the Python level and serialized with
# json.dumps(), never typed as a literal escape sequence in this file's own
# source text. That distinction matters: an escape sequence typed here can
# get collapsed to the raw byte (or vice versa) by an editing/transport layer
# before this file is even saved, at which point the payload built from it
# no longer reproduces the finding it was meant to (measured while writing
# this suite — see the item 8 commit history). Building the byte at Python
# runtime from a plain decimal ordinal removes that whole class of risk.
_build_delim_payload() {
  local ord="$1" transcript_path="$2" agent_id="$3" session_id="$4"
  python3 -c "
import json, sys
ordv = int(sys.argv[1])
ch = chr(ordv)
tick = chr(96) * 3
envelope = json.dumps({'agent': 'executor', 'verdict': 'done', 'files_touched': ['a' + ch + 'b']})
lam = '<!-- AGENT_OUTPUT -->' + chr(10) + tick + 'json' + chr(10) + envelope + chr(10) + tick + chr(10) + '<!-- /AGENT_OUTPUT -->'
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': sys.argv[4],
    'transcript_path': sys.argv[2],
    'cwd': '/tmp/test-worktree',
    'agent_id': sys.argv[3],
    'agent_type': 'executor',
    'last_assistant_message': lam,
}))
" "$ord" "$transcript_path" "$agent_id" "$session_id"
}

# ── Item 1: three tool_use blocks -> count_tool_uses returns 3 ───────────────
echo "Item 1: count_tool_uses() on a transcript with 3 tool_use blocks returns 3"
{
  TMP=$(mktemp -d)
  TRANSCRIPT="$TMP/three.jsonl"
  python3 -c "
import json
rows = [
    {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'name': 'Bash'}, {'type': 'text', 'text': 'ok'}]}},
    {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'name': 'Read'}]}},
    {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'name': 'Edit'}]}},
]
with open('$TRANSCRIPT', 'w') as f:
    for r in rows:
        f.write(json.dumps(r) + chr(10))
"
  RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import count_tool_uses
print(count_tool_uses('$TRANSCRIPT'))
")
  [[ "$RESULT" == "3" ]] && _pass "count_tool_uses returned 3" || _fail "expected 3, got '$RESULT'"
  rm -rf "$TMP"
}

# ── Item 2: parseable transcript, zero tool_use blocks -> returns 0, not None ─
echo "Item 2: count_tool_uses() on a parseable zero-tool_use transcript returns 0"
{
  TMP=$(mktemp -d)
  TRANSCRIPT="$TMP/zero.jsonl"
  python3 -c "
import json
row = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
    {'type': 'text', 'text': 'No tool calls here.'}]}}
with open('$TRANSCRIPT', 'w') as f:
    f.write(json.dumps(row) + chr(10))
"
  RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import count_tool_uses
r = count_tool_uses('$TRANSCRIPT')
print(repr(r))
")
  [[ "$RESULT" == "0" ]] && _pass "count_tool_uses returned 0 (not None)" || _fail "expected 0, got '$RESULT'"
  rm -rf "$TMP"
}

# ── Item 3: absent / unreadable / unparseable path -> None ───────────────────
echo "Item 3: count_tool_uses() on an absent or unparseable path returns None"
{
  RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import count_tool_uses
print(repr(count_tool_uses('/tmp/d1791-does-not-exist-xyz.jsonl')))
")
  [[ "$RESULT" == "None" ]] && _pass "nonexistent path returns None" || _fail "expected None, got '$RESULT'"

  TMP=$(mktemp -d)
  GARBAGE="$TMP/garbage.jsonl"
  printf 'not json\n{also not json\n' > "$GARBAGE"
  RESULT2=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import count_tool_uses
print(repr(count_tool_uses('$GARBAGE')))
")
  [[ "$RESULT2" == "None" ]] && _pass "wholly unparseable file returns None" || _fail "expected None, got '$RESULT2'"
  rm -rf "$TMP"
}

# ── Item 4: own_transcript_path=="" -> tool_uses empty in resolve(), and ─────
# agent_run.tool_uses is written NULL end-to-end — never 0.
echo "Item 4: unresolvable own_transcript_path yields empty tool_uses, NULL in agent_run"
{
  # 4a. resolve() directly: no agent_id/session_id at all -> own_transcript_path
  # resolves to "" -> tool_uses must be None, not 0.
  RESOLVED=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import resolve
out = resolve({'session_id': 'sess-noagent', 'transcript_path': ''}, '$REPO_ROOT')
print(repr(out['own_transcript_path']), repr(out['tool_uses']))
")
  [[ "$RESOLVED" == "'' None" ]] && _pass "resolve(): own_transcript_path=='' -> tool_uses is None" || _fail "resolve() mismatch: $RESOLVED"

  # 4b. Through the hook (dry-run): no agent_id -> own_transcript_path can't
  # resolve -> the flattened TOOL_USES field must be empty, so --tool-uses is
  # never passed to post-agent-hook.sh.
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  LAM='<!-- AGENT_OUTPUT -->
```json
{"agent": "executor", "verdict": "done"}
```
<!-- /AGENT_OUTPUT -->'
  PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-item4',
    'cwd': '/tmp/test-worktree',
    'last_assistant_message': sys.argv[1],
}))
" "$LAM")
  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  if [[ -f "$ARGS_FILE" ]]; then
    HAS_KEY=$(python3 -c "
import json
d = json.load(open('$ARGS_FILE'))
print('tool_uses' in d)
")
    [[ "$HAS_KEY" == "False" ]] && _pass "no --tool-uses forwarded when unresolved (never coerced to 0)" || _fail "tool_uses key unexpectedly present: $(cat "$ARGS_FILE")"
  else
    _fail "args file not created"
  fi
  rm -rf "$TMP"

  # 4c. End to end through agent_run_tracker.complete: omitting --tool-uses
  # must leave the column NULL, never 0.
  AGENT_ID="d1791-tooluse-null-$$"
  python3 "$TRACKER" complete --agent-id "$AGENT_ID" --verdict done \
    --input-tokens 10 --output-tokens 5 --role executor --discussion 1791 \
    > /dev/null 2>&1
  DB_VAL=$(python3 -c "
import duckdb, sys
sys.path.insert(0, '$REPO_ROOT')
from backend import state_paths
conn = duckdb.connect(str(state_paths.STATS_DB))
row = conn.execute('SELECT tool_uses FROM agent_run WHERE agent_id = ?', ['$AGENT_ID']).fetchone()
print(repr(row[0] if row else 'NO_ROW'))
conn.close()
")
  [[ "$DB_VAL" == "None" ]] && _pass "agent_run.tool_uses is NULL when --tool-uses was never passed" || _fail "expected None, got $DB_VAL"

  # And the contrast case: a genuine zero must be stored as 0, not NULL —
  # this is what keeps the tri-state meaningful end to end.
  AGENT_ID2="d1791-tooluse-zero-$$"
  python3 "$TRACKER" complete --agent-id "$AGENT_ID2" --verdict done \
    --input-tokens 10 --output-tokens 5 --role executor --discussion 1791 \
    --tool-uses 0 > /dev/null 2>&1
  DB_VAL2=$(python3 -c "
import duckdb, sys
sys.path.insert(0, '$REPO_ROOT')
from backend import state_paths
conn = duckdb.connect(str(state_paths.STATS_DB))
row = conn.execute('SELECT tool_uses FROM agent_run WHERE agent_id = ?', ['$AGENT_ID2']).fetchone()
print(repr(row[0] if row else 'NO_ROW'))
conn.close()
")
  [[ "$DB_VAL2" == "0" ]] && _pass "agent_run.tool_uses is 0 when --tool-uses 0 was explicitly passed (0 != NULL)" || _fail "expected 0, got $DB_VAL2"
}

# ── Item 5: tool_uses is threaded from subagent-stop-hook.sh to ──────────────
# post-agent-hook.sh's _CR_ARGS.
echo "Item 5: tool_uses appears in FIELD_LIST and is forwarded as --tool-uses"
{
  HITS_STOP=$(grep -c "tool.uses" "$REPO_ROOT/scripts/subagent-stop-hook.sh")
  HITS_POST=$(grep -c "tool.uses" "$REPO_ROOT/scripts/post-agent-hook.sh")
  [[ "$HITS_STOP" -gt 0 ]] && _pass "scripts/subagent-stop-hook.sh references tool_uses ($HITS_STOP hits)" || _fail "no tool_uses reference in subagent-stop-hook.sh"
  [[ "$HITS_POST" -gt 0 ]] && _pass "scripts/post-agent-hook.sh references tool_uses ($HITS_POST hits)" || _fail "no tool_uses reference in post-agent-hook.sh"
}

# ── Item 6: agent_run.tool_uses column exists after migration, idempotent ────
echo "Item 6: agent_run gets a tool_uses column, and re-running the migration is a no-op"
{
  python3 -c "
import duckdb, sys
sys.path.insert(0, '$REPO_ROOT')
from backend import state_paths
from backend.agent_run_tracker import _ensure_schema
conn = duckdb.connect(str(state_paths.STATS_DB))
_ensure_schema(conn)
cols = {r[0] for r in conn.execute(
    \"SELECT column_name FROM information_schema.columns WHERE table_name='agent_run'\"
).fetchall()}
assert 'tool_uses' in cols, f'tool_uses missing from columns: {cols}'
# Second call must be a no-op (idempotent) — no exception, column count unchanged.
before = len(cols)
_ensure_schema(conn)
cols2 = {r[0] for r in conn.execute(
    \"SELECT column_name FROM information_schema.columns WHERE table_name='agent_run'\"
).fetchall()}
assert len(cols2) == before, f'column count changed on second _ensure_schema call: {before} -> {len(cols2)}'
conn.close()
print('OK')
"
  [[ $? -eq 0 ]] && _pass "tool_uses column present and migration is idempotent" || _fail "migration check failed"
}

# ── Item 7: a newline embedded in an agent-controlled field (files_touched) ──
# must not truncate the \x1f-delimited record. Before the fmt() fix, `read`
# (a single-line here-string consumer) stopped at the embedded newline: every
# field after the one containing it — including tool_uses, at the END of the
# list — silently went empty, while fields BEFORE it (verdict) survived.
# -- Item 7: a newline embedded in an agent-controlled field (files_touched) --
# must not truncate the \x1f-delimited record. Before the fmt() fix, `read`
# (a single-line here-string consumer) stopped at the embedded newline: every
# field after the one containing it -- including tool_uses, at the END of the
# list -- silently went empty, while fields BEFORE it (verdict) survived.
echo "Item 7: newline in files_touched must not truncate tool_uses out of the record"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  AGENT_ID="agentitem7nlxyz1"

  # Own transcript with a KNOWN real tool-use count (2) -- the ground truth
  # this test checks survives the transport intact.
  OWN_DIR="$TMP/tasks"
  mkdir -p "$OWN_DIR"
  OWN_TRANSCRIPT="$OWN_DIR/${AGENT_ID}.jsonl"
  python3 -c "
import json
rows = [
    {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Bash'}]}},
    {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Read'}]}},
]
with open('$OWN_TRANSCRIPT', 'w') as f:
    for r in rows:
        f.write(json.dumps(r) + chr(10))
"
  TRANSCRIPT="$TMP/transcript.jsonl"
  echo '{}' > "$TRANSCRIPT"

  # ord 10 = newline. Built via _build_delim_payload (see its definition
  # above for why this must be constructed at the Python level from a plain
  # ordinal rather than typed as an escape sequence in this file's source).
  PAYLOAD=$(_build_delim_payload 10 "$TRANSCRIPT" "$AGENT_ID" "sess-item7nl")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("verdict") == "done", f"verdict did not survive: {d}"
assert str(d.get("tool_uses")) == "2", f"tool_uses lost/corrupted by embedded newline: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "verdict survives AND tool_uses resolves to the real count (2), not lost to truncation" || _fail "newline truncation defeated tool_uses: $(cat "$ARGS_FILE")"
  else
    _fail "args file not created"
  fi
  rm -rf "$TMP"
}

# -- Item 8: the record delimiter itself (0x1F) embedded in the same field --
# must not desynchronize every field after it. Before the fmt() fix, the
# extra field boundary shifted garbage into TOOL_USES (the last-position
# variable absorbs any overflow), which -- forwarded live -- would abort
# `agent_run_tracker.py complete` entirely via argparse's `type=int`,
# suppressing the run's whole completion record, not just tool_uses.
echo "Item 8: 0x1F in files_touched must not desynchronize tool_uses or abort complete_run"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  AGENT_ID="agentitem8x1fxyz"

  OWN_DIR="$TMP/tasks"
  mkdir -p "$OWN_DIR"
  OWN_TRANSCRIPT="$OWN_DIR/${AGENT_ID}.jsonl"
  python3 -c "
import json
rows = [
    {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Bash'}]}},
    {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Read'}]}},
]
with open('$OWN_TRANSCRIPT', 'w') as f:
    for r in rows:
        f.write(json.dumps(r) + chr(10))
"
  TRANSCRIPT="$TMP/transcript.jsonl"
  echo '{}' > "$TRANSCRIPT"

  # ord 31 = the 0x1F record-delimiter byte itself.
  PAYLOAD=$(_build_delim_payload 31 "$TRANSCRIPT" "$AGENT_ID" "sess-item8x1f")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    TOOL_USES_VAL=$(python3 -c "
import json
d = json.load(open('$ARGS_FILE'))
print(d.get('tool_uses', ''))
")
    python3 -c "
import sys
v = sys.argv[1]
assert v == '' or v.isdigit(), f'tool_uses value is not a clean int: {v!r}'
" "$TOOL_USES_VAL"
    if [[ $? -eq 0 ]]; then
      _pass "tool_uses value is clean ('${TOOL_USES_VAL}'), no embedded delimiter shifted into it"
    else
      _fail "tool_uses value is corrupted (would abort argparse downstream): $(python3 -c "import json; print(repr(json.load(open('$ARGS_FILE')).get('tool_uses')))")"
    fi

    # Prove the concrete downstream consequence directly: feed whatever the
    # hook resolved to the real CLI and confirm it does not abort.
    AGENT_ID_DB="d1791-item8-$$"
    TRACKER_ARGS=(complete --agent-id "$AGENT_ID_DB" --verdict done \
      --input-tokens 1 --output-tokens 1 --role executor --discussion 1791)
    [[ -n "$TOOL_USES_VAL" ]] && TRACKER_ARGS+=(--tool-uses "$TOOL_USES_VAL")
    python3 "$TRACKER" "${TRACKER_ARGS[@]}" > /dev/null 2>&1
    TRACKER_RC=$?
    [[ $TRACKER_RC -eq 0 ]] && _pass "feeding the resolved value to the real tracker CLI does not abort complete_run" || _fail "tracker CLI aborted (exit $TRACKER_RC) on tool_uses='${TOOL_USES_VAL}' -- this is the reported telemetry-suppression failure mode"

    if [[ $TRACKER_RC -eq 0 ]]; then
      DB_VAL=$(python3 -c "
import duckdb, sys
sys.path.insert(0, '$REPO_ROOT')
from backend import state_paths
conn = duckdb.connect(str(state_paths.STATS_DB))
row = conn.execute('SELECT tool_uses FROM agent_run WHERE agent_id = ?', ['$AGENT_ID_DB']).fetchone()
print(repr(row[0] if row else 'NO_ROW'))
conn.close()
")
      [[ "$DB_VAL" == "2" ]] && _pass "agent_run.tool_uses lands as the real count (2), not corrupted" || _fail "expected 2, got $DB_VAL"
    fi
  else
    _fail "args file not created"
  fi
  rm -rf "$TMP"
}

# ── Item 9: a JSONL line that parses as valid JSON but is not an object ──────
# (null / a bare number / a list / a string) must not crash count_tool_uses —
# obj.get(...) on any of these raises AttributeError, and resolve() calls
# count_tool_uses unconditionally with no surrounding try/except, so this one
# line previously took out the ENTIRE payload (role/verdict/tokens), not just
# tool_uses.
echo "Item 9: non-dict JSON lines (null, number, list, string) return None, never crash"
{
  TMP=$(mktemp -d)
  for SHAPE in null 42 '[1,2,3]' '"just a string"'; do
    TRANSCRIPT="$TMP/nondict-$(echo "$SHAPE" | tr -dc 'a-zA-Z0-9').jsonl"
    printf '%s\n' "$SHAPE" > "$TRANSCRIPT"
    RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import count_tool_uses
print(repr(count_tool_uses('$TRANSCRIPT')))
" 2>&1)
    RC=$?
    if [[ $RC -ne 0 ]]; then
      _fail "count_tool_uses crashed on shape '$SHAPE' (exit $RC): $RESULT"
    elif [[ "$RESULT" == "None" ]]; then
      _pass "shape '$SHAPE' returns None, no crash"
    else
      _fail "shape '$SHAPE' expected None, got '$RESULT'"
    fi
  done

  # And resolve() itself, end to end: a non-dict line in the own transcript
  # must not take out role/verdict/tokens either.
  AGENT_ID="agentitem9crashxyz"
  OWN_DIR="$TMP/tasks"
  mkdir -p "$OWN_DIR"
  printf 'null\n' > "$OWN_DIR/${AGENT_ID}.jsonl"
  RESOLVED=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import resolve
out = resolve({
    'session_id': 'sess-item9',
    'transcript_path': '',
    'agent_id': '$AGENT_ID',
    'agent_type': 'executor',
    'last_assistant_message': '<!-- AGENT_OUTPUT -->\n\`\`\`json\n{\"agent\": \"executor\", \"verdict\": \"done\"}\n\`\`\`\n<!-- /AGENT_OUTPUT -->',
}, '$TMP')
print(out['role'], out['verdict'], repr(out['tool_uses']))
" 2>&1)
  RC=$?
  if [[ $RC -ne 0 ]]; then
    _fail "resolve() crashed with a non-dict line in the own transcript: $RESOLVED"
  elif [[ "$RESOLVED" == "executor done None" ]]; then
    _pass "resolve() survives a non-dict transcript line: role/verdict intact, tool_uses correctly None"
  else
    _fail "resolve() output wrong: $RESOLVED"
  fi
  rm -rf "$TMP"
}

# ── Item 10: schema drift (valid JSON dicts, but no recognizable role/ ───────
# content shape) must fail toward None, not a silent, confident 0 — the
# accusatory direction a fabrication check must never take on unmeasured input.
echo "Item 10: valid-JSON-wrong-shape lines (schema drift) return None, not 0"
{
  TMP=$(mktemp -d)
  TRANSCRIPT="$TMP/drift.jsonl"
  python3 -c "
import json
rows = [{'foo': 'bar'}, {'baz': 123}]
with open('$TRANSCRIPT', 'w') as f:
    for r in rows:
        f.write(json.dumps(r) + chr(10))
"
  RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import count_tool_uses
print(repr(count_tool_uses('$TRANSCRIPT')))
")
  [[ "$RESULT" == "None" ]] && _pass "fully-drifted schema returns None (never a guessed 0)" || _fail "expected None, got '$RESULT'"

  # Contrast: a real (if degenerate) transcript with only recognized
  # non-assistant turns is a genuine, recognized zero — must stay 0.
  TRANSCRIPT2="$TMP/user-only.jsonl"
  python3 -c "
import json
row = {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}}
with open('$TRANSCRIPT2', 'w') as f:
    f.write(json.dumps(row) + chr(10))
"
  RESULT2=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import count_tool_uses
print(repr(count_tool_uses('$TRANSCRIPT2')))
")
  [[ "$RESULT2" == "0" ]] && _pass "a recognized shape with zero assistant turns is a genuine 0, not None" || _fail "expected 0, got '$RESULT2'"
  rm -rf "$TMP"
}

# ── Item 11: find_own_transcript prefers an exact filename match over a ──────
# mere prefix match — plain lexicographic sort put a hyphenated sibling
# before the exact file ('-' < '.' in ASCII), so a counter measuring ONE
# specific agent could silently read a different agent's transcript.
echo "Item 11: find_own_transcript prefers an exact match over a prefix-matching sibling"
{
  TMP=$(mktemp -d)
  TASKS_DIR="$TMP/tasks"
  mkdir -p "$TASKS_DIR"
  AGENT_ID="abc"
  touch "$TASKS_DIR/${AGENT_ID}-x.jsonl"
  touch "$TASKS_DIR/${AGENT_ID}.jsonl"
  TRANSCRIPT="$TMP/transcript.jsonl"
  touch "$TRANSCRIPT"

  PICKED=$(python3 -c "
import sys, os
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
from subagent_payload import find_own_transcript
result = find_own_transcript('$TRANSCRIPT', '$AGENT_ID', '', '')
print(os.path.basename(result))
")
  [[ "$PICKED" == "${AGENT_ID}.jsonl" ]] && _pass "picked the exact match (${AGENT_ID}.jsonl), not the prefix-matching sibling" || _fail "picked '$PICKED' instead of the exact match"
  rm -rf "$TMP"
}

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
