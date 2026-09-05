#!/usr/bin/env bash
# tests/smoke-3spawn-d984.sh — Synthetic 3-spawn smoke test for D#984 fix.
#
# Simulates SubagentStop hook firing for 3 concurrent subagents:
#   1. executor with clean envelope + hook_event_id in transcript
#   2. code-reviewer with envelope + hook_event_id in transcript
#   3. unknown agent — no envelope, no hook_event_id (legacy/prose-only)
#
# Assertions:
#   - 3 distinct event-ids (Bug B fix)
#   - roles correctly resolved as executor, code-reviewer, unknown (Bug A fix)
#   - all 3 hook invocations exit 0
#
# Usage: bash tests/smoke-3spawn-d984.sh
# Exit 0 = all assertions passed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/scripts/subagent-stop-hook.sh"
TMP=$(mktemp -d)
SESS="synth-test-sess-d984abc"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

run_hook() {
  local transcript_path="$1"
  local session_id="$2"
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
  SUBAGENT_STOP_DRY_RUN=1 SUBAGENT_STOP_ARGS_FILE="$args_file" \
    bash "$HOOK" <<< "$stdin_json"
}

# --- Subagent 1: executor with clean envelope + hook_event_id ---
# NOTE (D#1807): the tag prefix below is split into adjacent string literals
# so this fixture-generating source line never carries a canonical-shaped id
# immediately after "hook_event_id=" — otherwise any agent reading this file
# would adopt the example id as its own. Python concatenates adjacent
# literals at parse time, so the JSONL this writes is byte-identical either
# way.
T1="$TMP/t1.jsonl"
python3 - "$T1" <<'PYEOF'
import json, sys
rows = [
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "Implement fix.\n\n" "hook_event_" "id=executor-984-1715800001"}
    ]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Done.\n\n<!-- AGENT_OUTPUT -->\n```json\n{\"agent\": \"executor\", \"discussion\": 984, \"pr\": 985, \"verdict\": \"done\"}\n```\n<!-- /AGENT_OUTPUT -->"}
    ]}}
]
with open(sys.argv[1], "w") as f:
    for r in rows: f.write(json.dumps(r) + "\n")
PYEOF

# --- Subagent 2: code-reviewer with envelope + hook_event_id ---
T2="$TMP/t2.jsonl"
python3 - "$T2" <<'PYEOF'
import json, sys
rows = [
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "Review PR.\n\n" "hook_event_" "id=code-reviewer-984-1715800002"}
    ]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "LGTM.\n\n<!-- AGENT_OUTPUT -->\n```json\n{\"agent\": \"code-reviewer\", \"discussion\": 984, \"verdict\": \"pass\"}\n```\n<!-- /AGENT_OUTPUT -->"}
    ]}}
]
with open(sys.argv[1], "w") as f:
    for r in rows: f.write(json.dumps(r) + "\n")
PYEOF

# --- Subagent 3: no envelope, no hook_event_id (legacy prose-only agent) ---
T3="$TMP/t3.jsonl"
python3 - "$T3" <<'PYEOF'
import json, sys
with open(sys.argv[1], "w") as f:
    f.write(json.dumps({"role": "assistant", "content": "Just prose. No structured output at all."}) + "\n")
PYEOF

A1="$TMP/a1.json"
A2="$TMP/a2.json"
A3="$TMP/a3.json"

echo "--- Running 3 hook invocations ---"
run_hook "$T1" "$SESS" "$A1"; RC1=$?
run_hook "$T2" "$SESS" "$A2"; RC2=$?
run_hook "$T3" "$SESS" "$A3"; RC3=$?

echo ""
echo "--- Exit codes ---"
[[ $RC1 -eq 0 ]] && _pass "subagent 1 exit 0" || _fail "subagent 1 exit $RC1"
[[ $RC2 -eq 0 ]] && _pass "subagent 2 exit 0" || _fail "subagent 2 exit $RC2"
[[ $RC3 -eq 0 ]] && _pass "subagent 3 exit 0" || _fail "subagent 3 exit $RC3"

echo ""
echo "--- Role resolution ---"
python3 - "$A1" "$A2" "$A3" <<'PYCHECK'
import json, sys

a1, a2, a3 = [json.load(open(f)) for f in sys.argv[1:4]]

# Bug A fix: roles from hook_event_id extraction
assert a1.get("role") == "executor",      f"Expected executor, got: {a1.get('role')!r}"
assert a2.get("role") == "code-reviewer", f"Expected code-reviewer, got: {a2.get('role')!r}"
assert a3.get("role") == "unknown",       f"Expected unknown (no hook_event_id), got: {a3.get('role')!r}"

# Bug B fix: all 3 event-ids are distinct
eids = [a.get("event_id") for a in [a1, a2, a3]]
assert len(set(eids)) == 3, f"Expected 3 distinct event-ids, got collisions: {eids}"

# Subagents with hook_event_id use canonical event-id (no nanos suffix)
assert a1.get("event_id") == "executor-984-1715800001", f"a1 event_id: {a1.get('event_id')!r}"
assert a2.get("event_id") == "code-reviewer-984-1715800002", f"a2 event_id: {a2.get('event_id')!r}"

# Legacy subagent (no hook_event_id) gets fallback with nanos suffix
eid3 = a3.get("event_id", "")
assert eid3.startswith("unknown-0-synth-test-sess-d984abc-"), \
    f"a3 event_id should start with 'unknown-0-synth-test-sess-d984abc-', got: {eid3!r}"

print("All role and event-id assertions passed")
PYCHECK
[[ $? -eq 0 ]] && _pass "role resolution and distinct event-ids (Bug A + Bug B fix)" || _fail "assertion failure"

echo ""
echo "--- Summary ---"
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
