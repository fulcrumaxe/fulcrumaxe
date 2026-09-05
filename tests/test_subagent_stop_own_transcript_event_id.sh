#!/usr/bin/env bash
# tests/test_subagent_stop_own_transcript_event_id.sh — D#2247 regression.
#
# D#2238 wired real token capture, but the completion-side event id and the
# spawn-side event id never met: scripts/subagent-stop-hook.sh fed the
# PARENT session's transcript (transcript_path) to
# scripts/lib/transcript_event_id.py, and the hook_event_id tag never
# appears there — it only appears in the SUBAGENT's own transcript. The
# fallback formula ("{role}-{disc}-{agent_id}") the hook falls back to can
# never match the row spawn-agent.sh's start_run() already wrote, so every
# completion landed on a new orphan-unmatched row instead of updating the
# existing one.
#
# CRITICAL fixture shape: the parent transcript carries NO hook_event_id
# tag, and the subagent's OWN transcript (found via
# scripts/lib/subagent_payload.py's find_own_transcript(), same
# tasks/<agent_id>* candidate find_own_usage already searches) carries one.
# A fixture that puts the tag in the parent transcript instead would pass
# against today's (pre-fix) code and prove nothing — this is the exact
# mistake the Discussion calls out.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/scripts/subagent-stop-hook.sh"

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

# ── Case 1: parent-without-tag / own-with-tag — hook must use the spawn id ──
echo "Case 1: parent transcript has no tag, own transcript does — hook must emit the spawn id"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  AGENT_ID="a5a272b8344ec3a2c"
  SPAWN_TAG_ID="code-reviewer-2235-1788405748"

  # Parent transcript: transcript_path as Claude Code sends it. Prose only,
  # zero canonical hook_event_id occurrences — this is the measured D#2247
  # baseline (0 matches in the parent transcript for a real production
  # capture).
  TRANSCRIPT="$TMP/transcript.jsonl"
  python3 - "$TRANSCRIPT" <<'PYEOF'
import json, sys
path = sys.argv[1]
rows = [
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "Review PR #2245."}
    ]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Reviewed. No blocking issues."}
    ]}},
]
with open(path, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")
PYEOF

  # Own transcript: same candidate find_own_usage already searches —
  # <dirname(transcript_path)>/tasks/<agent_id>*. Carries the canonical tag
  # inside a tool_result block, matching how spawn-agent.sh's prompt
  # reference is actually read back by the agent (D#1784).
  OWN_DIR="$TMP/tasks"
  mkdir -p "$OWN_DIR"
  OWN_TRANSCRIPT="$OWN_DIR/${AGENT_ID}.jsonl"
  python3 - "$OWN_TRANSCRIPT" "$SPAWN_TAG_ID" <<'PYEOF'
import json, sys
path, tag_id = sys.argv[1], sys.argv[2]
rows = [
    # The tag prefix is split so this fixture file never itself contains a
    # canonical-shaped id that a reading agent would adopt as its own.
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": [
            {"type": "text", "text": "Your task: review PR #2245.\n\n" "hook_event_" "id=" + tag_id}
        ]}
    ]}},
    {"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Reviewed. No blocking issues."}],
        "usage": {"input_tokens": 128, "output_tokens": 10007,
                   "cache_read_input_tokens": 5011170,
                   "cache_creation_input_tokens": 0},
    }},
]
with open(path, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")
PYEOF

  PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-d2247',
    'transcript_path': sys.argv[1],
    'cwd': '/tmp/test-worktree',
    'agent_id': sys.argv[2],
    'agent_type': 'code-reviewer',
    'last_assistant_message': 'Reviewed. No blocking issues.',
}))
" "$TRANSCRIPT" "$AGENT_ID")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" "$SPAWN_TAG_ID" "$AGENT_ID" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
spawn_tag_id, agent_id = sys.argv[2], sys.argv[3]
fallback_shape = "code-reviewer-0-" + agent_id
assert d.get("event_id") == spawn_tag_id, (
    f"expected event_id={spawn_tag_id!r} (own-transcript spawn tag), got {d.get('event_id')!r}: {d}"
)
assert d.get("event_id") != fallback_shape, (
    f"event_id fell back to the never-matching {fallback_shape!r} shape: {d}"
)
PYCHECK
    if [[ $? -eq 0 ]]; then
      _pass "event_id is the own-transcript spawn tag ($SPAWN_TAG_ID), not the fallback shape"
    else
      _fail "event_id was not the own-transcript spawn tag"
    fi
  else
    _fail "args file not created"
  fi

  rm -rf "$TMP"
}

# ── Case 2: no own transcript exists — falls back safely, never errors ──────
echo "Case 2: own transcript absent — falls back to legacy formula, hook still exits 0"
{
  TMP=$(mktemp -d)
  ARGS_FILE="$TMP/args.json"
  AGENT_ID="b9c1d2e3f4a5b6c7d"

  TRANSCRIPT="$TMP/transcript.jsonl"
  python3 - "$TRANSCRIPT" <<'PYEOF'
import json, sys
path = sys.argv[1]
row = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "Done."}
]}}
with open(path, "w") as f:
    f.write(json.dumps(row) + "\n")
PYEOF
  # Deliberately no tasks/ dir and no ~/.claude/projects/... own transcript
  # for this agent_id — this is the pre-existing (still-supported) fallback
  # case: an agent not spawned via spawn-agent.sh, or an unreadable/missing
  # own transcript.

  PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'hook_event_name': 'SubagentStop',
    'session_id': 'sess-d2247-b',
    'transcript_path': sys.argv[1],
    'cwd': '/tmp/test-worktree',
    'agent_id': sys.argv[2],
    'agent_type': 'executor',
    'last_assistant_message': 'Done.',
}))
" "$TRANSCRIPT" "$AGENT_ID")

  _run_hook_dry "$PAYLOAD" "$ARGS_FILE"
  EXIT_CODE=$?
  [[ $EXIT_CODE -eq 0 ]] && _pass "exits 0 with no own transcript present" || _fail "expected exit 0, got $EXIT_CODE"

  if [[ -f "$ARGS_FILE" ]]; then
    python3 - "$ARGS_FILE" "$AGENT_ID" <<'PYCHECK'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
agent_id = sys.argv[2]
expected = "executor-0-" + agent_id
assert d.get("event_id") == expected, f"expected legacy fallback {expected!r}, got {d.get('event_id')!r}: {d}"
PYCHECK
    [[ $? -eq 0 ]] && _pass "falls back to legacy {role}-{disc}-{agent_id} formula when no own transcript exists" \
                   || _fail "fallback formula not produced"
  else
    _fail "args file not created"
  fi

  rm -rf "$TMP"
}

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
