#!/usr/bin/env bash
# tests/test_spawn_agent_file_scope.sh
#
# Tests for the --touchpoints file-scope claim gate in scripts/spawn-agent.sh.
#
# Scenarios:
#   1. PR conflict: open PR touches foo.py; spawn with --touchpoints foo.py → blocked
#   2. Worktree conflict: live worktree has uncommitted foo.py; --touchpoints foo.py → blocked
#   3. No conflict: --touchpoints foo.py, no PR/worktree touches it → proceeds
#   4. Empty touchpoints: legacy spawn without --touchpoints → proceeds with warning
#   5. Nested path: --touchpoints backend/foo.py works the same way
#
# HARD RULE: never invoke claude, _start_loop_run, or /loop here.
# All tests use stubs — no real GitHub API calls, no real worktree mutations.
#
# Usage:
#   bash tests/test_spawn_agent_file_scope.sh
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
mkdir -p "$SCRIPTS_DIR/lib"
mkdir -p "$TEST_DIR/backend"
mkdir -p "$TEST_DIR/.autonomous-team"

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
cat > "$SCRIPTS_DIR/lib/gh-token.sh" <<'STUB'
#!/usr/bin/env bash
# stub: no-op
STUB

# Stub pre-spawn-check.sh (always allows)
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

# Stub control_plane.py — returns default values
cat > "$TEST_DIR/backend/control_plane.py" <<'STUB'
import sys
key = sys.argv[2] if len(sys.argv) > 2 else ""
if key == "policies.team_lead.concurrency_cap_executors":
    print("4")
elif key == "policies.team_lead.concurrency_cap_total":
    print("8")
else:
    sys.exit(1)
sys.exit(0)
STUB

# Stub discussion_cache.py — always SPEC_READY
cat > "$TEST_DIR/backend/discussion_cache.py" <<'STUB'
import sys
print("STATUS: SPEC_READY\n\nTest discussion body.")
sys.exit(0)
STUB

# Stub spawn_templates.py
cat > "$TEST_DIR/backend/spawn_templates.py" <<'STUB'
import sys
sys.exit(1)
STUB

# Copy and patch spawn-agent.sh to use TEST_DIR as REPO_ROOT
cp "$SPAWN_SCRIPT" "$SCRIPTS_DIR/spawn-agent.sh"
SPAWN_COPY="$SCRIPTS_DIR/spawn-agent.sh"
sed -i 's|REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"|REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." \&\& pwd)}"|' \
  "$SPAWN_COPY"

# Helper: run spawn with overridden gh and git stubs
# $1 = gh stub script content
# $2 = git stub script content (for worktree list / diff)
# remaining args = spawn-agent.sh args
run_spawn() {
  local gh_stub="$1"
  local git_stub="$2"
  shift 2

  local stub_bin="$TEST_DIR/stubs-$$"
  mkdir -p "$stub_bin"

  printf '%s' "$gh_stub" > "$stub_bin/gh"
  chmod +x "$stub_bin/gh"

  printf '%s' "$git_stub" > "$stub_bin/git"
  chmod +x "$stub_bin/git"

  local out rc
  out=$(REPO_ROOT="$TEST_DIR" \
    PATH="$stub_bin:$TEST_DIR:$SCRIPTS_DIR:$PATH" \
    SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    OVERRIDE_CAP=1 \
    SPAWN_AGENT_SKIP_EXIT_TRAP=1 \
    bash "$SPAWN_COPY" "$@" 2>&1) && rc=0 || rc=$?

  rm -rf "$stub_bin"
  echo "$out"
  return $rc
}

# ── Test 1: PR conflict ───────────────────────────────────────────────────────
echo ""
echo "Test 1: --touchpoints foo.py blocked when open PR claims foo.py"

GH_PR_CONFLICT='#!/usr/bin/env bash
# gh stub: returns PR #42 touching foo.py
if [[ "$*" == *"--json number,files"* ]]; then
  echo "foo.py PR#42"
fi
exit 0'

GIT_NO_WT='#!/usr/bin/env bash
# git stub: no live worktrees with changes
if [[ "$1" == "worktree" && "$2" == "list" ]]; then
  echo "worktree /fake/main"
  echo "HEAD abc123"
  echo "branch refs/heads/main"
fi
exit 0'

OUT=$(run_spawn "$GH_PR_CONFLICT" "$GIT_NO_WT" \
  --role executor --discussion 897 --task-prompt "test" \
  --touchpoints "foo.py") && RC=0 || RC=$?

if echo "$OUT" | grep -qi "CONFLICT"; then
  pass "PR conflict blocked with CONFLICT message"
else
  fail "PR conflict" "expected CONFLICT in output, got: $OUT"
fi

if [[ "$RC" -ne 0 ]]; then
  pass "spawn exits non-zero on PR conflict"
else
  fail "spawn exit code on PR conflict" "expected non-zero, got 0"
fi

# ── Test 2: Worktree conflict ─────────────────────────────────────────────────
echo ""
echo "Test 2: --touchpoints foo.py blocked when live worktree claims foo.py"

