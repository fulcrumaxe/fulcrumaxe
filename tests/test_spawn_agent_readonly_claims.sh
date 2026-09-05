#!/usr/bin/env bash
# tests/test_spawn_agent_readonly_claims.sh
#
# D#2153: makes the --touchpoints file-scope claim gate in
# scripts/spawn-agent.sh (section 0c) role-aware, via scripts/lib/role-capabilities.sh
# and the `read_only: true` frontmatter declaration on a role card.
#
# This suite is deliberately a NEW file, not an edit to
# tests/test_spawn_agent_file_scope.sh: that suite is a D#2152 denylist entry
# (BASH_SUITE_DENYLIST in scripts/run-pr-tests.sh), and D#2152/D#2153 must
# share no files. Modeled on that suite's stub harness (mktemp tree, stubbed
# gh/git/rotate-team-log.sh/pre-spawn-check.sh, no real API calls, no real
# worktree mutation) — see its header for the stubbing conventions this
# copies.
#
# Scenarios (Spec Acceptance item 1):
#   (a) discriminating case — read-only role, --touchpoints names a claimed
#       file: exit 0, no CONFLICT:, no "--touchpoints not set" warning.
#   (b) writers still blocked — --role executor, same touchpoints: blocked.
#   (c) legacy warning intact — read-only role, no --touchpoints: WARN.
#   (d) undeclared role defaults to writer — no read_only: field: blocked.
#   (e) no overlap, read-only role: exit 0, no CONFLICT:, no NOTE:.
# Plus:
#   - role-capabilities.sh CLI (Spec item 2)
#   - drift guard: no read-only role name hardcoded in section 0c (item 4)
#   - --touchpoints still forwarded to pre-spawn-check.sh on a read-only
#     spawn (item 5)
#
# HARD RULE: never invoke claude, _start_loop_run, or /loop here.
# All tests use stubs — no real GitHub API calls, no real worktree mutations.
#
# Usage:
#   bash tests/test_spawn_agent_readonly_claims.sh
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

# ── Test: item 2 — role-capabilities.sh CLI ──────────────────────────────────
# Runs against the REAL repo's role cards (the ones this PR declared), not a
# stub tree — this is exactly the command the Spec names.
echo ""
echo "Test: role-capabilities.sh is-read-only CLI"

bash "$REPO_ROOT/scripts/lib/role-capabilities.sh" is-read-only security-reviewer >/dev/null 2>&1
RC=$?
if [[ "$RC" -eq 0 ]]; then
  pass "is-read-only security-reviewer prints 0"
else
  fail "is-read-only security-reviewer" "expected exit 0, got $RC"
fi

bash "$REPO_ROOT/scripts/lib/role-capabilities.sh" is-read-only executor >/dev/null 2>&1
RC=$?
if [[ "$RC" -ne 0 ]]; then
  pass "is-read-only executor prints non-zero"
else
  fail "is-read-only executor" "expected non-zero, got 0"
fi

bash "$REPO_ROOT/scripts/lib/role-capabilities.sh" is-read-only no-such-role-card >/dev/null 2>&1
RC=$?
if [[ "$RC" -ne 0 ]]; then
  pass "is-read-only <role with no card> prints non-zero"
else
  fail "is-read-only <role with no card>" "expected non-zero, got 0"
fi

# ── Test: item 4 — drift guard, no role name hardcoded in section 0c ────────
echo ""
echo "Test: role names are not hardcoded inside the claim-gate block (section 0c)"

_0C_BLOCK=$(sed -n '/── 0c\. File-scope claim gate/,/── 0d\. Ensure team substrate exists/p' "$SPAWN_SCRIPT")
if [[ -z "$_0C_BLOCK" ]]; then
  fail "drift guard extraction" "could not locate section 0c in $SPAWN_SCRIPT — markers may have moved"
elif echo "$_0C_BLOCK" | grep -qE 'security-reviewer|code-reviewer|accessibility-reviewer|debater|run-analyst|researcher|technical-architect|product-owner|cost-analyst|performance-expert|security-expert'; then
  fail "drift guard" "a read-only role name is hardcoded inside section 0c — the read-only set must live only in role-card frontmatter"
else
  pass "no read-only role name appears inside section 0c"
fi

# ── Test: item 3 — every declared card carries read_only: true in-window ────
echo ""
echo "Test: all eleven role cards declare read_only: true within the first 512 bytes"

_MISSING=""
for _r in code-reviewer security-reviewer accessibility-reviewer debater run-analyst researcher technical-architect product-owner cost-analyst performance-expert security-expert; do
  head -c 512 "$REPO_ROOT/.claude/agents/$_r.md" 2>/dev/null | grep -q '^read_only: true' || _MISSING="$_MISSING $_r"
