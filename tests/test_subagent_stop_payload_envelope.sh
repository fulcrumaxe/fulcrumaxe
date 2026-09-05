#!/usr/bin/env bash
# tests/test_subagent_stop_payload_envelope.sh — D#2238 acceptance items
# 1-5, 7, 8, 11 for scripts/subagent-stop-hook.sh / scripts/lib/subagent_payload.py.
#
# Covers the parts of the Spec that tests/test_subagent_stop_hook.sh and
# tests/test_subagent_stop_hook_unknown_role.sh predate: reading agent_id /
# agent_type / last_assistant_message directly off the SubagentStop payload,
# instead of only ever hunting an envelope inside transcript_path (the
# PARENT session's transcript, which never carries a subagent's own turns).
#
# Item 6 (real captured payload, non-zero tokens) is NOT a suite case here --
# it depends on ephemeral, host-specific production artifacts (a live
# SubagentStop capture) rather than a repeatable fixture, and is verified
# separately as this PR's Gate 2 evidence. See the PR body.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/scripts/subagent-stop-hook.sh"

# D#2267: several items below assert on $REPO_ROOT/.autonomous-team/stats/
# unknown_subagent_stops-<date>.jsonl — the live tree every real agent's own
# unknown:unknown SubagentStop rows also land in, since the noise-gate write
# in scripts/subagent-stop-hook.sh runs unconditionally, before the
# SUBAGENT_STOP_DRY_RUN branch. SUBAGENT_STOP_REPO_ROOT_OVERRIDE (already a
# documented test-only seam in that script) redirects it to a per-run
# fixture instead.
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

# Build a SubagentStop-shaped stdin payload. Any field left empty/omitted by
# the caller is left out of the JSON entirely (matching item 1's "no
# transcript_path key at all" requirement) rather than emitted as null.
_make_payload() {
  python3 -c "
import json, sys
d = json.loads(sys.argv[1])
print(json.dumps(d))
" "$1"
}

_run_hook_dry() {
  local stdin_json="$1"
  local args_file="$2"
  SUBAGENT_STOP_DRY_RUN=1 \
  SUBAGENT_STOP_ARGS_FILE="$args_file" \
    bash "$HOOK" <<< "$stdin_json"
}

# ── Item 1: agent_id + agent_type + envelope in last_assistant_message, ────────
# no transcript_path key at all — role/verdict/discussion/pr come from the
# envelope, exit 0.
echo "Item 1: envelope in last_assistant_message, no transcript_path key"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"

  LAM='Work done.

<!-- AGENT_OUTPUT -->
```json
{
  "agent": "executor",
  "discussion": 2238,
  "pr": 2241,
  "verdict": "done",
  "files_touched": ["scripts/subagent-stop-hook.sh"]
}
```
<!-- /AGENT_OUTPUT -->'

  PAYLOAD=$(_make_payload "$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-item1',
    'cwd': '/tmp/test-worktree',
    'agent_id': 'agentitem1abc',
    'agent_type': 'executor',
    'last_assistant_message': sys.argv[1],
}))
" "$LAM")")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("role") == "executor", f"role wrong: {d}"
assert d.get("verdict") == "done", f"verdict wrong: {d}"
assert str(d.get("discussion")) == "2238", f"discussion wrong: {d}"
assert str(d.get("pr")) == "2241", f"pr wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "role/verdict/discussion/pr resolved from last_assistant_message" || _fail "field mismatch"
  else
    _fail "args file not created — payload had no transcript_path at all, hook must not require one"
  fi
  rm -rf "$TMP"
}

