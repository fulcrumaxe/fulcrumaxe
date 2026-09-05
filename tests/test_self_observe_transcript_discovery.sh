#!/usr/bin/env bash
# tests/test_self_observe_transcript_discovery.sh
#
# Verifies that the self-observe gate (scripts/lib/self-observe-gate.sh):
#   1. Discovers transcripts via $HOME (D#856)
#   2. Interpolates the repo root and its Claude-Code project-transcript slug
#      at GENERATION time, from a caller-supplied repo root — never a
#      hardcoded checkout path (D#1876, Finding A/B/D)
#   3. Never swallows a failed analyst invocation into a bare "[]" (D#1876,
#      scope item 2)
#   4. Emits syntactically valid bash — the if/else opened by transcript
#      discovery is actually closed (D#1876, Finding E — regression since
#      PR #694, 2026-05-13)
#
# Run: bash tests/test_self_observe_transcript_discovery.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE_SH="$REPO_ROOT/scripts/lib/self-observe-gate.sh"
EXPECTED_SLUG="${REPO_ROOT//\//-}"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — ${2:-}"; FAIL=$((FAIL + 1)); ERRORS+=("$1: ${2:-}"); }

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    pass "$label"
  else
    fail "$label" "expected to find: '$needle'"
    echo "    Searched in:" >&2
    echo "$haystack" | head -10 >&2
  fi
}

assert_not_contains() {
  local label="$1" haystack="$2" needle="$3"
  if ! echo "$haystack" | grep -qF "$needle"; then
    pass "$label"
  else
    fail "$label" "expected NOT to find: '$needle'"
    echo "    Found in:" >&2
    echo "$haystack" | head -10 >&2
  fi
}

# Same as assert_not_contains, but the needle is an extended regex. Used for
# the checkout-path assertion below, where matching a *pattern* rather than one
# spelling is both stronger and the only way to write the assertion without
# this file tripping scripts/check-no-hardcoded-checkout-paths.sh on itself.
assert_not_matches() {
  local label="$1" haystack="$2" pattern="$3"
  if ! echo "$haystack" | grep -qE "$pattern"; then
    pass "$label"
  else
    fail "$label" "expected NOT to match: '$pattern'"
    echo "    Found in:" >&2
    echo "$haystack" | grep -nE "$pattern" | head -5 >&2
  fi
}

# Extracts the ```bash ... ``` fenced block from a gate variant's output.
extract_bash_fence() {
  echo "$1" | sed -n '/^```bash$/,/^```$/p' | sed '1d;$d'
}

echo ""
echo "=== test_self_observe_transcript_discovery.sh ==="
echo ""

# ── Test 1: discovery finds fixture when AGENT_ID matches worktree dir ────────
echo "--- Test 1: discovery finds fixture JSONL via \$HOME path ---"

FAKE_AGENT_ID="testworker-$(date +%s)"
FAKE_HOME=$(mktemp -d)
FAKE_SLUG="-home-fake-checkout-for-test"
FAKE_SUBAGENT_DIR="$FAKE_HOME/.claude/projects/$FAKE_SLUG/session-abc123/subagents"
mkdir -p "$FAKE_SUBAGENT_DIR"
FIXTURE_FILE="$FAKE_SUBAGENT_DIR/agent-${FAKE_AGENT_ID}.jsonl"
echo '{"type":"text","text":"hello world"}' > "$FIXTURE_FILE"

FAKE_WORKTREE_PATH="/some/path/.claude/worktrees/agent-${FAKE_AGENT_ID}"

