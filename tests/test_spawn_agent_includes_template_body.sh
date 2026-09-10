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
# D#1985: this suite spawns real spawn-agent.sh processes. On a busy host the
# spawn is refused by the fleet concurrency cap (scripts/spawn-agent.sh:245) —
# unrelated to anything this file is supposed to test — and every check below
# used to swallow that refusal's stderr and report it as a missing-string
# content failure, indistinguishable from a real regression. Two fixes:
#   1. Pass --override-cap on every spawn (below) — this suite never
#      registers a fleet slot (--no-register) and never spawns a real agent,
#      so it has nothing to bypass but the read-only pre-check itself.
#   2. Capture stderr instead of discarding it, and check the exit code
#      before asserting on prompt content — a refusal now reports as a
#      refusal, with the reason, not as a missing string. This also closes a
#      second bug AC3 had on its own: an absence check ("Bash discipline" is
#      NOT present) passes vacuously when a spawn is refused and produces no
#      output at all, so AC3 needs the same exit-code check as AC1/AC2 even
#      though its content assertion runs the opposite direction.
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

# Helper: run spawn-agent.sh for a given role, capturing stdout and stderr
# separately, plus the exit code. Sets LAST_STDOUT / LAST_STDERR / LAST_RC —
# never discards stderr, so a refusal (concurrency cap, pre-spawn-check,
# anything else spawn-agent.sh can fail on) is diagnosable instead of
# silently indistinguishable from "the template lost its content".
#
# --override-cap: this suite is a pure prompt-rendering smoke test — it
# passes --no-register (below) so it never registers a fleet slot and never
# spawns a real agent — so the concurrency cap has nothing of this suite's
# to protect against, and bypassing it here does not touch the live gate
# other callers rely on (scripts/spawn-agent.sh:245 is unchanged).
run_spawn() {
  local role="$1"; shift
  local err_file
  err_file=$(mktemp)
  LAST_STDOUT=$(
    REPO_ROOT="$REPO_ROOT" \
    PATH="$TEST_DIR:$PATH" \
    SPAWN_AGENT_ALLOW_NO_SPEC=1 \
      bash "$SPAWN_COPY" \
        --role "$role" \
        --discussion 999 \
        --task-prompt "test" \
        --no-register \
        --override-cap \
        "$@" \
        2>"$err_file"
  )
  LAST_RC=$?
  LAST_STDERR=$(cat "$err_file")
  rm -f "$err_file"
}

# D#1788: always pass --pr — 5 of the 9 BASH_ROLES below (code-reviewer,
# security-reviewer, docs-writer, runbook-writer, release-manager) reference
# {{pr_number}} and now hard-fail without one. Harmless for the other 4
# (executor, project-manager, incident-commander, impl-coordinator), whose
# templates never reference it.
spawn_role() {
  local role="$1"
  run_spawn "$role" --pr 999
}

# Assert the spawn actually ran before asserting anything about its output.
# A refused spawn (exit != 0, or exit 0 with empty stdout) is reported as
# exactly that — with the captured stderr — rather than being fed into a
# content check that can only produce a misleading "expected X — not found".
# Returns 1 (and records a FAIL) when the spawn did not produce usable
# output; callers must skip their content assertion in that case.
assert_spawned_ok() {
  local label="$1"
  if [[ "$LAST_RC" -ne 0 ]]; then
    local reason="${LAST_STDERR:-(no stderr captured)}"
    fail "$label spawn" "spawn-agent.sh exited $LAST_RC (refused — not a content regression). stderr: $reason"
    return 1
  fi
  if [[ -z "$LAST_STDOUT" ]]; then
    fail "$label spawn" "spawn-agent.sh exited 0 but produced no stdout — cannot assert on prompt content"
    return 1
  fi
  return 0
}

# ── AC1: executor prompt contains "## Bash discipline" ───────────────────────

echo ""
echo "AC1: executor prompt contains '## Bash discipline'"

spawn_role executor

if assert_spawned_ok "executor"; then
  if echo "$LAST_STDOUT" | grep -qF "## Bash discipline"; then
    pass "executor prompt contains '## Bash discipline'"
  else
    fail "executor prompt" "expected '## Bash discipline' — not found"
  fi
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
  spawn_role "$role"
  if assert_spawned_ok "$role"; then
    if echo "$LAST_STDOUT" | grep -qF "## Bash discipline"; then
      pass "$role prompt contains '## Bash discipline'"
    else
      fail "$role prompt" "expected '## Bash discipline' — not found"
    fi
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
run_spawn "$NO_TMPL_ROLE"

# This spawn must still succeed — "no .tmpl file" is not "refused". If we
# skipped this check, a refused spawn (empty stdout) would make the absence
# assertion below pass vacuously and hide the exact same false-regression
# failure mode AC1/AC2 have, just inverted.
if assert_spawned_ok "$NO_TMPL_ROLE"; then
  if echo "$LAST_STDOUT" | grep -qF "## Bash discipline"; then
    fail "$NO_TMPL_ROLE prompt" "expected NO '## Bash discipline' — found spurious injection"
  else
    pass "$NO_TMPL_ROLE prompt has no spurious '## Bash discipline'"
  fi
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