# ── Item 2: agent_type present, last_assistant_message has NO envelope ────────
# role must be the canonical agent_type, not "unknown"; no skip row written.
echo "Item 2: agent_type fallback role, no envelope in last_assistant_message"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  STATS_BEFORE=0
  STATS_FILE="$FIXTURE_ROOT/.autonomous-team/stats/unknown_subagent_stops-$(date -u +%F).jsonl"
  [[ -f "$STATS_FILE" ]] && STATS_BEFORE=$(wc -l < "$STATS_FILE")

  PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-item2',
    'cwd': '/tmp/test-worktree',
    'agent_id': 'agentitem2abc',
    'agent_type': 'code-reviewer',
    'last_assistant_message': 'Reviewed the diff. Looks fine, no notes.',
}))
")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    ROLE=$(python3 -c "import json; print(json.load(open('$ARGS_FILE')).get('role'))")
    [[ "$ROLE" == "code-reviewer" ]] && _pass "role is canonical agent_type, not unknown" || _fail "role was '$ROLE', expected code-reviewer"
  else
    _fail "args file not created — a real agent_type must not be treated as unknown:unknown"
  fi

  STATS_AFTER=0
  [[ -f "$STATS_FILE" ]] && STATS_AFTER=$(wc -l < "$STATS_FILE")
  [[ "$STATS_AFTER" -eq "$STATS_BEFORE" ]] && _pass "unknown_subagent_stops gained zero rows" || _fail "unknown_subagent_stops grew ($STATS_BEFORE -> $STATS_AFTER)"
  rm -rf "$TMP"
}

# ── Item 3: envelope 'agent' and payload 'agent_type' disagree ────────────────
# envelope 'agent' wins — agent_type is the fallback, not the winner.
echo "Item 3: envelope agent wins over disagreeing agent_type"
{
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
    'session_id': 'sess-item3',
    'cwd': '/tmp/test-worktree',
    'agent_id': 'agentitem3abc',
    'agent_type': 'code-reviewer',
    'last_assistant_message': sys.argv[1],
}))
" "$LAM")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"

  if [[ -f "$ARGS_FILE" ]]; then
    ROLE=$(python3 -c "import json; print(json.load(open('$ARGS_FILE')).get('role'))")
    [[ "$ROLE" == "executor" ]] && _pass "envelope's agent ('executor') wins over agent_type ('code-reviewer')" || _fail "role was '$ROLE', expected executor"
  else
    _fail "args file not created"
  fi
  rm -rf "$TMP"
}

# ── Item 4: two payloads, same session_id, different agent_id, no ─────────────
# recoverable hook_event_id — two distinct event_ids, each containing its own
# agent_id.
echo "Item 4: distinct event_ids for distinct agent_ids sharing one session_id"
{
  TMP=$(mktemp -d)
  ARGS_A="$TMP/args_a.json"
  ARGS_B="$TMP/args_b.json"

  PAYLOAD_A=$(python3 -c "
import json
print(json.dumps({
    'hook_event_name': 'SubagentStop', 'session_id': 'sess-shared',
    'cwd': '/tmp/test-worktree', 'agent_id': 'agentAAAA111',
    'agent_type': 'executor', 'last_assistant_message': 'done A',
}))
")
  PAYLOAD_B=$(python3 -c "
import json
print(json.dumps({
    'hook_event_name': 'SubagentStop', 'session_id': 'sess-shared',
    'cwd': '/tmp/test-worktree', 'agent_id': 'agentBBBB222',
    'agent_type': 'executor', 'last_assistant_message': 'done B',
}))
")

  _run_hook_dry "$PAYLOAD_A" "$ARGS_A"
  _run_hook_dry "$PAYLOAD_B" "$ARGS_B"

  if [[ -f "$ARGS_A" && -f "$ARGS_B" ]]; then
    EVID_A=$(python3 -c "import json; print(json.load(open('$ARGS_A')).get('event_id'))")
    EVID_B=$(python3 -c "import json; print(json.load(open('$ARGS_B')).get('event_id'))")
    if [[ "$EVID_A" != "$EVID_B" ]]; then
      _pass "event_ids differ ($EVID_A vs $EVID_B)"
    else
      _fail "event_ids collided: $EVID_A"
    fi
    [[ "$EVID_A" == *"agentAAAA111"* ]] && _pass "event_id A contains its own agent_id" || _fail "event_id A ($EVID_A) missing agent_id"
    [[ "$EVID_B" == *"agentBBBB222"* ]] && _pass "event_id B contains its own agent_id" || _fail "event_id B ($EVID_B) missing agent_id"
  else
    _fail "one or both args files not created (A exists=$( [[ -f "$ARGS_A" ]] && echo yes || echo no ), B exists=$( [[ -f "$ARGS_B" ]] && echo yes || echo no ))"
  fi
  rm -rf "$TMP"
}

# ── Item 5: token misattribution guard ─────────────────────────────────────────
# Envelope tokens_used {input:100, output:50} AND a transcript_path whose last
# assistant message carries usage {input_tokens:999999, output_tokens:888888}
# — the parent transcript's usage must NEVER reach the row.
echo "Item 5: parent transcript usage never overrides envelope tokens_used"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  TRANSCRIPT="$TMP/parent_transcript.jsonl"

  # This transcript stands in for the PARENT session transcript: a real shape,
  # with a huge usage figure that must never be attributed to the subagent.
  python3 -c "
import json
row = {
    'type': 'assistant',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': 'Some unrelated parent-session turn.'}],
        'usage': {'input_tokens': 999999, 'output_tokens': 888888},
    },
}
print(json.dumps(row))
" > "$TRANSCRIPT"

  LAM='<!-- AGENT_OUTPUT -->
```json
{"agent": "executor", "verdict": "done", "tokens_used": {"input": 100, "output": 50}}
```
<!-- /AGENT_OUTPUT -->'

  PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-item5',
    'transcript_path': sys.argv[1],
    'cwd': '/tmp/test-worktree',
    'agent_id': 'agentitem5abc',
    'agent_type': 'executor',
    'last_assistant_message': sys.argv[2],
}))
" "$TRANSCRIPT" "$LAM")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("input_tokens") == 100, f"input_tokens leaked parent usage: {d}"
assert d.get("output_tokens") == 50, f"output_tokens leaked parent usage: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "tokens are the envelope's (100/50), not the parent transcript's (999999/888888)" || _fail "misattribution guard failed"
  else
    _fail "args file not created"
  fi
  rm -rf "$TMP"
}