done
if [[ -z "$_MISSING" ]]; then
  pass "all eleven role cards declare read_only: true"
else
  fail "role card declarations" "missing read_only: true on:$_MISSING"
fi

# ── Setup shared stubs for the spawn-agent.sh integration cases ─────────────

TEST_DIR=$(mktemp -d)
SCRIPTS_DIR="$TEST_DIR/scripts"
mkdir -p "$SCRIPTS_DIR/lib"
mkdir -p "$TEST_DIR/backend"
mkdir -p "$TEST_DIR/.autonomous-team"
mkdir -p "$TEST_DIR/.claude/agents"

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

# The claim gate (0c) sources these directly by relative path from
# $SCRIPT_DIR/lib — copy the REAL modules under test rather than stubbing
# them, so this suite exercises the actual production logic (role-capabilities.sh,
# worktree-claims.sh) against stubbed gh/git, not a re-implementation of it.
cp "$REPO_ROOT/scripts/lib/repo-resolve.sh" "$SCRIPTS_DIR/lib/repo-resolve.sh"
cp "$REPO_ROOT/scripts/lib/worktree-claims.sh" "$SCRIPTS_DIR/lib/worktree-claims.sh"
cp "$REPO_ROOT/scripts/lib/role-capabilities.sh" "$SCRIPTS_DIR/lib/role-capabilities.sh"
[[ -f "$REPO_ROOT/scripts/lib/state-dir.sh" ]] && cp "$REPO_ROOT/scripts/lib/state-dir.sh" "$SCRIPTS_DIR/lib/state-dir.sh"

# Stub pre-spawn-check.sh (always allows). Also logs the args it received to
# $PSC_ARGS_LOG when that env var is set, so item 5 can assert --touchpoints
# forwarding without a real dial-class derivation call.
cat > "$SCRIPTS_DIR/pre-spawn-check.sh" <<'STUB'
#!/usr/bin/env bash
if [[ -n "${PSC_ARGS_LOG:-}" ]]; then
  printf '%s\n' "$*" >> "$PSC_ARGS_LOG"
fi
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

# Stub role cards.
#   security-reviewer — declares read_only: true (the discriminating role)
#   executor          — no read_only: field (the real repo's writer role)
#   stub-new-role     — no read_only: field at all (Decision 3: undeclared ->
#                       write-capable; a role distinct from executor so this
#                       is genuinely testing "absent means writer", not just
#                       re-testing executor's own behaviour)
cat > "$TEST_DIR/.claude/agents/security-reviewer.md" <<'STUB'
---
name: security-reviewer
description: stub
model: opus
tier: premium
read_only: true
---
STUB

cat > "$TEST_DIR/.claude/agents/executor.md" <<'STUB'
---
name: executor
description: stub
model: sonnet
tier: mid
---
STUB

cat > "$TEST_DIR/.claude/agents/stub-new-role.md" <<'STUB'
---
name: stub-new-role
description: stub — an undeclared role, no read_only field
model: sonnet
---
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
    AUTONOMOUS_TEAM_REPO="test-org/test-repo" \
    PATH="$stub_bin:$TEST_DIR:$SCRIPTS_DIR:$PATH" \
    SPAWN_AGENT_ALLOW_NO_SPEC=1 \
    OVERRIDE_CAP=1 \
    SPAWN_AGENT_SKIP_EXIT_TRAP=1 \
    PSC_ARGS_LOG="${PSC_ARGS_LOG:-}" \
    bash "$SPAWN_COPY" "$@" 2>&1) && rc=0 || rc=$?

  rm -rf "$stub_bin"
  echo "$out"
  return $rc
}

# A PR claims foo.py (used by cases a, b, d)
GH_PR_CONFLICT='#!/usr/bin/env bash
if [[ "$*" == *"--json number,files"* ]]; then
  echo "foo.py PR#42"
fi
exit 0'

# No open PR claims anything (used by case e)
GH_NO_PRS='#!/usr/bin/env bash
if [[ "$*" == *"--json number,files"* ]]; then
  echo ""
fi
exit 0'

GIT_NO_WT='#!/usr/bin/env bash
if [[ "$1" == "worktree" && "$2" == "list" ]]; then
  echo "worktree /fake/main"
  echo "HEAD abc123"
  echo "branch refs/heads/main"
fi
exit 0'

# ── (a) discriminating case ──────────────────────────────────────────────────
echo ""
echo "Test (a): read-only role with --touchpoints on a claimed file — no block"

