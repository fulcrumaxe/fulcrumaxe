#!/usr/bin/env bash
# tests/test_subagent_stop_dial_audit.sh
#
# Smoke tests for hooks/subagent_stop_dial_audit.py
#
# Constructs synthetic JSONL transcripts that contain Agent tool_use entries,
# invokes the SubagentStop hook, and asserts that audit rows appear in the
# blocks-YYYY-MM-DD.jsonl file.
#
# Usage: bash tests/test_subagent_stop_dial_audit.sh
# Exit 0 = all tests passed, non-zero = at least one failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/hooks/subagent_stop_dial_audit.py"

# D#2267: this hook's _HOOK_EVENTS_DIR is `resolve_main_repo_root() /
# ".autonomous-team" / "hook-events"` (hooks/subagent_stop_dial_audit.py), and
# resolve_main_repo_root() *does* honour SANDBOX_MAIN_REPO_ROOT — its own
# module comment says the override exists "so the shell tests can pin it to
# a synthetic root." Use that sanctioned seam instead of pointing this suite
# at the real, live blocks-<date>.jsonl every other running agent also
# writes to. (No git structure needed here, unlike hooks/sandbox.py's
# classify_cwd — this hook does not gate on repo-root confidence.)
FIXTURE_ROOT="$(mktemp -d "$REPO_ROOT/.repo-root-fixture.XXXXXX")" || {
  echo "FAIL: could not create isolated repo-root fixture" >&2
  exit 1
}
trap 'rm -rf "$FIXTURE_ROOT"' EXIT
export SANDBOX_MAIN_REPO_ROOT="$FIXTURE_ROOT"
MAIN_REPO_ROOT="$FIXTURE_ROOT"

BLOCKS_FILE="$MAIN_REPO_ROOT/.autonomous-team/hook-events/blocks-$(date +%Y-%m-%d).jsonl"

# A real worktree path under the main repo (the test worktree itself is fine)
WT_CWD="$MAIN_REPO_ROOT/.claude/worktrees/test-stop-hook-99999"
# Team-lead CWD: main repo root (not a worktree)
TL_CWD="$MAIN_REPO_ROOT"

PASS=0
FAIL=0

green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }

count_rows() {
  local kind="$1"
  if [ ! -f "$BLOCKS_FILE" ]; then
    echo "0"
    return
  fi
  python3 -c "
import json, sys
count = 0
with open('$BLOCKS_FILE') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if row.get('kind') == '$kind':
                count += 1
        except Exception:
            pass
print(count)
"
}

echo "=== SubagentStop Dial Audit Hook Tests ==="
echo "Hook: $HOOK"
echo "Blocks file: $BLOCKS_FILE"
echo ""

make_transcript_with_agent() {
  local path="$1"
  python3 -c "
import json
line = json.dumps({
    'type': 'message',
    'role': 'assistant',
    'content': [{
        'type': 'tool_use',
        'id': 'tu_abc',
        'name': 'Agent',
        'input': {'prompt': 'do something dangerous'}
    }]
})
print(line)
" > "$path"
}

make_transcript_no_agent() {
  local path="$1"
  python3 -c "
import json
line = json.dumps({
    'type': 'message',
    'role': 'assistant',
    'content': [{'type': 'text', 'text': 'hello'}]
})
print(line)
" > "$path"
}

invoke_hook() {
  local cwd="$1"
  local transcript="$2"
  python3 -c "
import json
print(json.dumps({'cwd': '$cwd', 'transcript_path': '$transcript'}))
" | CLAUDE_HOOK_CWD="$cwd" CLAUDE_SUBAGENT_TRANSCRIPT_PATH="$transcript" \
    python3 "$HOOK"
}

# ---------------------------------------------------------------------------
# Test 1: worktree CWD + Agent call in transcript -> audit row written
# ---------------------------------------------------------------------------
echo "--- Test 1: worktree + Agent call ---"
T1=$(mktemp /tmp/dial_audit_t1_XXXXXX.jsonl)
make_transcript_with_agent "$T1"
BEFORE=$(count_rows sandbox_block_agent_spawn)
invoke_hook "$WT_CWD" "$T1"
AFTER=$(count_rows sandbox_block_agent_spawn)
rm -f "$T1"
if [ "$AFTER" -gt "$BEFORE" ]; then
  green "PASS Test 1: audit row written ($BEFORE -> $AFTER)"
  PASS=$((PASS+1))
else
  red "FAIL Test 1: expected new audit row; count stayed at $AFTER"
  FAIL=$((FAIL+1))
fi

# ---------------------------------------------------------------------------
# Test 2: worktree CWD + no Agent call -> no new audit row
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 2: worktree + no Agent call ---"
T2=$(mktemp /tmp/dial_audit_t2_XXXXXX.jsonl)
make_transcript_no_agent "$T2"
BEFORE=$(count_rows sandbox_block_agent_spawn)
invoke_hook "$WT_CWD" "$T2"
AFTER=$(count_rows sandbox_block_agent_spawn)
rm -f "$T2"
if [ "$AFTER" -eq "$BEFORE" ]; then
  green "PASS Test 2: no spurious row"
  PASS=$((PASS+1))
else
  red "FAIL Test 2: count went $BEFORE -> $AFTER (should be stable)"
  FAIL=$((FAIL+1))
fi

# ---------------------------------------------------------------------------
# Test 3: team_lead CWD + Agent call -> no audit row
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 3: team_lead CWD + Agent call ---"
T3=$(mktemp /tmp/dial_audit_t3_XXXXXX.jsonl)
make_transcript_with_agent "$T3"
BEFORE=$(count_rows sandbox_block_agent_spawn)
invoke_hook "$TL_CWD" "$T3"
AFTER=$(count_rows sandbox_block_agent_spawn)
rm -f "$T3"
if [ "$AFTER" -eq "$BEFORE" ]; then
  green "PASS Test 3: team-lead CWD skipped"
  PASS=$((PASS+1))
else
  red "FAIL Test 3: count went $BEFORE -> $AFTER (team-lead should not produce rows)"
  FAIL=$((FAIL+1))
fi

# ---------------------------------------------------------------------------
# Test 4: missing transcript path -> exit 0, no crash
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 4: missing transcript ---"
EC=0
python3 -c "import json; print(json.dumps({'cwd': '$WT_CWD', 'transcript_path': '/tmp/nonexistent_abc.jsonl'}))" \
  | python3 "$HOOK" || EC=$?
if [ "$EC" -eq 0 ]; then
  green "PASS Test 4: graceful exit on missing transcript"
  PASS=$((PASS+1))
else
  red "FAIL Test 4: exited $EC (should be 0)"
  FAIL=$((FAIL+1))
fi

# ---------------------------------------------------------------------------
# Test 5: empty stdin -> exit 0, no crash
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 5: empty stdin ---"
EC=0
python3 -c "print('')" | python3 "$HOOK" || EC=$?
if [ "$EC" -eq 0 ]; then
  green "PASS Test 5: graceful exit on empty stdin"
  PASS=$((PASS+1))
else
  red "FAIL Test 5: exited $EC (should be 0)"
  FAIL=$((FAIL+1))
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  red "FAIL -- $FAIL test(s) did not pass"
  exit 1
else
  green "All $PASS tests passed"
  exit 0
fi