# ── Item 7: tokens_used {} in envelope, no resolvable subagent transcript ─────
# args file still carries the real role/verdict with tokens 0, and no skip row.
echo "Item 7: empty tokens_used does not re-trigger the unknown:unknown skip"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  STATS_FILE="$FIXTURE_ROOT/.autonomous-team/stats/unknown_subagent_stops-$(date -u +%F).jsonl"
  STATS_BEFORE=0
  [[ -f "$STATS_FILE" ]] && STATS_BEFORE=$(wc -l < "$STATS_FILE")

  LAM='<!-- AGENT_OUTPUT -->
```json
{"agent": "executor", "verdict": "done", "tokens_used": {}}
```
<!-- /AGENT_OUTPUT -->'

  PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-item7',
    'cwd': '/tmp/test-worktree',
    'agent_id': 'agentitem7abc',
    'agent_type': 'executor',
    'last_assistant_message': sys.argv[1],
}))
" "$LAM")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("role") == "executor", f"role wrong: {d}"
assert d.get("verdict") == "done", f"verdict wrong: {d}"
assert d.get("input_tokens") == 0, f"input_tokens wrong: {d}"
assert d.get("output_tokens") == 0, f"output_tokens wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "real role/verdict kept with tokens 0" || _fail "field mismatch"
  else
    _fail "args file not created — a real envelope with empty tokens_used must still write a row"
  fi

  STATS_AFTER=0
  [[ -f "$STATS_FILE" ]] && STATS_AFTER=$(wc -l < "$STATS_FILE")
  [[ "$STATS_AFTER" -eq "$STATS_BEFORE" ]] && _pass "no new skip row" || _fail "unexpected skip row written"
  rm -rf "$TMP"
}

# ── Item 8: skip rows are diagnosable — always carry an agent_id field ────────
echo "Item 8: unknown_subagent_stops rows always carry agent_id (empty when absent)"
{
  STATS_FILE="$FIXTURE_ROOT/.autonomous-team/stats/unknown_subagent_stops-$(date -u +%F).jsonl"

  PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-item8-noagentid',
    'cwd': '/tmp/test-worktree',
}))
")
  # Live path (not dry-run): a genuine unknown:unknown payload writes to the
  # real stats file under REPO_ROOT, same as tests/test_subagent_stop_hook_unknown_role.sh's
  # AC-1 pattern.
  bash "$HOOK" <<< "$PAYLOAD" > /dev/null 2>&1

  if [[ -f "$STATS_FILE" ]]; then
    LAST_ROW=$(grep '"session_id": "sess-item8-noagentid"' "$STATS_FILE" | tail -1)
    if [[ -n "$LAST_ROW" ]]; then
      echo "$LAST_ROW" | python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
