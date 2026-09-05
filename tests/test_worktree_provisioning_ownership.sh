#!/usr/bin/env bash
# tests/test_worktree_provisioning_ownership.sh — regression coverage for D#2222.
#
# D#2222 bug: the canonical fresh-spawn shape (--isolation worktree, no --pr,
# no --worktree-path — see scripts/lib/team-lead-prompts.sh's EXECUTOR
# snippet) made spawn-agent.sh assemble a prompt telling the agent to
# hard-fail, even though nothing had failed: the Agent tool's own
# isolation="worktree" param on the actual Agent() call provisions the real
# tree, a step spawn-agent.sh has no visibility into. Separately,
# --worktree-path was accepted and silently ignored whenever the Agent tool
# also provisioned its own tree, producing a registry entry describing a
# tree nothing ever ran in.
#
# This file checks:
#   1. --worktree-path without --pr is rejected loudly (exit non-zero, error
#      on stderr) instead of accepted-and-ignored.
#   2. --isolation worktree alone (no --pr, no --worktree-path) renders the
#      honest "Agent tool provisions this" block, not the old
#      "NO WORKTREE WAS PROVISIONED ... hard-fail" claim.
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs — no real GitHub API calls.
#
# Usage:
#   bash tests/test_worktree_provisioning_ownership.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_SCRIPT="$REPO_ROOT/scripts/spawn-agent.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

# ── Test 1: --worktree-path without --pr is rejected loudly ──────────────────

echo ""
echo "Test 1: --worktree-path without --pr is rejected, not silently accepted"

REJECT_OUT=$(bash "$SPAWN_SCRIPT" \
  --role executor \
  --discussion 900 \
  --task-prompt "test task" \
  --isolation worktree \
  --worktree-path "/tmp/some-hand-made-worktree" \
  2>&1)
REJECT_RC=$?

if [[ "$REJECT_RC" -ne 0 ]]; then
  pass "exits non-zero when --worktree-path is passed without --pr"
else
  fail "exit code" "expected non-zero, got 0"
fi

if echo "$REJECT_OUT" | grep -q -- "--worktree-path requires --pr"; then
  pass "error message names the actual constraint"
else
  fail "error message" "expected '--worktree-path requires --pr' in output, got: $REJECT_OUT"
fi

# ── Setup for Test 2: stub pre-spawn-check.sh + gh so no network is hit ──────

TEST_DIR=$(mktemp -d)
STATS_DB="$TEST_DIR/stats.duckdb"
export STATS_DB_PATH="$STATS_DB"

SCRIPTS_DIR="$TEST_DIR/scripts"
mkdir -p "$SCRIPTS_DIR"

cat > "$SCRIPTS_DIR/rotate-team-log.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRIPTS_DIR/rotate-team-log.sh"

cat > "$SCRIPTS_DIR/pre-spawn-check.sh" <<'STUB'
#!/usr/bin/env bash
cat <<JSON
{
  "allowed": true,
  "persona_voice": "",
  "working_principles": "",
  "self_observe_gate": "",
  "gate_context": {"gates": {}}
}
JSON
STUB
chmod +x "$SCRIPTS_DIR/pre-spawn-check.sh"

cat > "$TEST_DIR/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$TEST_DIR/gh"

cp "$SPAWN_SCRIPT" "$SCRIPTS_DIR/spawn-agent.sh"
SPAWN_COPY="$SCRIPTS_DIR/spawn-agent.sh"

# Patch the copy so REPO_ROOT can be supplied via env — same pattern as
# tests/test_spawn_agent_hook_event_id.sh.
sed -i 's|REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"|REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." \&\& pwd)}"|' \
  "$SPAWN_COPY" 2>/dev/null || true

run_spawn_copy() {
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  STATS_DB_PATH="$STATS_DB" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    bash "$SPAWN_COPY" \
      --role executor \
      --discussion 901 \
      --task-prompt "test task" \
      --isolation worktree \
      2>/dev/null
}

# ── Test 2: canonical fresh-spawn shape gets the honest block ────────────────

