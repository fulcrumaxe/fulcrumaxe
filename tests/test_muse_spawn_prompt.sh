#!/usr/bin/env bash
# tests/test_muse_spawn_prompt.sh — verify scripts/lib/muse-spawn-prompt.sh.
#
# HARD RULE: no network, no gh, no claude. Uses only local role cards,
# templates, and synthetic briefs.
#
# Usage:
#   bash tests/test_muse_spawn_prompt.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RENDERER="$REPO_ROOT/scripts/lib/muse-spawn-prompt.sh"

PASS=0
FAIL=0
ERRORS=()

# Hermetic temp dir (D#2254 gate: no fixed /tmp paths in tests/).
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
ERR1="$T/muse-spawn-err.txt"
ERR2="$T/muse-spawn-err2.txt"
ERR9="$T/muse-spawn-err9.txt"
PWN="$T/muse-pwned-2601"

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

EVENT_ID="executor-2601-1789589303"
BRIEF="Implement the thing. Closes D#2601."

echo ""
echo "Test 1: positive render exits 0"
OUT=$(bash "$RENDERER" --role executor --brief "$BRIEF" --event-id "$EVENT_ID" --discussion 2601 2>"$ERR1")
RC=$?
if [[ "$RC" -eq 0 ]]; then
  pass "render exits 0"
else
  fail "render exit code" "expected 0, got $RC: $(cat "$ERR1")"
fi

echo ""
echo "Test 2: working principles injected (not forked)"
if echo "$OUT" | grep -q "## Working Principles"; then
  pass "prompt contains ## Working Principles"
else
  fail "working principles" "prompt missing '## Working Principles'"
fi
# Renderer must call working_principles_block, not carry its own copy.
if grep -q "working_principles_block" "$RENDERER" && ! grep -q "Think Before Coding" "$RENDERER"; then
  pass "renderer sources working-principles.sh instead of forking text"
else
  fail "principles sourcing" "renderer must call working_principles_block without forking its text"
fi

echo ""
echo "Test 3: hook_event_id is the last line"
LAST_LINE=$(printf '%s' "$OUT" | tail -1)
if [[ "$LAST_LINE" == "hook_event_id=$EVENT_ID" ]]; then
  pass "hook_event_id last line ('$LAST_LINE')"
else
  fail "hook_event_id last line" "got '$LAST_LINE'"
fi

echo ""
echo "Test 4: AGENT_OUTPUT + tokens_used closing section required"
if echo "$OUT" | grep -q "AGENT_OUTPUT" && echo "$OUT" | grep -q "tokens_used"; then
  pass "prompt requires AGENT_OUTPUT with tokens_used"
else
  fail "closing section" "prompt missing AGENT_OUTPUT/tokens_used requirement"
fi

echo ""
echo "Test 5: Claude-only directives stripped/rewritten (needles live in the real sources)"
# Vacuity guard: each raw needle MUST be present in the role card or spawn
# template, or its absence from the render proves nothing. Grep the sources
# first; a source that stops containing a needle fails loudly here instead
# of passing vacuously below.
CARD_SRC="$REPO_ROOT/.claude/agents/executor.md"
TMPL_SRC="$REPO_ROOT/backend/spawn_templates/executor.tmpl"
for raw in 'autonomous-agent-7/fulcrumaxe' 'repository(owner:"autonomous-agent-7", name:"fulcrumaxe")'; do
  if grep -q -F "$raw" "$CARD_SRC"; then
    pass "needle is live in role card: '$raw'"
  else
    fail "vacuity guard" "role card no longer contains '$raw' — update needles"
  fi
done
BAD=0
for needle in 'autonomous-agent-7'; do
  if echo "$OUT" | grep -q -F "$needle"; then
    fail "sanitize '$needle'" "prompt still contains Claude-only text"
    BAD=1
  fi
done
[[ "$BAD" -eq 0 ]] && pass "no private-plane slug remains"
# Positive proof the slug rule fired (not just absent input): the GraphQL
# owner pattern from the card must render in its public form.
if echo "$OUT" | grep -q -F 'repository(owner:"fulcrumaxe", name:"fulcrumaxe")'; then
  pass "private slug translated to public repo form"
else
  fail "slug translation" "prompt missing public 'repository(owner:\"fulcrumaxe\", name:\"fulcrumaxe\")' form"
fi
if echo "$OUT" | grep -q "Closes D#"; then
  pass "bare Closes D#n reference present"
else
  fail "bare reference" "prompt missing bare 'Closes D#n' form"
fi
# Classes with no live needle in these two sources (spawn-agent.sh, Agent(),
# $CLAUDE_PROJECT_DIR, Discussion URLs) are covered by the hostile-brief
# fixture in Test 9, which injects them by construction.