assert 'agent_id' in d, f'agent_id key missing: {d}'
assert d['agent_id'] == '', f'expected empty agent_id, got: {d}'
"
      [[ $? -eq 0 ]] && _pass "skip row carries agent_id (empty string)" || _fail "skip row agent_id field wrong"
    else
      _fail "no matching row found in $STATS_FILE"
    fi
  else
    _fail "stats file not created: $STATS_FILE"
  fi
}

# ── Item 11: path-traversal guard on agent_id ─────────────────────────────────
echo "Item 11: agent_id path traversal is neutralized"
{
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
    'session_id': 'sess-item11',
    'cwd': '/tmp/test-worktree',
    'agent_id': '../../etc/passwd',
    'agent_type': 'executor',
    'last_assistant_message': sys.argv[1],
}))
" "$LAM")

  # Snapshot anything outside .autonomous-team/ that might be affected —
  # specifically /tmp/etc (a sentinel a real traversal into /etc would need
  # to have created, since we can't write to real /etc from a test).
  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    EVID=$(python3 -c "import json; print(json.load(open('$ARGS_FILE')).get('event_id'))")
    if [[ "$EVID" == *".."* || "$EVID" == *"/etc/"* ]]; then
      _fail "malicious agent_id leaked into event_id: $EVID"
    else
      _pass "malicious agent_id did not leak into event_id ($EVID)"
    fi
  else
    _fail "args file not created"
  fi

  # No file should have been written anywhere outside .autonomous-team/ as a
  # result of the malicious agent_id (find_own_usage must treat it as absent
  # before ever building a path from it).
  [[ ! -e "/etc/passwd.autonomous-team-test-marker" ]] && _pass "no file written outside .autonomous-team/" || _fail "traversal wrote outside .autonomous-team/"
  rm -rf "$TMP"
}

# ── Regression: existing envelope-only fixtures still work through the ────────
# rewritten hook (legacy path — no last_assistant_message in the payload at
# all, envelope embedded in the transcript instead).
echo "Regression: legacy transcript-only envelope (no last_assistant_message)"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  TRANSCRIPT="$TMP/transcript.jsonl"

  python3 -c "
import json
text = '''Done.

<!-- AGENT_OUTPUT -->
\`\`\`json
{\"agent\": \"executor\", \"verdict\": \"done\", \"discussion\": 7, \"pr\": 8}
\`\`\`
<!-- /AGENT_OUTPUT -->'''
row = {'role': 'assistant', 'content': text}
print(json.dumps(row))
" > "$TRANSCRIPT"

  PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-legacy',
    'transcript_path': sys.argv[1],
    'cwd': '/tmp/test-worktree',
}))
" "$TRANSCRIPT")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"

  if [[ -f "$ARGS_FILE" ]]; then
    ROLE=$(python3 -c "import json; print(json.load(open('$ARGS_FILE')).get('role'))")
    [[ "$ROLE" == "executor" ]] && _pass "legacy transcript-only envelope still parses" || _fail "role was '$ROLE'"
  else
    _fail "args file not created for legacy-shape payload"
  fi
  rm -rf "$TMP"
}

# ── Item 5b: last_assistant_message PRESENT but unparseable, transcript_path ──
# present and containing a RICH parent turn (real-looking role/verdict/pr/
# discussion/tokens). Per the code-review finding on PR #2244: falling back
# to the legacy transcript scan whenever the envelope fails to parse
# (instead of only when last_assistant_message is absent from the payload
# entirely) let the PARENT transcript's role/verdict/discussion/pr/tokens
# bleed onto the subagent's row whenever its own message was present but
# crashed/truncated/malformed. Unknown/zero is correct here; borrowed
# parent values are not.
echo "Item 5b: unparseable last_assistant_message must not borrow the parent transcript's fields"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  TRANSCRIPT="$TMP/parent_transcript.jsonl"

  # A rich, well-formed PARENT-session turn -- if this leaks through, it will
  # look like a perfectly good agent_run row, just for the wrong agent.
  python3 -c "