echo ""
echo "Test 2: --isolation worktree alone (no --pr, no --worktree-path) is honest"

PROMPT_OUT=$(run_spawn_copy)
RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "spawn exits 0 for the canonical fresh-spawn shape"
else
  fail "spawn exit code" "expected 0, got $RC"
fi

if echo "$PROMPT_OUT" | grep -q "NO WORKTREE WAS PROVISIONED"; then
  fail "false failure claim" "canonical fresh-spawn shape still renders the old hard-fail block"
else
  pass "does not claim NO WORKTREE WAS PROVISIONED for the canonical shape"
fi

if echo "$PROMPT_OUT" | grep -q "Agent tool"; then
  pass "tells the agent the Agent tool provisions the real tree"
else
  fail "honest block missing" "expected a mention of the Agent tool's own provisioning"
fi

if echo "$PROMPT_OUT" | grep -q "worktree_not_provisioned"; then
  pass "still carries the real-failure fallback (isolation truly not applied)"
else
  fail "missing fallback" "expected worktree_not_provisioned fallback to remain present"
fi

# ── Test 3: --pr amend where gh api fails to resolve the head sha ────────────
#
# Review finding on the original D#2222 fix (PR #2231): this case used to
# collapse into the "agent_tool_provisions" reason (same as Test 2), because
# both share "the PR_ARG-and-sha elif branch is false". That is wrong: for a
# --pr amend, telling the agent the Agent tool's auto-provisioned tree is
# fine to proceed in means it silently amends a tree that is NOT the PR's
# branch. This must hard-fail instead. Verified by construction: force the
# gh api call to fail and confirm the resulting prompt tells the agent to stop.

echo ""
echo "Test 3: --pr amend spawn where gh api fails to resolve the head sha hard-fails"

# Overwrite the gh stub so any 'gh api ...' call fails loudly, simulating a
# rate limit / network blip during head-sha resolution.
cat > "$TEST_DIR/gh" <<'GHSTUB'
#!/usr/bin/env bash
if [[ "$1" == "api" ]]; then
  echo "gh: rate limit exceeded (simulated)" >&2
  exit 1
fi
exit 0
GHSTUB
chmod +x "$TEST_DIR/gh"

run_spawn_copy_pr_resolution_failed() {
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  STATS_DB_PATH="$STATS_DB" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    bash "$SPAWN_COPY" \
      --role executor \
      --discussion 902 \
      --pr 902 \
      --task-prompt "test task" \
      --isolation worktree \
      2>/dev/null
}

PR_FAIL_OUT=$(run_spawn_copy_pr_resolution_failed)
PR_FAIL_RC=$?

if [[ "$PR_FAIL_RC" -eq 0 ]]; then
  pass "spawn still exits 0 (the hard-fail is in the rendered prompt, not the spawn itself)"
else
  fail "spawn exit code" "expected 0, got $PR_FAIL_RC"
fi

if echo "$PR_FAIL_OUT" | grep -q "NO WORKTREE WAS PROVISIONED"; then
  pass "renders the hard-fail block for an unresolved --pr amend"
else
  fail "false honest-case claim" "expected NO WORKTREE WAS PROVISIONED, got: $PR_FAIL_OUT"
fi

if echo "$PR_FAIL_OUT" | grep -qi "proceed normally"; then
  fail "unsafe proceed instruction" "--pr resolution failure must never tell the agent to proceed normally"
else
  pass "never tells the agent to proceed normally"
fi

if echo "$PR_FAIL_OUT" | grep -qi "amend"; then
  pass "explains the amend-specific risk (wrong tree, not just 'no tree')"
else
  fail "missing amend risk explanation" "expected a mention of the PR-amend risk"
fi

if echo "$PR_FAIL_OUT" | grep -q "worktree_not_provisioned"; then
  pass "instructs verdict: fail / worktree_not_provisioned, same as a genuine failure"
else
  fail "missing hard-fail instruction" "expected worktree_not_provisioned in the output"
fi

# ── Teardown ──────────────────────────────────────────────────────────────────

rm -rf "$TEST_DIR"

# ── Summary ───────────────────────────────────────────────────────────────────

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
