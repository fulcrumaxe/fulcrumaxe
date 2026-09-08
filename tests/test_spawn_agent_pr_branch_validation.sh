#!/usr/bin/env bash
# tests/test_spawn_agent_pr_branch_validation.sh — verify spawn-agent.sh
# validates PR_BRANCH (`.head.ref`) before letting it reach any template.
#
# D#1981: the code plane is now a public repo with forking enabled, so a
# fork PR's head branch name is attacker-chosen. It flows into
# {{pr_branch}} template slots — one of which (docs-writer) is a
# double-quoted shell command an agent is told to run. This suite verifies
# spawn-agent.sh validates PR_BRANCH against the allowlist
# ^[A-Za-z0-9][A-Za-z0-9._/-]*$ (length 1-255) and fails closed:
#   - a role whose template does NOT reference {{pr_branch}} still spawns
#     successfully, with the invalid value silently emptied (never rendered)
#   - a role whose template DOES reference {{pr_branch}} hard-blocks the
#     spawn (exit 1) rather than rendering an empty or malicious string
#   - a valid branch name is untouched and reaches the rendered prompt
#
# Which arm each check covers:
#   AC1     valid-branch pass-through (the happy path is unaffected)
#   AC2     end-to-end: malicious branch never leaks into output for a
#           {{pr_branch}}-using role (does not by itself prove which of the
#           two independent guards — spawn-agent.sh's own hard-block, or
#           prompt_builder's downstream empty-required-var guard — fired;
#           see AC6/AC7)
#   AC3     malicious branch does not block a role that never uses
#           {{pr_branch}} (the value is cleared, not escaped)
#   AC4     negative-shape battery against the hard-block path
#   AC5     mutation check on the validation regex itself
#   AC6     spawn-agent.sh's OWN hard-block arm fires (checked by message,
#           not just exit code — see AC6's comment for why exit code alone
#           is not enough)
#   AC7     mutation check proving AC6 — not just AC2 — actually
#           discriminates that hard-block arm
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and stub gh / pre-spawn-check.sh — no real API
# calls, no network.
#
# Usage:
#   bash tests/test_spawn_agent_pr_branch_validation.sh
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

# ── Setup ─────────────────────────────────────────────────────────────────────

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

SCRIPTS_DIR="$TEST_DIR/scripts"
mkdir -p "$SCRIPTS_DIR"

cat > "$SCRIPTS_DIR/rotate-team-log.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRIPTS_DIR/rotate-team-log.sh"

cat > "$SCRIPTS_DIR/pre-spawn-check.sh" <<'STUB'
#!/usr/bin/env bash
for arg in "$@"; do
  if [[ "${PREV:-}" == "--event-id" ]]; then EVID="$arg"; fi
  PREV="$arg"
done
echo "hook_event_id=${EVID:-test-event}"
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

# Stub gh — the branch name for the fake PR is controlled by
# TEST_FAKE_BRANCH so each case below can drive a different .head.ref
# without a real network call.
cat > "$TEST_DIR/gh" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "api" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == *"/pulls/"* ]]; then
      printf 'deadbeef\t%s\n' "${TEST_FAKE_BRANCH:-feature/test-branch}"
      exit 0
    fi
  done
fi
exit 0
STUB
chmod +x "$TEST_DIR/gh"

cp "$SPAWN_SCRIPT" "$SCRIPTS_DIR/spawn-agent.sh"
SPAWN_COPY="$SCRIPTS_DIR/spawn-agent.sh"

sed -i 's|REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"|REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." \&\& pwd)}"|' \
  "$SPAWN_COPY" 2>/dev/null || true

spawn_role() {
  local role="$1" branch="$2"
  # AUTONOMOUS_TEAM_REPO: makes this test hermetic against a tree that has
  # no .autonomous-team/project.json "repo" field (e.g. a code-plane-only
  # checkout with no Discussion-plane state) — spawn_templates otherwise
  # hard-fails render with "could not resolve a repo slug" before this
  # test's own assertions ever run.
  TEST_FAKE_BRANCH="$branch" \
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
  AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe" \
    bash "$SPAWN_COPY" \
      --role "$role" \
      --discussion 999 \
      --pr 999 \
      --task-prompt "test" \
      --no-register
}

# ── AC1: valid branch name reaches the rendered prompt unchanged ────────────

echo ""
echo "AC1: valid branch name reaches {{pr_branch}} unchanged"

