#!/usr/bin/env bash
# tests/test_spawn_enforcement_gates.sh
#
# Tests for the 4 enforcement gates added in D#874 Sub-PR 3:
#
#   1. Concurrency cap: 5 active executors → spawn refuses
#   2. (retired: impl-coordinator block removed — role retired in D#899)
#   3. Runaway loop hook: 'until x; do sleep 5; done' → blocked
#   4. --repo warning: 'gh api foo' without --repo → warns (exit 0)
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and stubs — no real GitHub API calls.
#
# Usage:
#   bash tests/test_spawn_enforcement_gates.sh
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

# ── Setup shared stubs ────────────────────────────────────────────────────────

TEST_DIR=$(mktemp -d)
SCRIPTS_DIR="$TEST_DIR/scripts"
mkdir -p "$SCRIPTS_DIR"
mkdir -p "$TEST_DIR/backend"
mkdir -p "$TEST_DIR/.autonomous-team"
mkdir -p "$TEST_DIR/lib"

# Stub rotate-team-log.sh
cat > "$SCRIPTS_DIR/rotate-team-log.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRIPTS_DIR/rotate-team-log.sh"

# Stub setup-state-dir.sh
cat > "$SCRIPTS_DIR/setup-state-dir.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRIPTS_DIR/setup-state-dir.sh"

# Stub lib/gh-token.sh
mkdir -p "$SCRIPTS_DIR/lib"
cat > "$SCRIPTS_DIR/lib/gh-token.sh" <<'STUB'
#!/usr/bin/env bash
# stub: no-op
STUB

# Allow stub for pre-spawn-check.sh
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

# Stub post-agent-hook.sh
cat > "$SCRIPTS_DIR/post-agent-hook.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRIPTS_DIR/post-agent-hook.sh"

# Stub agent_run_tracker.py
cat > "$TEST_DIR/backend/agent_run_tracker.py" <<'STUB'
import sys
sys.exit(0)
STUB

# Stub control_plane.py — returns default caps
cat > "$TEST_DIR/backend/control_plane.py" <<'STUB'
import sys
key = sys.argv[2] if len(sys.argv) > 2 else ""
if key == "policies.team_lead.concurrency_cap_executors":
    print("4")
    sys.exit(0)
elif key == "policies.team_lead.concurrency_cap_total":
    print("8")
    sys.exit(0)
elif key.startswith("gates.") or key.startswith("policies."):
    sys.exit(1)
sys.exit(1)
STUB

# Stub discussion_cache.py — discussion always has SPEC_READY
cat > "$TEST_DIR/backend/discussion_cache.py" <<'STUB'
import sys
print("STATUS: SPEC_READY\n\nTest discussion body.")
sys.exit(0)
STUB

# Stub spawn_templates.py
cat > "$TEST_DIR/backend/spawn_templates.py" <<'STUB'
import sys
# No templates to render — exit 1 so TMPL_FILE check handles it
sys.exit(1)
STUB

# Stub gh to avoid real network calls
cat > "$TEST_DIR/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$TEST_DIR/gh"

# Copy and patch spawn-agent.sh to use TEST_DIR as REPO_ROOT
cp "$SPAWN_SCRIPT" "$SCRIPTS_DIR/spawn-agent.sh"
SPAWN_COPY="$SCRIPTS_DIR/spawn-agent.sh"
# Allow REPO_ROOT override via environment
sed -i 's|REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"|REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." \&\& pwd)}"|' \
  "$SPAWN_COPY" 2>/dev/null || true

# ── Test 1: Concurrency cap — 5 active executors should block ─────────────────
#
# Strategy: override the DuckDB inline Python by injecting a fake duckdb module
# via PYTHONPATH. The fake always returns 5 active executors so the cap check fires.

echo ""
echo "Test 1: Concurrency cap blocks when executor count >= 4"

# Fake duckdb shim in PYTHONPATH
SHIM_DIR="$TEST_DIR/pyshim"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/duckdb.py" <<'PYSHIM'
class Connection:
    def execute(self, query):
        return self
    def fetchall(self):
        return [("executor", 5), ("code-reviewer", 1)]
    def close(self):
        pass

def connect(path, read_only=False):
    return Connection()
PYSHIM

