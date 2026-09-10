#!/usr/bin/env bash
# tests/smoke-3spawn-d984.sh — Synthetic 3-spawn smoke test for D#984 fix.
#
# Simulates SubagentStop hook firing for 3 concurrent subagents:
#   1. executor with clean envelope + hook_event_id in transcript
#   2. code-reviewer with envelope + hook_event_id in transcript
#   3. unknown agent — no envelope, no hook_event_id (legacy/prose-only)
#
# Assertions:
#   - subagents 1 and 2 resolve roles correctly (Bug A fix) and get distinct,
#     canonical event-ids (Bug B fix)
#   - subagent 3 hits section 3b's noise-drop path (unknown:unknown with no
#     spawn context): it never writes an args file, and the hook says so on
#     stderr. That is the hook choosing not to write, not a crash — this
#     suite used to assert the opposite (D#2168) and fail every run.
#   - all 3 hook invocations exit 0
#
# Usage: bash tests/smoke-3spawn-d984.sh
# Exit 0 = all assertions passed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/scripts/subagent-stop-hook.sh"
TMP=$(mktemp -d)
SESS="synth-test-sess-d984abc"

# D#2168 item 4: section 3b's counter-row write is repo-relative
# ($REPO_ROOT/.autonomous-team/stats/...). scripts/subagent-stop-hook.sh
# ships a test-only seam for exactly this (SUBAGENT_STOP_REPO_ROOT_OVERRIDE,
# already used by tests/test_subagent_stop_hook.sh and friends) — point it at
# this run's own scratch dir so the noise-drop row subagent 3 triggers below
# lands there instead of the real .autonomous-team/stats/.
export SUBAGENT_STOP_REPO_ROOT_OVERRIDE="$TMP"

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
  local stderr_file="${4:-}"
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
  if [[ -n "$stderr_file" ]]; then
    # Capture this invocation's own stderr so the noise-drop trace can be
    # asserted on below, while still echoing it so a human reading the run's
    # output sees the same thing the non-captured invocations show live.
    SUBAGENT_STOP_DRY_RUN=1 SUBAGENT_STOP_ARGS_FILE="$args_file" \
      bash "$HOOK" <<< "$stdin_json" 2>"$stderr_file"
    local rc=$?
    cat "$stderr_file" >&2
    return $rc
  fi
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
# This is the only fixture of this shape in the suite — do not give it a
# hook_event_id to "fix" the assertions below. That would turn it into a
# third copy of subagent 1 and delete the only coverage of the legacy
# prose-only path (D#2168).
T3="$TMP/t3.jsonl"
python3 - "$T3" <<'PYEOF'
import json, sys
with open(sys.argv[1], "w") as f:
    f.write(json.dumps({"role": "assistant", "content": "Just prose. No structured output at all."}) + "\n")
PYEOF

A1="$TMP/a1.json"
A2="$TMP/a2.json"
A3="$TMP/a3.json"
E3="$TMP/e3.stderr"

echo "--- Running 3 hook invocations ---"
run_hook "$T1" "$SESS" "$A1"; RC1=$?
run_hook "$T2" "$SESS" "$A2"; RC2=$?
run_hook "$T3" "$SESS" "$A3" "$E3"; RC3=$?

echo ""
echo "--- Exit codes ---"
[[ $RC1 -eq 0 ]] && _pass "subagent 1 exit 0" || _fail "subagent 1 exit $RC1"
[[ $RC2 -eq 0 ]] && _pass "subagent 2 exit 0" || _fail "subagent 2 exit $RC2"
[[ $RC3 -eq 0 ]] && _pass "subagent 3 exit 0" || _fail "subagent 3 exit $RC3"

echo ""
echo "--- Role resolution (subagents 1 and 2) ---"
python3 - "$A1" "$A2" <<'PYCHECK'
import json, sys

a1, a2 = [json.load(open(f)) for f in sys.argv[1:3]]

# Bug A fix: roles from hook_event_id extraction
assert a1.get("role") == "executor",      f"Expected executor, got: {a1.get('role')!r}"
assert a2.get("role") == "code-reviewer", f"Expected code-reviewer, got: {a2.get('role')!r}"

# Bug B fix: subagents with hook_event_id use canonical event-id (no nanos
# suffix), and the two are distinct from each other.
assert a1.get("event_id") == "executor-984-1715800001", f"a1 event_id: {a1.get('event_id')!r}"
assert a2.get("event_id") == "code-reviewer-984-1715800002", f"a2 event_id: {a2.get('event_id')!r}"
assert a1.get("event_id") != a2.get("event_id"), \
    f"a1 and a2 event-ids collided: {a1.get('event_id')!r}"

print("All role and event-id assertions passed")
PYCHECK
[[ $? -eq 0 ]] && _pass "role resolution and distinct event-ids (Bug A + Bug B fix)" || _fail "assertion failure"

echo ""
echo "--- Subagent 3: noise-drop path (section 3b), not a crash ---"
# D#2168: the hook chose not to write a3.json — section 3b drops
# unknown:unknown rows with no spawn context before the dry-run write is ever
# reached. Both halves are required: file absence alone also holds if the
# hook crashed on startup, which is exactly the ambiguity this suite used to
# have no way to resolve.
if [[ -f "$A3" ]]; then
  _fail "a3.json should not exist — section 3b should have dropped this row before the write"
else
  _pass "a3.json absent (section 3b noise-drop, not a crash)"
fi
if grep -q "skipped unknown:unknown row (no spawn context) session=$SESS" "$E3" 2>/dev/null; then
  _pass "hook emitted the noise-drop trace for subagent 3"
else
  _fail "hook did not emit the expected noise-drop trace for subagent 3"
fi

echo ""
echo "--- Summary ---"
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