PROMPT=$(spawn_role docs-writer "feature/url-detection" 2>/dev/null)
RC=$?
if [[ $RC -ne 0 ]]; then
  fail "docs-writer valid branch spawn exit code" "expected 0, got $RC"
elif echo "$PROMPT" | grep -qF "feature/url-detection"; then
  pass "valid branch name 'feature/url-detection' rendered into prompt"
else
  fail "valid branch name rendering" "expected 'feature/url-detection' in prompt — not found"
fi

# ── AC2: malicious branch name is refused, spawn hard-blocks for a role ─────
#     that references {{pr_branch}} (docs-writer)

echo ""
echo "AC2: malicious branch name hard-blocks spawn for a {{pr_branch}}-using role"

MALICIOUS='$(whoami)'
OUT=$(spawn_role docs-writer "$MALICIOUS" 2>&1)
RC=$?
if [[ $RC -eq 0 ]]; then
  fail "docs-writer malicious branch spawn exit code" "expected non-zero (hard block), got 0"
else
  pass "docs-writer spawn hard-blocked (exit $RC) on malicious branch name"
fi
if echo "$OUT" | grep -qF "$MALICIOUS"; then
  fail "malicious branch containment" "raw payload '$MALICIOUS' leaked into spawn output"
else
  pass "malicious payload never appears in spawn output"
fi

# ── AC3: malicious branch name does NOT block a role whose template never ───
#     references {{pr_branch}} — it is silently emptied instead
#
# Uses code-reviewer, not executor: executor has its own unrelated
# network-dependent gate (external_docs marker check via a live GraphQL
# read of the Discussion body) that hard-fails in any offline test harness
# regardless of PR_BRANCH — see the D#1981 PR description for detail. That
# gate is orthogonal to this Spec; code-reviewer.tmpl has no {{pr_branch}}
# reference and no such gate, so it isolates the behavior this AC tests.

echo ""
echo "AC3: malicious branch name does not block a role that never uses {{pr_branch}}"

PROMPT=$(spawn_role code-reviewer "$MALICIOUS" 2>/dev/null)
RC=$?
if [[ $RC -ne 0 ]]; then
  fail "code-reviewer malicious branch spawn exit code" "expected 0 (pr_branch unused by this role), got $RC"
else
  pass "code-reviewer spawn succeeds despite malicious branch name (role never renders {{pr_branch}})"
fi
if echo "$PROMPT" | grep -qF "$MALICIOUS"; then
  fail "code-reviewer malicious branch containment" "raw payload leaked into code-reviewer prompt"
else
  pass "malicious payload never reaches code-reviewer prompt"
fi

# ── AC4: a battery of shapes that must each be refused ──────────────────────

echo ""
echo "AC4: each disallowed shape is refused for a {{pr_branch}}-using role"

DISALLOWED=(
  '$(id)'
  '`id`'
  '-rf'
  '--upload-pack=touch /tmp/pwned'
  ''
  "$(printf 'a%.0s' {1..256})"
  'branch;rm -rf /'
  'branch|cat /etc/passwd'
  'a b'
)

for shape in "${DISALLOWED[@]}"; do
  [[ -z "$shape" ]] && continue  # empty branch = API-returned-nothing case, covered elsewhere
  # Use the suite's own private mktemp -d (TEST_DIR), not a fixed /tmp path —
  # a predictable filename under a world-writable directory is a symlink/
  # race target (CWE-377).
  spawn_role docs-writer "$shape" >"$TEST_DIR/spawn_out.log" 2>&1
  RC=$?
  if [[ $RC -eq 0 ]]; then
    fail "disallowed shape '$shape'" "expected hard block (non-zero exit), got 0"
  else
    pass "disallowed shape '$shape' refused (exit $RC)"
  fi
  rm -f "$TEST_DIR/spawn_out.log"
done

# ── AC5: mutation check — prove the validator can fail ──────────────────────
# Temporarily widen the regex to accept everything, confirm AC2 goes red,
# then confirm the real (unmutated) file is what ships.

echo ""
echo "AC5: mutation check — weakening the regex makes AC2 fail (proves the test can fail)"

MUT_COPY="$TEST_DIR/scripts/spawn-agent-mutated.sh"
cp "$SPAWN_COPY" "$MUT_COPY"
# Mutate: replace the allowlist regex with one that accepts anything.
sed -i 's/\^\[A-Za-z0-9\]\[A-Za-z0-9\._\/-\]\*\$/.*/' "$MUT_COPY"
chmod +x "$MUT_COPY"

