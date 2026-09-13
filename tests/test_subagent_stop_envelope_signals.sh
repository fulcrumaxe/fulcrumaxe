#!/usr/bin/env bash
# tests/test_subagent_stop_envelope_signals.sh — D#1791 PR 3 (the wiring).
#
# PR 2's own reviewer traced scripts/subagent-stop-hook.sh and found it never
# passed --content to post-agent-hook.sh, so on the default SubagentStop
# path CONTENT was always empty, check_impossible_sources always saw
# sources_count=0, and the detector never fired. This suite pins the fix:
# scripts/lib/subagent_payload.py now computes sources_count / claimed_artifact
# from the PARSED envelope (backend.envelope_check's own extract_sources_count
# / extract_claimed_artifact — never raw prose), and
# scripts/subagent-stop-hook.sh threads them to post-agent-hook.sh as
# --sources-count / --claimed-artifact.
#
# Uses the dry-run seam (SUBAGENT_STOP_DRY_RUN / SUBAGENT_STOP_ARGS_FILE)
# throughout, matching tests/test_subagent_tool_use_count.sh's own
# convention — exercises the real subagent-stop-hook.sh and the real
# subagent_payload.py without running post-agent-hook.sh's full chain
# (budget/circuit-breaker/team-log/etc), which would risk a real network
# call to the team log issue in an automated suite.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/scripts/subagent-stop-hook.sh"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

_run_hook_dry() {
  SUBAGENT_STOP_DRY_RUN=1 SUBAGENT_STOP_ARGS_FILE="$2" bash "$HOOK" <<< "$1"
}

# Builds a SubagentStop payload whose last_assistant_message is a fenced
# AGENT_OUTPUT envelope wrapping the given JSON object, plus any prose
# prefix given (argv, never interpolated into shell text).
_make_payload() {
  python3 -c "
import json, sys
env = json.loads(sys.argv[1])
lam = sys.argv[3] + '<!-- AGENT_OUTPUT -->\n\`\`\`json\n' + json.dumps(env) + '\n\`\`\`\n<!-- /AGENT_OUTPUT -->\n'
print(json.dumps({
    'hook_event_name': 'SubagentStop', 'session_id': sys.argv[2],
    'cwd': '/tmp/fake-worktree', 'agent_id': '', 'agent_type': 'researcher',
    'last_assistant_message': lam,
}))
" "$1" "$2" "${3:-}"
}

_args_field() { python3 -c "import json; print(json.load(open('$1')).get('$2', $3))"; }
_args_has_key() { python3 -c "import json; print('$2' in json.load(open('$1')))"; }

# ── Item 1: a fabricated envelope's sources array threads through as ────────
# --sources-count, and a legitimate (empty-sources) envelope forwards 0 —
# never omitted, never mistaken for unknown.
echo "Item 1: sources_count is forwarded, present even when 0"
{
  TMP=$(mktemp -d)
  PAYLOAD=$(_make_payload '{"agent":"researcher","verdict":"pass","sources":[{"url":"https://a.invalid"},{"url":"https://b.invalid"},{"url":"https://c.invalid"},{"url":"https://d.invalid"},{"url":"https://e.invalid"}]}' "sess-item1a")
  _run_hook_dry "$PAYLOAD" "$TMP/a.json"
  VAL=$(_args_field "$TMP/a.json" sources_count None)
  [[ "$VAL" == "5" ]] && _pass "sources_count=5 forwarded for a 5-source envelope" || _fail "expected 5, got $VAL"

  PAYLOAD=$(_make_payload '{"agent":"researcher","verdict":"pass","sources":[]}' "sess-item1b")
  _run_hook_dry "$PAYLOAD" "$TMP/b.json"
  HAS=$(_args_has_key "$TMP/b.json" sources_count)
  VAL=$(_args_field "$TMP/b.json" sources_count None)
  [[ "$HAS" == "True" && "$VAL" == "0" ]] && _pass "sources_count=0 forwarded (not omitted)" || _fail "expected present+0, got has=$HAS val=$VAL"
  rm -rf "$TMP"
}