import json
row = {
    'type': 'assistant',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': '''Parent session turn.

<!-- AGENT_OUTPUT -->
\`\`\`json
{\"agent\": \"project-manager\", \"verdict\": \"pass\", \"discussion\": 9999, \"pr\": 8888, \"tokens_used\": {\"input\": 831000, \"output\": 50000}}
\`\`\`
<!-- /AGENT_OUTPUT -->'''}],
        'usage': {'input_tokens': 831000, 'output_tokens': 50000, 'cache_read_input_tokens': 831000},
    },
}
print(json.dumps(row))
" > "$TRANSCRIPT"

  # last_assistant_message IS present in the payload (the key exists) but its
  # value does not contain a well-formed envelope -- simulates a subagent
  # that crashed mid-message / emitted truncated markers.
  PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-item5b',
    'transcript_path': sys.argv[1],
    'cwd': '/tmp/test-worktree',
    'agent_id': 'agentitem5babc',
    'agent_type': 'executor',
    'last_assistant_message': 'Something went wrong mid-turn <!-- AGENT_OUTPUT --> BROKEN, no closing marker',
}))
" "$TRANSCRIPT")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("verdict") != "pass", f"borrowed parent verdict: {d}"
assert str(d.get("discussion")) != "9999", f"borrowed parent discussion: {d}"
assert str(d.get("pr")) != "8888", f"borrowed parent pr: {d}"
assert d.get("input_tokens") != 831000, f"borrowed parent input_tokens: {d}"
assert d.get("output_tokens") != 50000, f"borrowed parent output_tokens: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "no parent field bled through (verdict/discussion/pr/tokens all clean)" || _fail "parent field leaked"
  else
    _fail "args file not created — expected a row with agent_type fallback role, not silence"
  fi

  if [[ -f "$ARGS_FILE" ]]; then
    ROLE=$(python3 -c "import json; print(json.load(open('$ARGS_FILE')).get('role'))")
    [[ "$ROLE" == "executor" ]] && _pass "role falls back to agent_type ('executor'), not the parent's 'project-manager'" || _fail "role was '$ROLE'"
    INPUT_TOKENS=$(python3 -c "import json; print(json.load(open('$ARGS_FILE')).get('input_tokens'))")
    [[ "$INPUT_TOKENS" == "0" ]] && _pass "tokens are zero (no subagent-owned transcript on disk for this fake agent_id), not the parent's 831000" || _fail "input_tokens was '$INPUT_TOKENS', expected 0"
  fi
  rm -rf "$TMP"
}

# ── Item 5c: last_assistant_message ABSENT, transcript_path present ──────────
# Legacy path must still work exactly as before D#2238's payload-parsing
# change — this is the pre-existing behavior for old-style payloads/fixtures
# that never had last_assistant_message at all.
echo "Item 5c: last_assistant_message absent, legacy transcript-embedded envelope still resolves"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  TRANSCRIPT="$TMP/transcript.jsonl"

  python3 -c "
import json
text = '''Done.

<!-- AGENT_OUTPUT -->
\`\`\`json
{\"agent\": \"executor\", \"verdict\": \"done\", \"discussion\": 42, \"pr\": 43, \"tokens_used\": {\"input\": 500, \"output\": 200}}
\`\`\`
<!-- /AGENT_OUTPUT -->'''
row = {
    'type': 'assistant',
    'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]},
}
print(json.dumps(row))
" > "$TRANSCRIPT"

  # No 'last_assistant_message' key at all in this payload.
  PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-item5c',
    'transcript_path': sys.argv[1],
    'cwd': '/tmp/test-worktree',
}))
" "$TRANSCRIPT")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" <<'PYCHECK'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("role") == "executor", f"role wrong: {d}"
assert d.get("verdict") == "done", f"verdict wrong: {d}"
assert str(d.get("discussion")) == "42", f"discussion wrong: {d}"
assert str(d.get("pr")) == "43", f"pr wrong: {d}"
assert d.get("input_tokens") == 500, f"input_tokens wrong: {d}"
assert d.get("output_tokens") == 200, f"output_tokens wrong: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "legacy path (no last_assistant_message key) still resolves fully from transcript_path" || _fail "field mismatch"
  else
    _fail "args file not created for legacy no-LAM payload"
  fi
  rm -rf "$TMP"
}

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