GH_NO_PRS='#!/usr/bin/env bash
# gh stub: no open PRs claim foo.py
if [[ "$*" == *"--json number,files"* ]]; then
  echo ""
fi
exit 0'

GIT_WT_CONFLICT='#!/usr/bin/env bash
# git stub: worktree agent-abc has foo.py changed vs origin/main
if [[ "$1" == "worktree" && "$2" == "list" ]]; then
  echo "worktree /fake/main"
  echo "HEAD abc123"
  echo "branch refs/heads/main"
  echo ""
  echo "worktree /fake/.claude/worktrees/agent-abc"
  echo "HEAD def456"
  echo "branch refs/heads/worktree-agent-abc"
  exit 0
fi
# git -C <path> diff --name-only origin/main
if [[ "$1" == "-C" && "${3:-}" == "diff" ]]; then
  echo "foo.py"
  exit 0
fi
exit 0'

OUT=$(run_spawn "$GH_NO_PRS" "$GIT_WT_CONFLICT" \
  --role executor --discussion 897 --task-prompt "test" \
  --touchpoints "foo.py") && RC=0 || RC=$?

if echo "$OUT" | grep -qi "CONFLICT"; then
  pass "worktree conflict blocked with CONFLICT message"
else
  fail "worktree conflict" "expected CONFLICT in output, got: $OUT"
fi

if [[ "$RC" -ne 0 ]]; then
  pass "spawn exits non-zero on worktree conflict"
else
  fail "spawn exit code on worktree conflict" "expected non-zero, got 0"
fi

# ── Test 3: No conflict — proceeds ───────────────────────────────────────────
echo ""
echo "Test 3: --touchpoints foo.py proceeds when no PR or worktree claims it"

GH_OTHER_FILES='#!/usr/bin/env bash
# gh stub: open PR touches bar.py, not foo.py
if [[ "$*" == *"--json number,files"* ]]; then
  echo "bar.py PR#10"
fi
exit 0'

GIT_OTHER_WT='#!/usr/bin/env bash
# git stub: worktree claims bar.py, not foo.py
if [[ "$1" == "worktree" && "$2" == "list" ]]; then
  echo "worktree /fake/main"
  echo "HEAD abc123"
  echo "branch refs/heads/main"
  exit 0
fi
exit 0'

OUT=$(run_spawn "$GH_OTHER_FILES" "$GIT_OTHER_WT" \
  --role executor --discussion 897 --task-prompt "test" \
  --touchpoints "foo.py") && RC=0 || RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "no conflict — spawn proceeds (exit 0)"
else
  fail "no conflict" "expected exit 0 but got $RC. Output: $OUT"
fi

if echo "$OUT" | grep -qi "CONFLICT"; then
  fail "no conflict false positive" "CONFLICT appeared when there should be none. Output: $OUT"
else
  pass "no CONFLICT message when paths don't overlap"
fi

# ── Test 4: Empty touchpoints — warning but proceeds ─────────────────────────
echo ""
echo "Test 4: legacy spawn without --touchpoints proceeds with warning"

GH_NOOP='#!/usr/bin/env bash
exit 0'

GIT_NOOP='#!/usr/bin/env bash
exit 0'

OUT=$(run_spawn "$GH_NOOP" "$GIT_NOOP" \
  --role executor --discussion 897 --task-prompt "test") && RC=0 || RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "legacy spawn without --touchpoints exits 0"
else
  fail "legacy spawn exit code" "expected 0, got $RC. Output: $OUT"
fi

if echo "$OUT" | grep -qi "WARN.*touchpoints\|touchpoints.*WARN"; then
  pass "legacy spawn emits warning about missing touchpoints"
else
  fail "legacy spawn warning" "expected WARN about touchpoints in output, got: $OUT"
fi

# ── Test 5: Nested path — backend/foo.py ────────────────────────────────────
echo ""
echo "Test 5: nested path --touchpoints backend/foo.py blocked on PR overlap"

GH_NESTED='#!/usr/bin/env bash
if [[ "$*" == *"--json number,files"* ]]; then
  echo "backend/foo.py PR#55"
fi
exit 0'

GIT_CLEAN='#!/usr/bin/env bash
if [[ "$1" == "worktree" && "$2" == "list" ]]; then
  echo "worktree /fake/main"
  echo "HEAD abc123"
  echo "branch refs/heads/main"
  exit 0
fi
exit 0'

OUT=$(run_spawn "$GH_NESTED" "$GIT_CLEAN" \
  --role executor --discussion 897 --task-prompt "test" \
  --touchpoints "backend/foo.py") && RC=0 || RC=$?

if echo "$OUT" | grep -qi "CONFLICT"; then
  pass "nested path backend/foo.py conflict detected"
else
  fail "nested path" "expected CONFLICT for backend/foo.py, got: $OUT"
fi

if [[ "$RC" -ne 0 ]]; then
  pass "spawn exits non-zero on nested path conflict"
else
  fail "spawn exit code on nested path" "expected non-zero, got 0"
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