MUT_OUT=$(TEST_FAKE_BRANCH="$MALICIOUS" REPO_ROOT="$REPO_ROOT" PATH="$TEST_DIR:$PATH" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
  AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe" \
  bash "$MUT_COPY" --role docs-writer --discussion 999 --pr 999 \
    --task-prompt "test" --no-register 2>&1)
MUT_RC=$?

if [[ $MUT_RC -eq 0 ]] && echo "$MUT_OUT" | grep -qF "$MALICIOUS"; then
  pass "mutated (regex=.*) validator lets malicious branch through — confirms test_spawn_agent_pr_branch_validation AC2 is a real, failable check"
else
  fail "mutation check" "expected mutated build to leak '$MALICIOUS' into output (rc=$MUT_RC) — test may not be able to fail"
fi

# ── AC6: the spawn-agent.sh hard-block arm itself fires, distinct from ──────
#     prompt_builder's own independent empty-required-var guard
#
# AC2's exit-code check alone does NOT discriminate this arm: prompt_builder
# (backend/prompt_builder.py) independently refuses to render a template
# that references an unset required var with no RENDER_EMPTY_BY_DESIGN
# excuse — so even with spawn-agent.sh's own
#   if [[ -n "$_PA_API_FAILED" || -n "$_PA_BRANCH_INVALID" ]]; then ... exit 1
# arm deleted, PR_BRANCH is still cleared to "" by the earlier validation
# step, prompt_builder still refuses to render docs-writer's {{pr_branch}}
# as empty, and the process still exits non-zero — just later, and with a
# different message ("prompt_builder: render failed ... resolving to
# empty: pr_branch" instead of "role=$ROLE requires {{pr_branch}}, but ...
# head branch name failed validation"). AC2 cannot tell these two failure
# paths apart. This AC greps for the spawn-agent.sh-specific message, which
# only the hard-block arm itself can produce.

echo ""
echo "AC6: spawn-agent.sh's own hard-block message fires (not just prompt_builder's downstream guard)"

HB_OUT=$(spawn_role docs-writer "$MALICIOUS" 2>&1)
if echo "$HB_OUT" | grep -qF "requires {{pr_branch}}, but PR #999's head branch name failed validation"; then
  pass "spawn-agent.sh's own hard-block message present for docs-writer + invalid branch"
else
  fail "hard-block message" "expected spawn-agent.sh's own 'head branch name failed validation' block message — not found (may be relying solely on prompt_builder's downstream guard)"
fi

# ── AC7: mutation check for AC6 — prove AC6 actually discriminates the ──────
#     hard-block arm, not just overall exit-code failure
#
# Mutate spawn-agent.sh's hard-block condition back to only checking
# _PA_API_FAILED (i.e. delete the `|| -n "$_PA_BRANCH_INVALID"` disjunct —
# the actual code this PR adds to the pre-existing hard-block gate). The
# earlier validation step that clears PR_BRANCH to "" on an invalid name is
# untouched by this mutation, so the overall spawn still fails (via
# prompt_builder's guard) — proving AC2's exit-code-only check would stay
# green under this mutation. AC6's message grep must go red.

echo ""
echo "AC7: mutation check — removing the hard-block OR-arm makes AC6 fail (but not AC2)"

MUT2_COPY="$TEST_DIR/scripts/spawn-agent-mutated-hardblock.sh"
cp "$SPAWN_COPY" "$MUT2_COPY"
sed -i 's/if \[\[ -n "\$_PA_API_FAILED" || -n "\$_PA_BRANCH_INVALID" \]\]; then/if [[ -n "$_PA_API_FAILED" ]]; then/' "$MUT2_COPY"
chmod +x "$MUT2_COPY"

MUT2_OUT=$(TEST_FAKE_BRANCH="$MALICIOUS" REPO_ROOT="$REPO_ROOT" PATH="$TEST_DIR:$PATH" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
  AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe" \
  bash "$MUT2_COPY" --role docs-writer --discussion 999 --pr 999 \
    --task-prompt "test" --no-register 2>&1)
MUT2_RC=$?

if [[ $MUT2_RC -ne 0 ]] && ! echo "$MUT2_OUT" | grep -qF "requires {{pr_branch}}, but PR #999's head branch name failed validation"; then
  pass "mutated (hard-block OR-arm removed) build still exits non-zero (AC2-style check would stay green) but AC6's specific message is gone — confirms AC6 is a real, failable check for the hard-block arm"
else
  fail "AC6 mutation check" "expected mutated build to still fail (rc=$MUT2_RC) but WITHOUT the hard-block message — AC6 may not be discriminating"
fi

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