OUT=$(run_spawn "$GH_PR_CONFLICT" "$GIT_NO_WT" \
  --role security-reviewer --discussion 2153 --task-prompt "test" --pr 42 \
  --touchpoints "foo.py") && RC=0 || RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "(a) read-only spawn with claimed touchpoint exits 0"
else
  fail "(a) exit code" "expected 0, got $RC. Output: $OUT"
fi
if echo "$OUT" | grep -q "CONFLICT:"; then
  fail "(a) no CONFLICT" "CONFLICT: line present when it must not block a read-only role. Output: $OUT"
else
  pass "(a) no CONFLICT: line"
fi
if echo "$OUT" | grep -q "file-scope conflict detection skipped"; then
  fail "(a) no skip warning" "--touchpoints not set warning fired even though --touchpoints was passed. Output: $OUT"
else
  pass "(a) no '--touchpoints not set' warning"
fi
# item 6: overlap still reported, one NOTE: line naming the file and holder
if echo "$OUT" | grep -q "NOTE:.*foo.py.*PR#42"; then
  pass "(a) NOTE: line names the file and claim holder"
else
  fail "(a) NOTE: line" "expected a NOTE: line naming foo.py and PR#42. Output: $OUT"
fi

# ── (b) writers still blocked ────────────────────────────────────────────────
echo ""
echo "Test (b): write-capable role (executor) with the same touchpoints is still blocked"

OUT=$(run_spawn "$GH_PR_CONFLICT" "$GIT_NO_WT" \
  --role executor --discussion 2153 --task-prompt "test" \
  --touchpoints "foo.py") && RC=0 || RC=$?

if [[ "$RC" -ne 0 ]]; then
  pass "(b) executor spawn exits non-zero on conflict"
else
  fail "(b) exit code" "expected non-zero, got 0. Output: $OUT"
fi
if echo "$OUT" | grep -q "CONFLICT:"; then
  pass "(b) CONFLICT: line present for a writer"
else
  fail "(b) CONFLICT: line" "expected CONFLICT: in output, got: $OUT"
fi

# ── (c) legacy warning intact ────────────────────────────────────────────────
echo ""
echo "Test (c): read-only role with no --touchpoints still gets the legacy warning"

OUT=$(run_spawn "$GH_NO_PRS" "$GIT_NO_WT" \
  --role security-reviewer --discussion 2153 --task-prompt "test" --pr 42) && RC=0 || RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "(c) legacy spawn without --touchpoints exits 0"
else
  fail "(c) exit code" "expected 0, got $RC. Output: $OUT"
fi
if echo "$OUT" | grep -qi "WARN.*touchpoints not set"; then
  pass "(c) WARN --touchpoints not set still fires"
else
  fail "(c) WARN" "expected the legacy WARN about missing --touchpoints, got: $OUT"
fi

# ── (d) undeclared role defaults to writer ───────────────────────────────────
echo ""
echo "Test (d): a role card with no read_only: field is treated as write-capable"

OUT=$(run_spawn "$GH_PR_CONFLICT" "$GIT_NO_WT" \
  --role stub-new-role --discussion 2153 --task-prompt "test" \
  --touchpoints "foo.py") && RC=0 || RC=$?

if [[ "$RC" -ne 0 ]]; then
  pass "(d) undeclared role spawn exits non-zero on conflict"
else
  fail "(d) exit code" "expected non-zero, got 0. Output: $OUT"
fi
if echo "$OUT" | grep -q "CONFLICT:"; then
  pass "(d) CONFLICT: line present for an undeclared role"
else
  fail "(d) CONFLICT: line" "expected CONFLICT: in output, got: $OUT"
fi

# ── (e) no overlap, read-only role ───────────────────────────────────────────
echo ""
echo "Test (e): read-only role, no overlap — clean pass, no CONFLICT or NOTE"

OUT=$(run_spawn "$GH_NO_PRS" "$GIT_NO_WT" \
  --role security-reviewer --discussion 2153 --task-prompt "test" --pr 42 \
  --touchpoints "foo.py") && RC=0 || RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "(e) read-only spawn with no overlap exits 0"
else
  fail "(e) exit code" "expected 0, got $RC. Output: $OUT"
fi
if echo "$OUT" | grep -qE "CONFLICT:|NOTE:"; then
  fail "(e) clean output" "expected neither CONFLICT: nor NOTE:, got: $OUT"
else
  pass "(e) no CONFLICT: or NOTE: line"
fi

# ── item 5: --touchpoints still forwarded to pre-spawn-check.sh ─────────────
echo ""
echo "Test: --touchpoints is still forwarded to pre-spawn-check.sh on a read-only spawn"