# Create fake stats.duckdb at the path the code expects: $REPO_ROOT/.autonomous-team/stats.duckdb
touch "$TEST_DIR/.autonomous-team/stats.duckdb"

# Run spawn — expect it to be blocked by concurrency cap
OUT=$(REPO_ROOT="$TEST_DIR" \
  PYTHONPATH="$SHIM_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  PATH="$TEST_DIR:$SCRIPTS_DIR:$PATH" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
  bash "$SPAWN_COPY" \
    --role executor \
    --discussion 874 \
    --task-prompt "test" 2>&1)
RC=$?

if echo "$OUT" | grep -qi "concurrency cap"; then
  pass "concurrency cap message in stderr"
else
  fail "concurrency cap" "expected 'concurrency cap' in output, got: $OUT"
fi

if [[ "$RC" -ne 0 ]]; then
  pass "spawn exits non-zero on cap block"
else
  fail "spawn exit code on cap" "expected non-zero, got 0. Output: $OUT"
fi

# Test 1b: --override-cap bypasses the check and proceeds past the cap gate
OUT2=$(REPO_ROOT="$TEST_DIR" \
  PYTHONPATH="$SHIM_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  PATH="$TEST_DIR:$SCRIPTS_DIR:$PATH" \
  SPAWN_AGENT_ALLOW_NO_SPEC=1 \
  OVERRIDE_CAP=1 \
  bash "$SPAWN_COPY" \
    --role executor \
    --discussion 874 \
    --task-prompt "test" 2>&1)
RC2=$?

# With --override-cap, the cap check is bypassed; spawn may succeed or fail for
# other reasons (missing template, etc.) — what matters is no cap block message
if echo "$OUT2" | grep -qi "concurrency cap"; then
  fail "--override-cap bypass" "cap block message still appeared with --override-cap"
else
  pass "--override-cap bypasses the concurrency cap check"
fi

# ── Test 3: Runaway loop hook blocks 'until x; do sleep N; done' ─────────────

echo ""
echo "Test 3: runaway_loop_guard.py blocks until...sleep pattern"

HOOK_SCRIPT="$REPO_ROOT/hooks/runaway_loop_guard.py"

if [[ ! -f "$HOOK_SCRIPT" ]]; then
  fail "runaway_loop_guard exists" "file not found: $HOOK_SCRIPT"
else
  pass "runaway_loop_guard.py exists"

  # Test 3a: blocked pattern — capture exit code explicitly (don't use || true)
  PAYLOAD='{"tool_name":"Bash","tool_input":{"command":"until x; do sleep 5; done"},"cwd":"/tmp"}'
  ERR_OUT=$(echo "$PAYLOAD" | python3 "$HOOK_SCRIPT" 2>&1) && HOOK_RC=0 || HOOK_RC=$?

  if [[ "$HOOK_RC" -eq 2 ]]; then
    pass "until...sleep pattern exits 2 (block)"
  else
    fail "runaway_loop_guard block" "expected exit 2, got $HOOK_RC. Output: $ERR_OUT"
  fi

  if echo "$ERR_OUT" | grep -qi "BLOCKED"; then
    pass "runaway_loop_guard outputs BLOCKED message"
  else
    fail "runaway_loop_guard message" "expected BLOCKED in output, got: $ERR_OUT"
  fi

  # Test 3b: safe for-loop not blocked
  SAFE='{"tool_name":"Bash","tool_input":{"command":"for i in 1 2 3; do echo $i; done"},"cwd":"/tmp"}'
  echo "$SAFE" | python3 "$HOOK_SCRIPT" 2>/dev/null && SAFE_RC=0 || SAFE_RC=$?
  if [[ "$SAFE_RC" -eq 0 ]]; then
    pass "safe for-loop not blocked"
  else
    fail "runaway_loop_guard false positive" "safe loop should not be blocked (exit $SAFE_RC)"
  fi

  # Test 3c: non-Bash tool is allowed
  NON_BASH='{"tool_name":"Read","tool_input":{"file_path":"/tmp/x"},"cwd":"/tmp"}'
  echo "$NON_BASH" | python3 "$HOOK_SCRIPT" 2>/dev/null && NB_RC=0 || NB_RC=$?
  if [[ "$NB_RC" -eq 0 ]]; then
    pass "non-Bash tool allowed by runaway_loop_guard"
  else
    fail "runaway_loop_guard non-Bash" "non-Bash tool should exit 0, got $NB_RC"
  fi

  # Test 3d: variant with different command word
  VARIANT='{"tool_name":"Bash","tool_input":{"command":"until check_done; do sleep 10; done"},"cwd":"/tmp"}'
  echo "$VARIANT" | python3 "$HOOK_SCRIPT" 2>/dev/null && VAR_RC=0 || VAR_RC=$?
  if [[ "$VAR_RC" -eq 2 ]]; then
    pass "multi-word until...sleep pattern also blocked"
  else
    fail "runaway_loop_guard variant" "expected exit 2 for multi-word until, got $VAR_RC"
  fi
fi

# ── Test 4: repo_scope_warn.py warns on gh call without --repo ────────────────

echo ""
echo "Test 4: repo_scope_warn.py warns on gh api/pr/issue without --repo"

WARN_HOOK="$REPO_ROOT/hooks/repo_scope_warn.py"

if [[ ! -f "$WARN_HOOK" ]]; then
  fail "repo_scope_warn exists" "file not found: $WARN_HOOK"
else
  pass "repo_scope_warn.py exists"

  # Test 4a: warns on gh api without --repo (exit 0 — warn only)
  PAYLOAD='{"tool_name":"Bash","tool_input":{"command":"gh api repos/foo/bar"},"cwd":"/tmp"}'
  WARN_OUT=$(echo "$PAYLOAD" | python3 "$WARN_HOOK" 2>&1) && WARN_RC=0 || WARN_RC=$?

  if [[ "$WARN_RC" -eq 0 ]]; then
    pass "repo_scope_warn exits 0 (warns only, never blocks)"
  else
    fail "repo_scope_warn exit code" "expected 0, got $WARN_RC"
  fi

  if echo "$WARN_OUT" | grep -qi "WARN"; then
    pass "repo_scope_warn outputs WARN for gh api without --repo"
  else
    fail "repo_scope_warn warning" "expected WARN in output, got: $WARN_OUT"
  fi

  # Test 4b: no warning when --repo is present
  GOOD='{"tool_name":"Bash","tool_input":{"command":"gh api repos/foo/bar --repo autonomous-agent-7/autonomous-forever"},"cwd":"/tmp"}'
  GOOD_OUT=$(echo "$GOOD" | python3 "$WARN_HOOK" 2>&1) && GOOD_RC=0 || GOOD_RC=$?

  if [[ "$GOOD_RC" -eq 0 ]]; then
    pass "repo_scope_warn exits 0 when --repo present"
  else
    fail "repo_scope_warn with --repo" "expected 0, got $GOOD_RC"
  fi

  if echo "$GOOD_OUT" | grep -qi "WARN"; then
    fail "repo_scope_warn false positive" "should not warn when --repo is present. Output: $GOOD_OUT"
  else
    pass "repo_scope_warn no warning when --repo present"
  fi

  # Test 4c: no warning on non-Bash tool
  NON_BASH='{"tool_name":"Read","tool_input":{"file_path":"/tmp/x"},"cwd":"/tmp"}'
  echo "$NON_BASH" | python3 "$WARN_HOOK" 2>/dev/null && NB_RC=0 || NB_RC=$?
  if [[ "$NB_RC" -eq 0 ]]; then
    pass "repo_scope_warn allows non-Bash tool"
  else
    fail "repo_scope_warn non-Bash" "non-Bash should exit 0, got $NB_RC"
  fi

  # Test 4d: warns on gh pr without --repo
  PR_PAYLOAD='{"tool_name":"Bash","tool_input":{"command":"gh pr list --state open"},"cwd":"/tmp"}'
  PR_OUT=$(echo "$PR_PAYLOAD" | python3 "$WARN_HOOK" 2>&1) && PR_RC=0 || PR_RC=$?

  if [[ "$PR_RC" -eq 0 ]]; then
    pass "repo_scope_warn exits 0 for gh pr without --repo"
  else
    fail "repo_scope_warn pr exit code" "expected 0, got $PR_RC"
  fi

  if echo "$PR_OUT" | grep -qi "WARN"; then
    pass "repo_scope_warn warns on gh pr without --repo"
  else
    fail "repo_scope_warn pr warning" "expected WARN for gh pr without --repo. Output: $PR_OUT"
  fi
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