# Run the discovery logic from the gate (inline, simulating what an agent would run,
# against an arbitrary slug — the discovery *mechanism* is slug-agnostic; which slug
# is correct for a given checkout is covered separately in Test 5/6 below).
FOUND=$(HOME="$FAKE_HOME" bash -c "
  WORKTREE_PATH='$FAKE_WORKTREE_PATH'
  AGENT_ID=\$(basename \"\$WORKTREE_PATH\" | sed 's/^agent-//')
  TRANSCRIPT=\$(ls -t \"\$HOME\"/.claude/projects/$FAKE_SLUG/*/subagents/agent-\${AGENT_ID}.jsonl 2>/dev/null | head -1)
  [ -r \"\$TRANSCRIPT\" ] || TRANSCRIPT=\"\"
  echo \"\$TRANSCRIPT\"
" 2>/dev/null)

if [[ "$FOUND" == "$FIXTURE_FILE" ]]; then
  pass "T1: discovery returned fixture path"
else
  fail "T1: discovery returned fixture path" "expected '$FIXTURE_FILE', got '$FOUND'"
fi

rm -rf "$FAKE_HOME"

# ── Test 2: discovery returns empty when no matching file exists ──────────────
echo "--- Test 2: discovery returns empty when no transcript exists ---"

FAKE_AGENT_ID2="nonexistent-$(date +%s)"
FAKE_HOME2=$(mktemp -d)
FAKE_WORKTREE_PATH2="/some/path/.claude/worktrees/agent-${FAKE_AGENT_ID2}"

FOUND2=$(HOME="$FAKE_HOME2" bash -c "
  WORKTREE_PATH='$FAKE_WORKTREE_PATH2'
  AGENT_ID=\$(basename \"\$WORKTREE_PATH\" | sed 's/^agent-//')
  TRANSCRIPT=\$(ls -t \"\$HOME\"/.claude/projects/$FAKE_SLUG/*/subagents/agent-\${AGENT_ID}.jsonl 2>/dev/null | head -1)
  [ -r \"\$TRANSCRIPT\" ] || TRANSCRIPT=\"\"
  echo \"\$TRANSCRIPT\"
" 2>/dev/null)

if [[ -z "$FOUND2" ]]; then
  pass "T2: empty result when no transcript exists"
else
  fail "T2: empty result when no transcript exists" "expected empty, got '$FOUND2'"
fi

rm -rf "$FAKE_HOME2"

# ── Test 3: gate block interpolates the CALLER-SUPPLIED repo root's real slug ─
# This is the D#1876 fix: the old code hardcoded the slug for a checkout path
# (-home-agent-autonomous-forever) that has never existed on this machine, so
# TRANSCRIPT always resolved empty and execution never reached step 2 (Finding A).
echo "--- Test 3: gate block interpolates repo root's real transcript slug ---"

source "$GATE_SH"

SHADOW_BLOCK=$(self_observe_gate_block --shadow "$REPO_ROOT" 2>&1)
ACTIVE_BLOCK=$(self_observe_gate_block "$REPO_ROOT" 2>&1)

assert_contains    "T3a: shadow block has real \$REPO_ROOT slug" "$SHADOW_BLOCK" ".claude/projects/$EXPECTED_SLUG"
assert_contains    "T3b: active block has real \$REPO_ROOT slug" "$ACTIVE_BLOCK" ".claude/projects/$EXPECTED_SLUG"
assert_not_contains "T3c: shadow block has no old /tmp glob invocation" "$SHADOW_BLOCK" 'ls -t /tmp/claude-*'
assert_not_contains "T3d: active block has no old /tmp glob invocation" "$ACTIVE_BLOCK" 'ls -t /tmp/claude-*'

# ── Test 4: gate block references D#856 ──────────────────────────────────────
echo "--- Test 4: gate block references D#856 in discovery comment ---"

assert_contains "T4a: shadow block cites D#856" "$SHADOW_BLOCK" "D#856"
assert_contains "T4b: active block cites D#856" "$ACTIVE_BLOCK" "D#856"

# ── Test 5: the checkout path is interpolated, not hardcoded ─────────────────
# Separate assertion per variant (Spec item 9 — mutation-test each assertion
# individually, not each file).
#
# What D#1876 actually fixed was that the gate stopped *hardcoding* a checkout
# path and started resolving one. The generated block still contains an
# absolute path, and must: the agent that runs it needs a real path to run
# against. So the assertion is not "no absolute path appears" — it is "the only
# absolute checkout path that appears is this machine's".
#
# The previous version of this test asserted that one specific home directory
# never appeared. That is not the same thing, and it only passed because this
# host is not that user: on a machine whose home directory matched, the
# correctly-interpolated path would have failed it. A test whose result depends
# on the tester's username is not testing the code. D#1997.
echo "--- Test 5: checkout path is interpolated, not hardcoded ---"

CHECKOUT_PATH_RE='/home/(agent|jp)'
source "$REPO_ROOT/scripts/lib/repo-root-resolve.sh"
SELF_OBSERVE_ROOT="$(_resolve_repo_root)"

# Positive half: interpolation actually happened. Without this, stripping the
# root below would make the negative half vacuous for an empty block.
assert_contains "T5a: shadow block interpolates this checkout" "$SHADOW_BLOCK" "$SELF_OBSERVE_ROOT"
assert_contains "T5b: active block interpolates this checkout" "$ACTIVE_BLOCK" "$SELF_OBSERVE_ROOT"

# Negative half: with the legitimately-interpolated root removed, nothing that
# looks like a checkout path may remain — that residue could only be a literal.
assert_not_matches "T5c: shadow block has no other checkout path" \
  "${SHADOW_BLOCK//$SELF_OBSERVE_ROOT/}" "$CHECKOUT_PATH_RE"
assert_not_matches "T5d: active block has no other checkout path" \
  "${ACTIVE_BLOCK//$SELF_OBSERVE_ROOT/}" "$CHECKOUT_PATH_RE"

# ── Test 6: agent-side variables survive generation-time interpolation ────────
# Naively unquoting the heredoc to interpolate $REPO_ROOT would also blank these
# (Finding D). Assert each one individually, not one grep over the whole file.
echo "--- Test 6: agent-side variables survive interpolation (per-variable) ---"

for var in '$HOME' '$TRANSCRIPT' '$FINDINGS' '$AGENT_ID' '$YOUR_ROLE' '$WORKTREE_PATH'; do
  assert_contains "T6-shadow: $var survives in shadow block" "$SHADOW_BLOCK" "$var"
  assert_contains "T6-active: $var survives in active block" "$ACTIVE_BLOCK" "$var"
done
assert_contains "T6-active: \$TURN_IDX survives in active block" "$ACTIVE_BLOCK" '$TURN_IDX'

# ── Test 6b: CLASSIFIER is actually ASSIGNED before it is used (D#1876 blocker) ─
# Checking that the literal substring "$CLASSIFIER" appears somewhere in the
# block (the way T6 above checks other agent-side vars) is NOT sufficient: a
# bare *use* with no assignment also contains that substring, and that is
# exactly the active-mode blocker (`--classifier "$CLASSIFIER"` with no
# `CLASSIFIER=` anywhere in the fence) — an "unbound variable" under `set -u`,
# or a silently blank classifier written to real retros otherwise. Model this
# on T9c/T9d below: count/locate actual structure (an assignment line, and
# its position relative to the use), not just whether words appear.
echo "--- Test 6b: CLASSIFIER= assignment exists and precedes its use ---"

SHADOW_FENCE_FOR_T6B=$(extract_bash_fence "$SHADOW_BLOCK")
ACTIVE_FENCE_FOR_T6B=$(extract_bash_fence "$ACTIVE_BLOCK")

SHADOW_CLASSIFIER_ASSIGN_LINE=$(echo "$SHADOW_FENCE_FOR_T6B" | grep -nE '^\s*CLASSIFIER=' | head -1 | cut -d: -f1)
SHADOW_CLASSIFIER_USE_LINE=$(echo "$SHADOW_FENCE_FOR_T6B" | grep -nF -- '--classifier "$CLASSIFIER"' | head -1 | cut -d: -f1)
ACTIVE_CLASSIFIER_ASSIGN_LINE=$(echo "$ACTIVE_FENCE_FOR_T6B" | grep -nE '^\s*CLASSIFIER=' | head -1 | cut -d: -f1)
ACTIVE_CLASSIFIER_USE_LINE=$(echo "$ACTIVE_FENCE_FOR_T6B" | grep -nF -- '--classifier "$CLASSIFIER"' | head -1 | cut -d: -f1)

if [[ -n "$SHADOW_CLASSIFIER_ASSIGN_LINE" ]]; then
  pass "T6b-shadow: CLASSIFIER= assignment exists in shadow fence"
else
  fail "T6b-shadow: CLASSIFIER= assignment exists in shadow fence" "no 'CLASSIFIER=' line found"
fi

if [[ -n "$ACTIVE_CLASSIFIER_ASSIGN_LINE" ]]; then
  pass "T6b-active: CLASSIFIER= assignment exists in active fence"
else
  fail "T6b-active: CLASSIFIER= assignment exists in active fence" "no 'CLASSIFIER=' line found"
fi

if [[ -n "$SHADOW_CLASSIFIER_ASSIGN_LINE" && -n "$SHADOW_CLASSIFIER_USE_LINE" && "$SHADOW_CLASSIFIER_ASSIGN_LINE" -lt "$SHADOW_CLASSIFIER_USE_LINE" ]]; then
  pass "T6c-shadow: CLASSIFIER= assignment precedes --classifier use"
else
  fail "T6c-shadow: CLASSIFIER= assignment precedes --classifier use" "assign_line=$SHADOW_CLASSIFIER_ASSIGN_LINE use_line=$SHADOW_CLASSIFIER_USE_LINE"
fi

if [[ -n "$ACTIVE_CLASSIFIER_ASSIGN_LINE" && -n "$ACTIVE_CLASSIFIER_USE_LINE" && "$ACTIVE_CLASSIFIER_ASSIGN_LINE" -lt "$ACTIVE_CLASSIFIER_USE_LINE" ]]; then
  pass "T6c-active: CLASSIFIER= assignment precedes --classifier use"
else
  fail "T6c-active: CLASSIFIER= assignment precedes --classifier use" "assign_line=$ACTIVE_CLASSIFIER_ASSIGN_LINE use_line=$ACTIVE_CLASSIFIER_USE_LINE"
fi

# ── Test 7: generated commands interpolate the real $REPO_ROOT and point at
# existing files ────────────────────────────────────────────────────────────
# Checking only "does this file exist on disk", independent of the generated
# block text, does not test the generator at all: a mutation to the active
# variant's budget.py path interpolation (e.g. dropping the __REPO_ROOT__
# substitution for that one line, or hardcoding a stale path) leaves
# backend/budget.py sitting on disk exactly as before, so the old
# disk-existence-only check passed regardless. Assert the actual generated
# text contains the real interpolated path, per script per variant, and keep
# the disk-existence check as a secondary sanity check (both must hold).
echo "--- Test 7: generated blocks interpolate real \$REPO_ROOT paths that exist on disk ---"

for f in backend/run_analyst.py backend/agent_retros.py; do
  assert_contains "T7-shadow: generated shadow block interpolates \$REPO_ROOT/$f" "$SHADOW_BLOCK" "$REPO_ROOT/$f"
  assert_contains "T7-active: generated active block interpolates \$REPO_ROOT/$f" "$ACTIVE_BLOCK" "$REPO_ROOT/$f"
  if [[ -f "$REPO_ROOT/$f" ]]; then
    pass "T7: $f exists on disk"
  else
    fail "T7: $f exists on disk" "not found at $REPO_ROOT/$f"
  fi
done

# backend/budget.py is only referenced in the active variant (the BUDGET_PCT
# check) — shadow mode never calls it.
assert_contains "T7-active: generated active block interpolates \$REPO_ROOT/backend/budget.py" "$ACTIVE_BLOCK" "$REPO_ROOT/backend/budget.py"
if [[ -f "$REPO_ROOT/backend/budget.py" ]]; then
  pass "T7: backend/budget.py exists on disk"
else
  fail "T7: backend/budget.py exists on disk" "not found at $REPO_ROOT/backend/budget.py"
fi

# ── Test 8: the interpolated slug directory is real and the full glob resolves ─
# Only meaningful on the checkout Claude Code has actually registered a
# ~/.claude/projects/<slug> entry for. Whether that's true is a property of
# *invocation history* (has a session ever been opened at this exact path?),
# not of worktree-vs-non-worktree. A prior version of this check used
# `[[ -f "$REPO_ROOT/.git" ]]` to detect "linked worktree" and skip in that
# case only — but that only tells a linked worktree apart from everything
# else. A PLAIN CLONE AT A NOVEL PATH also has `.git` as a directory (same as
# the main checkout), so it sailed past that check and then spuriously failed
# T8a/T8b for the unrelated reason that nothing was ever opened at that path
# — precisely the environment D#1864 tells reviewers to run from. Skip on the
# actual, checkable condition instead: whether the expected slug directory
# exists at all.
echo "--- Test 8: interpolated slug resolves to a real ~/.claude/projects dir ---"

SLUG_DIR="$HOME/.claude/projects/$EXPECTED_SLUG"

if [[ ! -d "$SLUG_DIR" ]]; then
  echo "  SKIP: T8a/T8b — no \$HOME/.claude/projects/<slug> entry for this checkout"
  echo "        ($SLUG_DIR). Claude Code only creates one for a path it has"
  echo "        actually opened a session against — a git worktree or a plain"
  echo "        clone at a novel path (D#1864) both legitimately lack one."
  echo "        Verified separately against the main checkout root in the PR description."
else
  pass "T8a: \$HOME/.claude/projects/<slug> exists for this checkout"

  KNOWN_TRANSCRIPT=$(find "$SLUG_DIR" -path '*/subagents/agent-*.jsonl' 2>/dev/null | head -1)
  if [[ -n "$KNOWN_TRANSCRIPT" ]]; then
    pass "T8b: glob matches >=1 real subagent transcript"
  else
    fail "T8b: glob matches >=1 real subagent transcript" "no subagents/agent-*.jsonl found under $SLUG_DIR"
  fi
fi

# ── Test 8c: the swallow pattern itself is gone — the actual defect, not just
# its downstream symptom. `2>/dev/null || echo "[]"` converts "could not run"
# into "found nothing"; assert that exact pattern is absent, and that both
# variants have distinct loud-failure reporting instead.
echo "--- Test 8c: the [] swallow pattern is gone from both variants ---"

assert_not_contains "T8c-shadow: no bare [] swallow on run_analyst.py call" "$SHADOW_BLOCK" '2>/dev/null || echo "[]"'
assert_not_contains "T8c-active: no bare [] swallow on run_analyst.py call" "$ACTIVE_BLOCK" '2>/dev/null || echo "[]"'
assert_contains     "T8d-shadow: loud-failure reporting present" "$SHADOW_BLOCK" "run_analyst.py failed"
assert_contains     "T8d-active: loud-failure reporting present" "$ACTIVE_BLOCK" "run_analyst.py failed"

# ── Test 9: emitted block is syntactically valid bash (Finding E regression) ──
# Regression since PR #694 (2026-05-13): the transcript-discovery `if` was
# introduced there and never closed. PR #539 (the file's original commit) had
# no if/else at all, so "restore prior state" is not the right target —
# bash -n on the CURRENT structure is.
echo "--- Test 9: emitted bash fence passes 'bash -n' (both variants) ---"

SHADOW_FENCE=$(extract_bash_fence "$SHADOW_BLOCK")
ACTIVE_FENCE=$(extract_bash_fence "$ACTIVE_BLOCK")

# bash -n stderr captures live under mktemp'd files, not fixed
# /tmp/{shadow,active}-bashn-err.$$ names (D#2254).
SHADOW_BASHN_ERR="$(mktemp /tmp/test_self_observe_shadow_bashn.XXXXXX)"
ACTIVE_BASHN_ERR="$(mktemp /tmp/test_self_observe_active_bashn.XXXXXX)"

if [[ -n "$SHADOW_FENCE" ]] && echo "$SHADOW_FENCE" | bash -n 2>"$SHADOW_BASHN_ERR"; then
  pass "T9a: shadow variant's bash fence is syntactically valid"
else
  fail "T9a: shadow variant's bash fence is syntactically valid" "$(cat "$SHADOW_BASHN_ERR" 2>/dev/null)"
fi
rm -f "$SHADOW_BASHN_ERR"

if [[ -n "$ACTIVE_FENCE" ]] && echo "$ACTIVE_FENCE" | bash -n 2>"$ACTIVE_BASHN_ERR"; then
  pass "T9b: active variant's bash fence is syntactically valid"
else
  fail "T9b: active variant's bash fence is syntactically valid" "$(cat "$ACTIVE_BASHN_ERR" 2>/dev/null)"
fi
rm -f "$ACTIVE_BASHN_ERR"

# `else` must be paired with a `fi` inside each fence — the specific defect
# this ticket fixes, tested directly rather than only via bash -n (which
# would also pass a script with zero if/else blocks at all).
SHADOW_IF_COUNT=$(echo "$SHADOW_FENCE" | grep -cE '^\s*if ')
SHADOW_FI_COUNT=$(echo "$SHADOW_FENCE" | grep -cE '^\s*fi\s*$')
if [[ "$SHADOW_IF_COUNT" -gt 0 && "$SHADOW_IF_COUNT" -eq "$SHADOW_FI_COUNT" ]]; then
  pass "T9c: shadow variant's if/fi counts balance ($SHADOW_IF_COUNT each)"
else
  fail "T9c: shadow variant's if/fi counts balance" "if=$SHADOW_IF_COUNT fi=$SHADOW_FI_COUNT"
fi

ACTIVE_IF_COUNT=$(echo "$ACTIVE_FENCE" | grep -cE '^\s*if ')
ACTIVE_FI_COUNT=$(echo "$ACTIVE_FENCE" | grep -cE '^\s*fi\s*$')
if [[ "$ACTIVE_IF_COUNT" -gt 0 && "$ACTIVE_IF_COUNT" -eq "$ACTIVE_FI_COUNT" ]]; then
  pass "T9d: active variant's if/fi counts balance ($ACTIVE_IF_COUNT each)"
else
  fail "T9d: active variant's if/fi counts balance" "if=$ACTIVE_IF_COUNT fi=$ACTIVE_FI_COUNT"
fi

# ── Test 10: repo root is required — loud failure, not a block with an empty path ─
echo "--- Test 10: refuses to emit a block when no repo root can be resolved ---"

NO_ROOT_OUTPUT=$(cd / && REPO_ROOT="" HOME=/nonexistent-home-for-test bash -c "
  cd /
  source '$GATE_SH'
  self_observe_gate_block --shadow 2>&1 1>/dev/null
" 2>&1)
NO_ROOT_RC=0
(cd / && REPO_ROOT="" HOME=/nonexistent-home-for-test bash -c "
  cd /
  source '$GATE_SH'
  self_observe_gate_block --shadow >/dev/null 2>&1
") || NO_ROOT_RC=$?

if [[ "$NO_ROOT_RC" -ne 0 ]]; then
  pass "T10a: unresolvable repo root returns non-zero"
else
  fail "T10a: unresolvable repo root returns non-zero" "returned 0"
fi
assert_contains "T10b: unresolvable repo root reports an error, not silence" "$NO_ROOT_OUTPUT" "cannot resolve repo root"

# ── Test 11: a failed analyst invocation is distinguishable from an empty result ─
echo "--- Test 11: broken root reports an error, healthy root reports valid JSON ---"

BROKEN_ROOT="/nonexistent/broken/root/for/d1876/test"
FAKE_TRANSCRIPT=$(mktemp)
echo '{}' > "$FAKE_TRANSCRIPT"

if BROKEN_OUT=$(python3 "$BROKEN_ROOT/backend/run_analyst.py" --single-transcript "$FAKE_TRANSCRIPT" 2>&1); then
  fail "T11a: broken root fails loudly (not silently)" "python3 unexpectedly succeeded: $BROKEN_OUT"
else
  if [[ "$BROKEN_OUT" != "[]" && -n "$BROKEN_OUT" ]]; then
    pass "T11a: broken root fails loudly (not silently, not '[]')"
  else
    fail "T11a: broken root fails loudly (not silently, not '[]')" "got: '$BROKEN_OUT'"
  fi
fi

EMPTY_TRANSCRIPT=$(mktemp)
: > "$EMPTY_TRANSCRIPT"
if HEALTHY_OUT=$(python3 "$REPO_ROOT/backend/run_analyst.py" --single-transcript "$EMPTY_TRANSCRIPT" 2>&1); then
  if echo "$HEALTHY_OUT" | python3 -c "import json,sys; json.load(sys.stdin)" >/dev/null 2>&1; then
    pass "T11b: healthy root with zero findings returns valid JSON (legitimate empty result)"
  else
    fail "T11b: healthy root with zero findings returns valid JSON" "not valid JSON: $HEALTHY_OUT"
  fi
else
  fail "T11b: healthy root with zero findings returns valid JSON" "unexpectedly failed: $HEALTHY_OUT"
fi

rm -f "$FAKE_TRANSCRIPT" "$EMPTY_TRANSCRIPT"

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
fi

echo ""
[[ $FAIL -eq 0 ]]