PSC_LOG="$TEST_DIR/psc-args.log"
: > "$PSC_LOG"
PSC_ARGS_LOG="$PSC_LOG"
OUT=$(run_spawn "$GH_NO_PRS" "$GIT_NO_WT" \
  --role security-reviewer --discussion 2153 --task-prompt "test" \
  --touchpoints "foo.py") && RC=0 || RC=$?

if [[ -f "$PSC_LOG" ]] && grep -q -- "--touchpoints foo.py" "$PSC_LOG"; then
  pass "pre-spawn-check.sh received --touchpoints on the read-only run"
else
  fail "pre-spawn-check.sh forwarding" "expected '--touchpoints foo.py' in pre-spawn-check.sh's args, got: $(cat "$PSC_LOG" 2>/dev/null)"
fi

# ── (f)/(g): D#2158 — worktree half is skipped for a read-only role, and the
# open-PR overlap NOTE (D#2153's information-not-lost guarantee) still fires ─
# GIT_NO_WT above never actually matches `git worktree list --porcelain`
# (worktree-claims.sh calls it with a leading `-C <root>`, so `$1` there is
# always "-C", not "worktree" — it happens to work only because that yields
# an empty worktree population either way). This stub matches on `$3` (the
# subcommand after `-C <path>`), the way the real call shape requires, and
# gives wtc_cmd_list one real linked worktree to scan — otherwise "zero
# worktree-scan git calls for a read-only role" and "a write-capable role's
# full scan is non-zero" are indistinguishable from "there was nothing to
# scan either way".
STATUS_LOG="$TEST_DIR/git-status-calls.log"
FAKE_WT="$TEST_DIR/fake-worktree"
mkdir -p "$FAKE_WT"

GIT_WITH_WT="#!/usr/bin/env bash
case \"\$3\" in
  worktree)
    echo 'worktree /fake/main'
    echo 'HEAD abc123'
    echo 'branch refs/heads/main'
    echo ''
    echo 'worktree $FAKE_WT'
    echo 'HEAD def456'
    echo 'branch refs/heads/some-other-branch'
    ;;
  status)
    echo call >> '$STATUS_LOG'
    ;;
  rev-list)
    echo 0
    ;;
  log)
    date +%s
    ;;
  *)
    :
    ;;
esac
exit 0"

echo ""
echo "Test (f): read-only role — zero worktree-scan git status calls, skip line present, PR-overlap NOTE still fires"

: > "$STATUS_LOG"
OUT=$(run_spawn "$GH_PR_CONFLICT" "$GIT_WITH_WT" \
  --role security-reviewer --discussion 2158 --task-prompt "test" --pr 42 \
  --touchpoints "foo.py") && RC=0 || RC=$?

if [[ "$RC" -eq 0 ]]; then
  pass "(f) read-only spawn with a live worktree population exits 0"
else
  fail "(f) exit code" "expected 0, got $RC. Output: $OUT"
fi
STATUS_CALLS_F=$(wc -l < "$STATUS_LOG" | tr -d ' ')
if [[ "$STATUS_CALLS_F" -eq 0 ]]; then
  pass "(f) zero 'git status --porcelain' calls recorded for the read-only spawn"
else
  fail "(f) worktree scan not skipped" "expected 0 status calls, got $STATUS_CALLS_F. Output: $OUT"
fi
if echo "$OUT" | grep -qi "read-only" && echo "$OUT" | grep -q "security-reviewer" && echo "$OUT" | grep -qi "skip"; then
  pass "(f) output names the role and states the worktree half was skipped"
else
  fail "(f) skip line" "expected a line naming role=security-reviewer, 'read-only', and 'skip'. Output: $OUT"
fi
if echo "$OUT" | grep -q "NOTE:.*foo.py.*PR#42"; then
  pass "(f) open-PR overlap NOTE is still emitted"
else
  fail "(f) open-PR overlap NOTE" "expected a NOTE: line naming foo.py and PR#42 even with the worktree half skipped. Output: $OUT"
fi

echo ""
echo "Test (g): write-capable role against the same live worktree population — full scan still runs"

: > "$STATUS_LOG"
OUT=$(run_spawn "$GH_PR_CONFLICT" "$GIT_WITH_WT" \
  --role executor --discussion 2158 --task-prompt "test" \
  --touchpoints "foo.py") && RC=0 || RC=$?

STATUS_CALLS_G=$(wc -l < "$STATUS_LOG" | tr -d ' ')
if [[ "$STATUS_CALLS_G" -gt 0 ]]; then
  pass "(g) write-capable role still runs the worktree scan (git status --porcelain count > 0)"
else
  fail "(g) full scan expected" "expected at least 1 status call for a write-capable role, got $STATUS_CALLS_G. Output: $OUT"
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
