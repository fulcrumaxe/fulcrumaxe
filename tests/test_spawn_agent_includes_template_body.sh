#!/usr/bin/env bash
# tests/test_spawn_agent_includes_template_body.sh — verify spawn-agent.sh injects .tmpl bodies.
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and stub pre-spawn-check.sh — no real API calls.
#
# What is tested (ACs 1–3 from D#663):
#   AC1. executor prompt contains "## Bash discipline"
#   AC2. Same for each of the 11 Bash-using roles
#   AC3. A role with no .tmpl file produces no spurious "## Bash discipline"
#        (quality-sweep and feedback-scanner have no .tmpl — use one of those)
#
# Usage:
#   bash tests/test_spawn_agent_includes_template_body.sh
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

# Stub rotate-team-log.sh
cat > "$SCRIPTS_DIR/rotate-team-log.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRIPTS_DIR/rotate-team-log.sh"

# Stub pre-spawn-check.sh — always allows, returns minimal JSON
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

# Stub gh to avoid real network calls (PM-gate check reads Discussion body).
# D#1788: spawn-agent.sh's PR-branch resolution runs `gh api repos/.../pulls/<N>
# --jq '[.head.sha, .head.ref] | @tsv'` whenever --pr is given (below). Answer
# that one shape with a fake sha+branch pair so pr_number/pr_url/pr_branch all
# resolve non-empty for the PR-scoped roles in BASH_ROLES — otherwise the
# unconditional `exit 0` (no stdout) reads as a gh api failure and hard-blocks
# docs-writer/runbook-writer (round 3's pr_branch fix). Everything else stays
# a no-op success.
cat > "$TEST_DIR/gh" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "api" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == *"/pulls/"* ]]; then
      printf 'deadbeef\tfeature/test-branch\n'
      exit 0
    fi
  done
fi
exit 0
STUB
chmod +x "$TEST_DIR/gh"

# Stub agent_run_tracker.py (non-fatal, but avoids DuckDB writes in tests)
cat > "$TEST_DIR/agent_run_tracker_stub.py" <<'STUB'
import sys; sys.exit(0)
STUB

# Copy spawn-agent.sh into temp scripts dir so SCRIPT_DIR resolves to SCRIPTS_DIR
cp "$SPAWN_SCRIPT" "$SCRIPTS_DIR/spawn-agent.sh"
SPAWN_COPY="$SCRIPTS_DIR/spawn-agent.sh"

# Patch copy to accept REPO_ROOT override via env var
sed -i 's|REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"|REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." \&\& pwd)}"|' \
  "$SPAWN_COPY" 2>/dev/null || true

# Helper: run spawn-agent.sh for a given role, capture stdout.
# D#1788: always pass --pr — 5 of the 9 BASH_ROLES below (code-reviewer,
# security-reviewer, docs-writer, runbook-writer, release-manager) reference
# {{pr_number}} and now hard-fail without one. Harmless for the other 4
# (executor, project-manager, incident-commander, impl-coordinator), whose
# templates never reference it.
spawn_role() {
  local role="$1"
  REPO_ROOT="$REPO_ROOT" \
  PATH="$TEST_DIR:$PATH" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    bash "$SPAWN_COPY" \
      --role "$role" \
      --discussion 999 \
      --pr 999 \
      --task-prompt "test" \
      --no-register \
      2>/dev/null
}

# ── AC1: executor prompt contains "## Bash discipline" ───────────────────────

echo ""
echo "AC1: executor prompt contains '## Bash discipline'"

PROMPT=$(spawn_role executor)
RC=$?

if [[ $RC -ne 0 ]]; then
  fail "executor spawn exit code" "expected 0, got $RC"
elif echo "$PROMPT" | grep -qF "## Bash discipline"; then
  pass "executor prompt contains '## Bash discipline'"
else
  fail "executor prompt" "expected '## Bash discipline' — not found"
fi

# ── AC2: all 11 Bash-using roles ─────────────────────────────────────────────

echo ""
echo "AC2: all 11 Bash-using roles produce prompts with '## Bash discipline'"

# These are the roles that received ## Bash discipline in PR #660 and are in KNOWN_ROLES.
# browser-tester, run-analyst, and others have extra template vars that require callers
# to pass them; they are NOT in spawn_templates.KNOWN_ROLES so are silently skipped.
BASH_ROLES=(
  executor
  code-reviewer
  security-reviewer
  impl-coordinator
  project-manager
  docs-writer
  incident-commander
  runbook-writer
  release-manager
)

for role in "${BASH_ROLES[@]}"; do
  PROMPT=$(spawn_role "$role" 2>/dev/null || true)
  if echo "$PROMPT" | grep -qF "## Bash discipline"; then
    pass "$role prompt contains '## Bash discipline'"
  else
    fail "$role prompt" "expected '## Bash discipline' — not found"
  fi
done

# ── AC3: role without a .tmpl file does not inject spurious content ───────────

echo ""
echo "AC3: role with no .tmpl file does not inject '## Bash discipline'"

# quality-sweep and feedback-scanner are spawnable roles with no .tmpl file.
# We verify with a role that is accepted by spawn-agent.sh but has no .tmpl.
# Use researcher — it has a .tmpl (so skip it) — use mission-analyst instead
# which has a .tmpl too. Check what roles have no .tmpl:
#   ls backend/spawn_templates/*.tmpl → the list above; all specialist roles
#   quality-sweep, feedback-scanner, visual-verifier have no .tmpl.
# spawn-agent.sh has no role allowlist, so any --role value passes.
NO_TMPL_ROLE="quality-sweep"

# Override the pm-gate check for non-impl roles (quality-sweep is not in the executor|impl-coordinator case)
PROMPT=$(SPAWN_AGENT_ALLOW_NO_SPEC=1 REPO_ROOT="$REPO_ROOT" PATH="$TEST_DIR:$PATH" \
  bash "$SPAWN_COPY" \
    --role "$NO_TMPL_ROLE" \
    --discussion 999 \
    --task-prompt "test" \
    --no-register \
    2>/dev/null || true)

if echo "$PROMPT" | grep -qF "## Bash discipline"; then
  fail "$NO_TMPL_ROLE prompt" "expected NO '## Bash discipline' — found spurious injection"
else
  pass "$NO_TMPL_ROLE prompt has no spurious '## Bash discipline'"
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