# ── Item 2: a claimed Discussion-comment permalink in a machine-readable ────
# field forwards as --claimed-artifact; the same URL merely mentioned in
# prose (never a parsed field) must NOT — matches extract_claimed_artifact's
# own contract (parsed dict only, never raw text).
echo "Item 2: claimed_artifact forwarded from a parsed field, never from prose alone"
{
  TMP=$(mktemp -d)
  URL="https://github.com/autonomous-agent-7/fulcrumaxe/discussions/1790#discussioncomment-11645892"
  ENV_JSON=$(python3 -c "import json,sys; print(json.dumps({'agent':'researcher','verdict':'pass','sources':[],'posted_url':sys.argv[1]}))" "$URL")
  PAYLOAD=$(_make_payload "$ENV_JSON" "sess-item2a")
  _run_hook_dry "$PAYLOAD" "$TMP/a.json"
  VAL=$(_args_field "$TMP/a.json" claimed_artifact None)
  [[ "$VAL" == "$URL" ]] && _pass "claimed_artifact forwarded from parsed field" || _fail "expected $URL, got $VAL"

  PAYLOAD=$(_make_payload '{"agent":"researcher","verdict":"pass","sources":[]}' "sess-item2b" "I noticed $URL in passing.

")
  _run_hook_dry "$PAYLOAD" "$TMP/b.json"
  HAS=$(_args_has_key "$TMP/b.json" claimed_artifact)
  [[ "$HAS" == "False" ]] && _pass "prose-only permalink not forwarded" || _fail "claimed_artifact unexpectedly present: $(cat "$TMP/b.json")"
  rm -rf "$TMP"
}

# ── Item 3: the wiring is textually present in both scripts (grep, matching ──
# PR 1's own item-5 precedent for tool_uses).
echo "Item 3: sources-count/claimed-artifact wiring present in both scripts"
{
  HITS_STOP=$(grep -c "sources.count\|claimed.artifact" "$REPO_ROOT/scripts/subagent-stop-hook.sh")
  HITS_POST=$(grep -c "sources.count\|claimed.artifact" "$REPO_ROOT/scripts/post-agent-hook.sh")
  [[ "$HITS_STOP" -gt 0 ]] && _pass "subagent-stop-hook.sh references sources_count/claimed_artifact ($HITS_STOP)" || _fail "no reference in subagent-stop-hook.sh"
  [[ "$HITS_POST" -gt 0 ]] && _pass "post-agent-hook.sh references --sources-count/--claimed-artifact ($HITS_POST)" || _fail "no reference in post-agent-hook.sh"
}

# ── Item 4: THE acceptance test — a real captured fabrication envelope now ──
# produces a finding through the real SubagentStop path end to end. This is
# what PR 2 could not demonstrate (the default path never forwarded
# sources_count at all) and the reason PR 3 exists. Reads the same two real
# 2026-09-12 transcripts backend/tests/test_envelope_check.py replays
# (ENVELOPE_CHECK_FABRICATION_EVIDENCE_DIR, not AUTONOMOUS_TEAM_STATE_DIR —
# see that file's docstring for why), skipping loudly when absent. Wires the
# first file's own copy into the tasks/<agent_id> fixture convention
# find_own_transcript already supports (tests/test_subagent_tool_use_count.sh
# item 4), so tool_uses resolves to a REAL 0, then feeds the hook's own
# resolved tool_uses/sources_count into the real
# `python3 -m backend.envelope_check` CLI, exactly as
# scripts/hooks/post-agent.d/envelope-check.sh would.
echo "Item 4: real captured fabrication envelopes produce a finding end-to-end"
{
  FAB_DIR="${ENVELOPE_CHECK_FABRICATION_EVIDENCE_DIR:-$HOME/.autonomous-forever-state/fabrication-evidence}"
  F1="$FAB_DIR/D1791-instance-2026-09-12-researcher-2565.jsonl"
  F2="$FAB_DIR/D1791-instance-2026-09-12-researcher-2565-second.jsonl"
  if [[ ! -f "$F1" || ! -f "$F2" ]]; then
    echo "  SKIP: real captured fabrication evidence not present on this host ($FAB_DIR) — host-local, sensitive state, not committed"
  else
    TMP=$(mktemp -d)
    TASKS_DIR="$TMP/parent/tasks"
    mkdir -p "$TASKS_DIR"
    AGENT_ID="researcher-2565-fabtest-$$"
    cp "$F1" "$TASKS_DIR/$AGENT_ID"

    RESULT=$(python3 -c "
import json, re, sys

def last_assistant_text(path):
    last = ''
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
            if not isinstance(msg, dict) or msg.get('role') != 'assistant':
                continue
            content = msg.get('content')
            if isinstance(content, list):
                texts = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text' and b.get('text')]
                if texts:
                    last = texts[-1]
    return last

def sources_len(text):
    m = re.search(r'<!--\s*AGENT_OUTPUT\s*-->\s*\`\`\`json\s*(.*?)\s*\`\`\`\s*<!--\s*/AGENT_OUTPUT\s*-->', text, re.DOTALL)
    if not m:
        return 0
    env = json.loads(m.group(1))
    sources = env.get('sources')
    return len(sources) if isinstance(sources, list) else 0

f1_text = last_assistant_text(sys.argv[1])
f2_text = last_assistant_text(sys.argv[2])
payload1 = json.dumps({
    'hook_event_name': 'SubagentStop', 'session_id': 'sess-e2e',
    'transcript_path': sys.argv[3], 'cwd': '/tmp/fake-worktree',
    'agent_id': sys.argv[4], 'agent_type': 'researcher',
    'last_assistant_message': f1_text,
})
print(json.dumps({'payload1': payload1, 'f2_sources': sources_len(f2_text)}))
" "$F1" "$F2" "$TMP/parent/transcript.jsonl" "$AGENT_ID")
    PAYLOAD1=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['payload1'])" "$RESULT")
    F2_SOURCES=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['f2_sources'])" "$RESULT")

    _run_hook_dry "$PAYLOAD1" "$TMP/args.json"
    TOOL_USES=$(_args_field "$TMP/args.json" tool_uses "''")
    SOURCES_COUNT=$(_args_field "$TMP/args.json" sources_count 0)
    [[ "$TOOL_USES" == "0" ]] && _pass "real transcript resolves tool_uses=0 (genuinely zero, not unknown)" || _fail "expected tool_uses=0, got '$TOOL_USES'"
    [[ "$SOURCES_COUNT" -gt 0 ]] && _pass "real envelope resolves sources_count=$SOURCES_COUNT (>0)" || _fail "expected sources_count>0, got $SOURCES_COUNT"
    [[ "$F2_SOURCES" -gt 0 ]] && _pass "second real envelope also has a non-empty sources array ($F2_SOURCES)" || _fail "expected second file's sources>0, got $F2_SOURCES"

    FINDING_JSON=$(cd "$REPO_ROOT" && python3 -m backend.envelope_check --tool-uses "$TOOL_USES" --sources-count "$SOURCES_COUNT" --json)
    FINDING=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('finding'))" "$FINDING_JSON")
    [[ "$FINDING" == "impossible_sources_without_tool_calls" ]] \
      && _pass "end-to-end: real fabrication envelope through the real SubagentStop path now produces a finding" \
      || _fail "expected finding=impossible_sources_without_tool_calls, got '$FINDING' (raw: $FINDING_JSON)"
    rm -rf "$TMP"
  fi
}

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