echo ""
echo "Test 6: negative — missing --event-id fails loudly"
if bash "$RENDERER" --role executor --brief "$BRIEF" 2>"$ERR2"; then
  fail "missing event-id" "expected non-zero exit"
else
  if grep -q "event-id" "$ERR2"; then
    pass "missing event-id exits non-zero with loud error"
  else
    fail "missing event-id stderr" "stderr does not name --event-id"
  fi
fi

echo ""
echo "Test 7: negative — missing --role fails"
if bash "$RENDERER" --brief "$BRIEF" --event-id "$EVENT_ID" 2>/dev/null; then
  fail "missing role" "expected non-zero exit"
else
  pass "missing role exits non-zero"
fi

echo ""
echo "Test 8: negative — unknown role fails"
if bash "$RENDERER" --role no-such-role --brief "$BRIEF" --event-id "$EVENT_ID" 2>/dev/null; then
  fail "unknown role" "expected non-zero exit"
else
  pass "unknown role exits non-zero"
fi

echo ""
echo "Test 9: hostile brief — all five translation classes + command substitution"
# The brief is untrusted Discussion Spec prose. This fixture injects every
# sanitizer class plus shell metacharacters by construction (so no needle is
# vacuous), then asserts none survive rendering.
HOSTILE_BRIEF='Spec prose. See https://github.com/autonomous-agent-7/fulcrumaxe/discussions/2601 for context. Clone autonomous-agent-7/fulcrumaxe and run $(touch '"$PWN"') and `id` with root $CLAUDE_PROJECT_DIR. Spawn via scripts/spawn-agent.sh using Agent(subagent_type=executor) and the claude CLI. Slot: {{discussion_url}}.'
HOUT=$(bash "$RENDERER" --role executor --brief "$HOSTILE_BRIEF" --event-id "$EVENT_ID" --discussion 2601 2>"$ERR9")
HRC=$?
if [[ "$HRC" -ne 0 ]]; then
  fail "hostile render exit code" "expected 0, got $HRC: $(cat "$ERR9")"
else
  pass "hostile render exits 0"
fi
HBAD=0
for needle in 'https://github.com/autonomous-agent-7/fulcrumaxe/discussions/2601' \
              'autonomous-agent-7/fulcrumaxe' \
              "\$(touch $PWN)" \
              '`id`' \
              '$CLAUDE_PROJECT_DIR' \
              'scripts/spawn-agent.sh' \
              'Agent(subagent_type=executor)' \
              'claude CLI' \
              '{{discussion_url}}'; do
  if echo "$HOUT" | grep -q -F "$needle"; then
    fail "hostile needle '$needle'" "hostile brief token survived rendering"
    HBAD=1
  fi
done
[[ "$HBAD" -eq 0 ]] && pass "no hostile brief token survives rendering"
# Positive proof each hostile token was translated, not just dropped input.
for good in 'Closes D#2601' 'implement directly' 'scripts/lib/muse-spawn-prompt.sh' "(touch $PWN)" 'muse CLI'; do
  if echo "$HOUT" | grep -q -F "$good"; then
    pass "hostile translation present: '$good'"
  else
    fail "hostile translation" "prompt missing translated form '$good'"
  fi
done
if echo "$HOUT" | grep -q -F "$REPO_ROOT"; then
  pass "\$CLAUDE_PROJECT_DIR translated to checkout root"
else
  fail "project-dir translation" "prompt missing checkout root '$REPO_ROOT'"
fi
rm -f "$ERR9"

echo ""
echo "Test 10: no literal {{...}} placeholder reaches output (loud markers, not blanks)"
POUT=$(bash "$RENDERER" --role executor --brief "$BRIEF" --event-id "$EVENT_ID" --discussion 2601 2>/dev/null)
if echo "$POUT" | grep -qE '\{\{'; then
  fail "literal placeholder" "rendered prompt contains a literal '{{...}}' token:"
  echo "$POUT" | grep -nE '\{\{[^}]*\}\}' | head -5
else
  pass "no literal {{...}} in rendered prompt"
fi
# Loud, not silent: markers must be present, and the slots this lane HAS
# suppliers for must carry their values.
if echo "$POUT" | grep -q "MUSE:"; then
  pass "unresolvable slots carry loud MUSE: markers"
else
  fail "loud markers" "prompt has neither '{{...}}' nor 'MUSE:' — slots went silently blank"
fi
for good in 'Implement Discussion #2601' \
            'Discussion URL: Closes D#2601' \
            '$(source scripts/lib/repo-resolve.sh && _resolve_code_repo)' \
            'MUSE:include-fragment:hard-stop-no-claude'; do
  if echo "$POUT" | grep -q -F "$good"; then
    pass "placeholder rendered: '$good'"
  else
    fail "placeholder rendering" "prompt missing rendered form '$good'"
  fi
done

rm -f "$ERR1" "$ERR2"

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
