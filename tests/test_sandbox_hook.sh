#!/usr/bin/env bash
# tests/test_sandbox_hook.sh
#
# Integration tests for hooks/sandbox.py
# Pipes fixture JSON directly to the hook and asserts exit codes + stderr substrings.
#
# Usage: bash tests/test_sandbox_hook.sh
# Exit 0 = all tests passed; non-zero = at least one failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# D#2267: hooks/sandbox.py's own telemetry dir is anchored to wherever the
# invoked *file* lives (Path(__file__).resolve().parent.parent), with no env
# override. Invoking the real $REPO_ROOT/hooks/sandbox.py directly would
# append every block this suite generates to the LIVE
# .autonomous-team/hook-events/blocks-<date>.jsonl — the file every running
# agent's own sandbox hook is also writing to. Run this suite against an
# isolated fixture copy instead; see tests/lib/repo-root-fixture.sh for why
# a copy (not a symlink) and why it needs a real .git.
source "$REPO_ROOT/tests/lib/repo-root-fixture.sh"
FIXTURE_ROOT="$(repo_root_fixture_make "$REPO_ROOT")" || {
  echo "FAIL: could not create isolated repo-root fixture" >&2
  exit 1
}
HOOK="$FIXTURE_ROOT/hooks/sandbox.py"

if [[ ! -f "$HOOK" ]]; then
  echo "FAIL: hook not found at $HOOK" >&2
  exit 1
fi

PASS=0
FAIL=0

# Scratch dir for this suite's own stderr captures. Was a shared fixed
# /tmp/_hook_stderr{,_2} filename — a live sibling reviewer running this same
# suite concurrently in another worktree could race on that name and produce
# a false failure (D#2254).
RUN_TMP="$(mktemp -d /tmp/test_sandbox_hook.XXXXXX)"
trap 'rm -rf "$RUN_TMP" "$FIXTURE_ROOT"' EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

run_hook() {
  local json="$1"
  # Returns exit code from the hook; captures stderr in HOOK_STDERR
  HOOK_STDERR=$(echo "$json" | python3 "$HOOK" 2>&1) || true
  HOOK_EXIT="${PIPESTATUS[0]:-$?}"
  HOOK_EXIT=$(echo "$json" | python3 "$HOOK" 2>"$RUN_TMP/hook_stderr"; echo $?)
  HOOK_STDERR=$(cat "$RUN_TMP/hook_stderr" 2>/dev/null || true)
}

run_hook2() {
  local json="$1"
  HOOK_STDERR=""
  HOOK_EXIT=0
  echo "$json" | python3 "$HOOK" >/dev/null 2>"$RUN_TMP/hook_stderr_2" || HOOK_EXIT=$?
  HOOK_STDERR=$(cat "$RUN_TMP/hook_stderr_2" 2>/dev/null || true)
}

assert_allowed() {
  local name="$1"
  local json="$2"
  run_hook2 "$json"
  if [[ "$HOOK_EXIT" -eq 0 ]]; then
    echo "PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name — expected exit 0 (allow), got $HOOK_EXIT"
    echo "      stderr: $HOOK_STDERR"
    FAIL=$((FAIL + 1))
  fi
}

assert_blocked() {
  local name="$1"
  local json="$2"
  local expected_substr="$3"
  run_hook2 "$json"
  if [[ "$HOOK_EXIT" -eq 2 ]]; then
    if echo "$HOOK_STDERR" | grep -qF "$expected_substr"; then
      echo "PASS: $name"
      PASS=$((PASS + 1))
    else
      echo "FAIL: $name — exit was 2 but stderr did not contain '$expected_substr'"
      echo "      stderr: $HOOK_STDERR"
      FAIL=$((FAIL + 1))
    fi
  else
    echo "FAIL: $name — expected exit 2 (block), got $HOOK_EXIT"
    echo "      stderr: $HOOK_STDERR"
    FAIL=$((FAIL + 1))
  fi
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MAIN_REPO is the fixture root, not the real checkout: hooks/sandbox_rules.py
# (the copy we just made under $FIXTURE_ROOT/hooks/) derives its own notion
# of "main repo root" from its own __file__ via hooks/repo_root.py, and that
# derivation is confident precisely because repo_root_fixture_make() gave the
# fixture a real .git. Pointing MAIN_REPO at the real checkout here would
# disagree with what the invoked hook copy actually believes its root is.
MAIN_REPO="$FIXTURE_ROOT"
WT_CLAUDE="$MAIN_REPO/.claude/worktrees/testid123"
WT_TMP="/tmp/wt-testid"

# ---------------------------------------------------------------------------
# AC1 — git write-verb rejection
# ---------------------------------------------------------------------------

assert_blocked "AC1: git checkout main" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git checkout main\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "AC1: git -C main-repo checkout" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -C $MAIN_REPO checkout main\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "AC1: cd main-repo && git checkout" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd $MAIN_REPO && git checkout main\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "AC1: bash -c git reset" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"bash -c \\\"git reset --hard origin/main\\\"\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox:"

assert_blocked "AC1: git switch main" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git switch main\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "AC1: git branch -D feature-x from worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch -D feature-x\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

# ---------------------------------------------------------------------------
# D#2058 — _GIT_ALWAYS_BLOCKED_VERBS ignored flags: `git branch`/`git worktree`
# read-only spellings, and `--help` on any of the seven always-blocked verbs,
# were refused identically to their write spellings. End-to-end through the
# real hook (criterion 9), not just classify_bash in isolation.
# ---------------------------------------------------------------------------

# Criterion 4: read-only spellings now ALLOW.
assert_allowed "D#2058 c4: git branch (bare listing)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git branch --list" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch --list\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git branch -l" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch -l\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git branch --show-current" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch --show-current\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git branch -a" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch -a\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git branch -v" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch -v\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git branch --merged" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch --merged\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git branch --help" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch --help\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git reset --help (doc lookup, not a reset)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git reset --help\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git checkout --help (doc lookup, not a checkout)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git checkout --help\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4/c11: git worktree list — the D#2078 diagnostic" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git worktree list\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c4: git worktree list --porcelain" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git worktree list --porcelain\"},\"cwd\":\"$WT_CLAUDE\"}"

# Criterion 5: write spellings of the SAME verbs stay refused.
assert_blocked "D#2058 c5: git branch -D foo" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch -D foo\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#2058 c5: git branch -d foo" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch -d foo\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#2058 c5: git branch -m a b" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch -m a b\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#2058 c5: git branch --set-upstream-to=origin/x" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch --set-upstream-to=origin/x\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#2058 c5: git worktree add /tmp/x" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git worktree add /tmp/x\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#2058 c5: git worktree remove /tmp/x" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git worktree remove /tmp/x\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

# Criterion 6: unrecognised flags/subcommands fail closed (allowlist, not denylist).
assert_blocked "D#2058 c6: git branch --some-future-flag" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git branch --some-future-flag\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#2058 c6: git worktree some-future-subcommand" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git worktree some-future-subcommand\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

# Criterion 7: chained read-only-then-write escape (D#1729 F3) must still block
# — a per-invocation escape must not resurrect first-verb-wins.
assert_blocked "D#2058 c7: git log;git reset --hard origin/main" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git log;git reset --hard origin/main\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#2058 c7: git status && git worktree remove /tmp/x" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git status && git worktree remove /tmp/x\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

# ---------------------------------------------------------------------------
# AC2 — read-only git allowlist
# ---------------------------------------------------------------------------

assert_allowed "AC2: git fetch origin" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git fetch origin\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git log -5" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git log -5\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git status" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git status\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git diff" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git diff\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git show HEAD" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git show HEAD\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git rev-parse HEAD" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git rev-parse HEAD\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git ls-files" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git ls-files\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git cat-file -p HEAD" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git cat-file -p HEAD\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git for-each-ref" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git for-each-ref\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC2: git -C main-repo fetch (read-only, allowed)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -C $MAIN_REPO fetch origin\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# AC3 — write-path rejection
# ---------------------------------------------------------------------------

assert_blocked "AC3: Edit file outside worktree" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$MAIN_REPO/CLAUDE.md\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: file_path outside worktree"

assert_blocked "AC3: Write file outside worktree" \
  "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$MAIN_REPO/scripts/foo.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: file_path outside worktree"

assert_blocked "AC3: Bash redirect outside worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo X > $MAIN_REPO/CLAUDE.md\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "AC3: tee outside worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo X | tee $MAIN_REPO/foo\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox:"

assert_blocked "AC3: cp outside worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cp x $MAIN_REPO/\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox:"

assert_blocked "AC3: mv outside worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"mv x $MAIN_REPO/newfile\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox:"

# Edit within the worktree — allowed
assert_allowed "AC3: Edit within worktree" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$WT_CLAUDE/src/app.py\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "AC3: Relative path Edit allowed" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"src/app.py\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# AC4 — merge rejection
# ---------------------------------------------------------------------------

assert_blocked "AC4: gh pr merge squash" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"gh pr merge 999 --squash\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: sub-agents may not merge"

assert_blocked "AC4: gh api graphql mergePullRequest" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"gh api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\\\"x\\\"}) { pullRequest { merged } } }'\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: sub-agents may not merge"

assert_blocked "AC4: gh api PUT merge endpoint" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"gh api -X PUT repos/autonomous-agent-7/autonomous-forever/pulls/999/merge\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: sub-agents may not merge"

# PR create is allowed
assert_allowed "AC4: gh pr create allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"gh pr create --base main --title 'x' --body 'y'\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# AC5 — Team Lead exempt (CWD = main repo root)
# ---------------------------------------------------------------------------

assert_allowed "AC5: TL git checkout main" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git checkout main\"},\"cwd\":\"$MAIN_REPO\"}"

assert_allowed "AC5: TL Edit outside worktree" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$MAIN_REPO/CLAUDE.md\"},\"cwd\":\"$MAIN_REPO\"}"

assert_allowed "AC5: TL gh pr merge" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"gh pr merge 42 --squash --delete-branch\"},\"cwd\":\"$MAIN_REPO\"}"

assert_allowed "AC5: TL Bash redirect outside worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > $MAIN_REPO/CLAUDE.md\"},\"cwd\":\"$MAIN_REPO\"}"

# ---------------------------------------------------------------------------
# D#439 AC1/AC3 — claude_spawn_forbidden: block event + deny tests
# ---------------------------------------------------------------------------

assert_blocked "D#439: claude -p direct invocation blocked" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"claude -p \\\"Run ONE /loop iteration...\\\"\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#439: /usr/local/bin/claude -p blocked" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"/usr/local/bin/claude -p test\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#439: bash -c claude blocked" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"bash -c 'claude -p test'\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#439: spawn-agent.sh fragment blocked" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"bash scripts/spawn-agent.sh --role executor\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#439: backend/trigger.py fragment blocked" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"python3 backend/trigger.py\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

# Verify negative: grep claude CLAUDE.md must NOT be blocked
assert_allowed "D#439: grep claude CLAUDE.md allowed (negative corpus)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"grep claude CLAUDE.md\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# D#2058 — _FORBIDDEN_FRAGMENTS matched the raw string anywhere, so MENTIONING
# a forbidden path (reading it, grepping it, quoting it in a PR body) refused
# identically to actually running it. End-to-end through the real hook
# (criterion 9), not just check_claude_spawn in isolation.
# ---------------------------------------------------------------------------

# Criterion 1: the five harmless rows from the filing now ALLOW.
assert_allowed "D#2058 c1: grep -n legacy_lane backend/trigger.py (read, not run)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"grep -n legacy_lane backend/trigger.py\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c1/c11: cat run-loop-iteration.sh (read, not run)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cat run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c1: gh pr edit --body mentioning backend/trigger.py (prose)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"gh pr edit 1 --body 'this fixes backend/trigger.py'\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c1: git log -- run-loop-iteration.sh (read, not run)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git log --oneline -- run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 c1: echo mentioning spawn-agent.sh (prose)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo 'see spawn-agent.sh for details'\"},\"cwd\":\"$WT_CLAUDE\"}"

# The two genuine runaway forms from the filing's table stay refused.
assert_blocked "D#2058 c1: bash run-loop-iteration.sh (genuine runaway)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"bash run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 c1: python3 backend/trigger.py 'go' (genuine runaway)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"python3 backend/trigger.py 'go'\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

# Criterion 2: every interpreter/sourcing/chained spelling still refuses.
assert_blocked "D#2058 c2: sh run-loop-iteration.sh" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"sh run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 c2: source scripts/spawn-agent.sh" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"source scripts/spawn-agent.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 c2: . scripts/spawn-agent.sh (dot-sourcing)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\". scripts/spawn-agent.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 c2: ./run-loop-iteration.sh (direct exec)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"./run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 c2: bash -c wrapping bash run-loop-iteration.sh" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"bash -c 'bash run-loop-iteration.sh'\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 c2: chained with && after an unrelated command" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls && bash run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 c2: chained with ; after an unrelated command" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls; python3 backend/trigger.py\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

# ---------------------------------------------------------------------------
# D#2058 fix-cycle 1 (security review) — command-prefix wrappers (timeout,
# nohup, nice, stdbuf, setsid, time, ionice, xargs) let the wrapped runaway
# form through both shapes untouched. End-to-end through the real hook.
# ---------------------------------------------------------------------------

assert_blocked "D#2058 fc1: timeout 60 bash run-loop-iteration.sh" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"timeout 60 bash run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 fc1: timeout --kill-after=5s 600 bash run-loop-iteration.sh (mandated bounded-run form)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"timeout --kill-after=5s 600 bash run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 fc1: nohup bash run-loop-iteration.sh & — still blocked (D#2248 background check now fires first)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"nohup bash run-loop-iteration.sh &\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "background_run_forbidden"

assert_blocked "D#2058 fc1: nice -n 10 bash run-loop-iteration.sh" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"nice -n 10 bash run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 fc1: stdbuf -oL bash run-loop-iteration.sh" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"stdbuf -oL bash run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 fc1: xargs bash run-loop-iteration.sh" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"xargs bash run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_blocked "D#2058 fc1: python3 backend/trigger.py wrapped in timeout" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"timeout 300 python3 backend/trigger.py\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "claude_spawn_forbidden"

assert_allowed "D#2058 fc1: timeout 60 cat run-loop-iteration.sh (wrapped READ, must stay allowed)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"timeout 60 cat run-loop-iteration.sh\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2058 fc1: timeout --kill-after=5s 600 pytest ... (mandated bounded form over a harmless command)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"timeout --kill-after=5s 600 pytest backend/tests/test_foo.py\"},\"cwd\":\"$WT_CLAUDE\"}"

# Verify the structured block event is written to the daily blocks file
D439_CMD='claude -p "Run ONE /loop iteration..."'
D439_JSON="{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"$D439_CMD\"},\"cwd\":\"$WT_CLAUDE\"}"
TODAY=$(date +%Y-%m-%d)
BLOCKS_FILE="$MAIN_REPO/.autonomous-team/hook-events/blocks-$TODAY.jsonl"
echo "$D439_JSON" | python3 "$HOOK" 2>/dev/null || true
if [[ -f "$BLOCKS_FILE" ]] && grep -q "claude_spawn_forbidden" "$BLOCKS_FILE" 2>/dev/null; then
  echo "PASS: D#439 AC3 — structured block event written to $BLOCKS_FILE"
  PASS=$((PASS + 1))
else
  echo "FAIL: D#439 AC3 — block event with claude_spawn_forbidden not found in $BLOCKS_FILE"
  FAIL=$((FAIL + 1))
fi

# Team Lead is exempt even from claude spawn block (no worktree)
assert_allowed "D#439: Team Lead claude -p exempt" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"claude -p test\"},\"cwd\":\"$MAIN_REPO\"}"

# ---------------------------------------------------------------------------
# AC7 — Transcript visibility: rejection message contains "blocked by sandbox:"
# ---------------------------------------------------------------------------

RUN_STDERR=$(echo "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git checkout main\"},\"cwd\":\"$WT_CLAUDE\"}" | python3 "$HOOK" 2>&1 || true)
if echo "$RUN_STDERR" | grep -q "blocked by sandbox:"; then
  echo "PASS: AC7 transcript visibility — 'blocked by sandbox:' in stderr"
  PASS=$((PASS + 1))
else
  echo "FAIL: AC7 transcript visibility — expected 'blocked by sandbox:' in stderr"
  echo "      stderr: $RUN_STDERR"
  FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# D#1898 — classify_bash never expanded ~ or $HOME; tilde-spelled writes
# escaped the worktree sandbox. Fixture commands below are the exact four
# from the Discussion's repro table, invoked at the hook boundary (subprocess,
# piped JSON) as required by its acceptance criteria — not a unit test of a
# helper function.
# ---------------------------------------------------------------------------

assert_blocked "D#1898: echo > ~/.claude/settings.json escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo '{}' > ~/.claude/settings.json\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1898: printf >> \$HOME/.bashrc escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"printf x >> \$HOME/.bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1898: echo >> \${HOME}/.bashrc escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x >> \${HOME}/.bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1898: cd ~ && echo x >> .bashrc escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd ~ && echo x >> .bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree (cd left the worktree)"

# Additional spellings identified while implementing the fix (cp/tee/mv also
# accept ~ and $HOME destinations, not just redirects).
assert_blocked "D#1898: cp to ~ destination escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cp x ~/.bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1898: tee to \$HOME destination escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x | tee \$HOME/.claude/settings.json\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

# Negative corpus — writes that must remain allowed after this fix.
assert_allowed "D#1898: plain relative write (no cd) stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo hi > src/app.py\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1898: absolute write inside worktree stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo hi > $WT_CLAUDE/src/app.py\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1898: cd within worktree then relative write stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd $WT_CLAUDE/src && echo hi > app.py\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1898: /tmp write stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo hi > /tmp/scratch.txt\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# D#1898 round 2 (security review of PR #1901) — B1/B2 over-block regressions.
#
# Round 1 closed four real escapes but introduced two over-blocks: a
# raw-regex scan that couldn't tell a WRITE from a MENTION of one (B1), and
# a missing /tmp exemption in the cd-escape check that every agent's own
# scratchpad workflow tripped over (B2), plus a position-blindness gap where
# a write BEFORE a later `cd` was judged by the CWD AFTER the whole command.
# These fixtures prove all three are fixed without reopening the escapes
# tested above — each one is ALLOW on main, was wrongly BLOCK after round 1,
# and must be ALLOW again here.
# ---------------------------------------------------------------------------

# B1 — a heredoc writing a file whose content is a quoted string mentioning
# a home-path redirect (the realistic "writing this PR's own test file"
# case). Built via python so the embedded quotes/newlines aren't hand-escaped
# into the JSON incorrectly.
B1_HEREDOC_JSON=$(WT_CLAUDE="$WT_CLAUDE" python3 <<'PYEOF'
import json
import os

cmd = (
    "cat > /tmp/repro_test.py <<'EOF'\n"
    'assert classify_bash("echo x > ~/.claude/settings.json", cwd).allow is False\n'
    "EOF\n"
)
print(json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": os.environ["WT_CLAUDE"]}))
PYEOF
)
assert_allowed "D#1898 round2 B1: heredoc test-file body with quoted mention stays allowed" \
  "$B1_HEREDOC_JSON"

# B1 — an unquoted mention inside a heredoc body (e.g. plain-text doc
# content), a stricter variant of the case above with no quotes to lean on.
B1_HEREDOC_UNQUOTED_JSON=$(WT_CLAUDE="$WT_CLAUDE" python3 <<'PYEOF'
import json
import os

cmd = "cat > /tmp/notes.md <<'EOF'\nRepro: echo x > ~/.claude/settings.json\nEOF\n"
print(json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": os.environ["WT_CLAUDE"]}))
PYEOF
)
assert_allowed "D#1898 round2 B1: heredoc doc body with unquoted mention stays allowed" \
  "$B1_HEREDOC_UNQUOTED_JSON"

# B1 — reporting the bug via a quoted gh/echo argument must not itself get
# blocked as a write.
assert_allowed "D#1898 round2 B1: gh pr comment quoting a repro command stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"gh pr comment 1 --body 'repro: echo x > ~/.bashrc'\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1898 round2 B1: echo of a quoted mention stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo 'writes go to > ~/.bashrc'\"},\"cwd\":\"$WT_CLAUDE\"}"

# B2 — the /tmp scratchpad every agent is told to use must stay allowed even
# when reached via `cd`, not just via a direct absolute redirect.
assert_allowed "D#1898 round2 B2: cd into /tmp scratchpad then relative write stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd /tmp/scratchpad-xyz && echo hi > notes.txt\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1898 round2 B2: cd into /var/tmp scratchpad then relative write stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd /var/tmp/scratchpad-xyz && echo hi > notes.txt\"},\"cwd\":\"$WT_CLAUDE\"}"

# Should-fix — position blindness: the write happens BEFORE the escaping
# `cd`, so it must not be judged by the CWD left after the whole command.
assert_allowed "D#1898 round2 should-fix: write before a later escaping cd stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo hi > out.txt && cd ~\"},\"cwd\":\"$WT_CLAUDE\"}"

# Non-vacuity for the order-aware rewrite: a write AFTER the escaping cd in
# the same compound command must still be caught.
assert_blocked "D#1898 round2 should-fix: write after an escaping cd is still caught" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo hi > out.txt && cd ~ && echo bye >> .bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree (cd left the worktree)"

# ---------------------------------------------------------------------------
# D#1898 round 3 (security review of PR #1901) — no-space redirect coverage
# regression. Round 2's tokenised `~`/`$HOME` scan used bare shlex.split,
# which treats `>` as an ordinary word character, not an operator, when
# there's no whitespace around it. `echo x >~/.bashrc` tokenised as one
# fused token `>~/.bashrc` — never matching `tok in (">", ">>")` — so the
# no-space spellings below silently regained the exact worktree escape
# round 1 closed, one keystroke away from the fixtures above. Confirmed
# against real bash with an isolated $HOME that these genuinely write the
# file. These are repro #1 and #4 with the space removed.
# ---------------------------------------------------------------------------

assert_blocked "D#1898 round3: echo x >~/.bashrc (no space) escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x >~/.bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1898 round3: echo x >>\$HOME/.bashrc (no space) escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x >>\$HOME/.bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1898 round3: cd ~ && echo x >>.bashrc (no space) escapes worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"cd ~ && echo x >>.bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree (cd left the worktree)"

# ---------------------------------------------------------------------------
# D#1756 (+ D#1903, closed as its duplicate) — a read-only OR permitted-write
# git verb used to short-circuit classify_bash before the redirect/write-
# target scans ever ran, so a write chained onto the same command line via
# `&&`/`;` laundered right through. Fixed by falling through instead of
# returning early once the git verb itself is vetted.
# ---------------------------------------------------------------------------

assert_blocked "D#1756: git log && rm -rf main-repo/scripts" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git log && rm -rf $MAIN_REPO/scripts\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: unenumerated command wrote outside worktree"

assert_blocked "D#1756: git status; echo x >> main-repo/CLAUDE.md" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git status; echo x >> $MAIN_REPO/CLAUDE.md\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1756: git commit -m x && rm -rf main-repo/scripts (write-verb sibling)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m x && rm -rf $MAIN_REPO/scripts\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: unenumerated command wrote outside worktree"

assert_blocked "D#1756: git add -A && echo x >> main-repo/CLAUDE.md (write-verb sibling)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git add -A && echo x >> $MAIN_REPO/CLAUDE.md\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1903: git log > ~/.bashrc" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git log > ~/.bashrc\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1903: git log > /etc/passwd" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git log > /etc/passwd\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

# Regression trap — the naive fix (fall through with no other change) makes
# git's own `-C <path>` operand look like a write-target token to the
# unenumerated path-token scan. These must keep allowing.

assert_allowed "D#1756 regression trap: git -C main-repo log --oneline -5 stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -C $MAIN_REPO log --oneline -5\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1756 regression trap: git -C main-repo status stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -C $MAIN_REPO status\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1756: git -C /etc log stays allowed (read-only allowlist)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -C /etc log\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1756: git commit -m x inside worktree stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m x\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1756: git add -A && git commit -m x inside worktree stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git add -A && git commit -m x\"},\"cwd\":\"$WT_CLAUDE\"}"

# Must-stay-BLOCK, no weakening (criteria 20/21).

assert_blocked "D#1756: git -C main-repo push --force still blocks" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -C $MAIN_REPO push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1756: git log && git checkout main still blocks (F3 multi-verb walker)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git log && git checkout main\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

# ---------------------------------------------------------------------------
# D#1792 — a quoted `/`-prefixed redirect target was invisible to the
# boundary check: PR #1901's tokenised `~`/`$HOME` scan is quote-correct, but
# its candidate filter only accepted `~`/`$HOME`/`${HOME}` prefixes, so a
# quoted `/`-prefixed target fell through to the quote-blind raw regex,
# which can't see past the opening quote.
# ---------------------------------------------------------------------------

assert_blocked "D#1792: echo x > \"main-repo/CLAUDE.md\" (double-quoted)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > \\\"$MAIN_REPO/CLAUDE.md\\\"\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1792: echo x > 'main-repo/CLAUDE.md' (single-quoted)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > '$MAIN_REPO/CLAUDE.md'\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1792: quoted target containing a space is caught" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > \\\"$MAIN_REPO/a file.md\\\"\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_blocked "D#1792: append form >> \"main-repo/CLAUDE.md\" is caught" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x >> \\\"$MAIN_REPO/CLAUDE.md\\\"\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: output redirect outside worktree"

assert_allowed "D#1792: quoted /tmp target stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > \\\"/tmp/scratch.txt\\\"\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1792: quoted in-worktree target stays allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo x > \\\"$WT_CLAUDE/notes.txt\\\"\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# D#1746 / D#1748 — resolve_effective_cwd reproduced the same command-
# substitution / bare-token / first-invocation-wins bugs _extract_all_git_verbs
# was hardened against (D#1729 F1/F2/F3), plus the redirection-displacement
# gap (D#1748 F7), independently in a sibling function. Both are closed by a
# shared tokenize/walk layer and per-invocation (verb, cwd) pairing in
# classify_bash instead of one CWD for the whole command.
# ---------------------------------------------------------------------------

assert_blocked "D#1746 A1: /usr/bin/git -C main-repo push --force (F2, no shielding)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"/usr/bin/git -C $MAIN_REPO push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 A2: git diff && git -C main-repo push --force (F3, second invocation's -C)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git diff && git -C $MAIN_REPO push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 A3: git log && cd main-repo && git push --force (F3 via cd)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git log && cd $MAIN_REPO && git push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 A4: (git -C main-repo push --force) (F1 subshell)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"(git -C $MAIN_REPO push --force)\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 A5: true;\$(git -C main-repo push --force) (F1 command substitution)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"true;\$(git -C $MAIN_REPO push --force)\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 A6: backtick-wrapped git -C main-repo push --force (F1 backtick)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"\`git -C $MAIN_REPO push --force\`\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 A7: git -c core.pager=cat -C main-repo push --force (F4 interaction)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -c core.pager=cat -C $MAIN_REPO push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1748 A8: git 2>&1 -C main-repo push --force (F7 redirection displaces -C slot)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git 2>&1 -C $MAIN_REPO push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 A9: /usr/bin/git --git-dir + --work-tree main-repo push --force" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"/usr/bin/git --git-dir $MAIN_REPO/.git --work-tree $MAIN_REPO push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

# Legitimate use that must NOT block (over-block guards, Spec section B).

assert_allowed "D#1746 B10: git commit -m wip in worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m wip\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1746 B11: git push -u origin HEAD in worktree" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git push -u origin HEAD\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1746 B13: git -C worktree push -u origin HEAD (explicit -C inside worktree)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -C $WT_CLAUDE push -u origin HEAD\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1746 B_TRAP: git -C main-repo log && git commit -m wip (per-invocation pairing, must not over-block)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git -C $MAIN_REPO log && git commit -m wip\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1746 B17: separator char inside a quoted commit message stays text" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"fix;bug\\\"\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# D#1746 round 2 — post-review finding: `--git-dir=<path>`/`--work-tree=<path>`
# (the glued long-option form, standard git CLI syntax) fell through the
# exact-token-equality match unrecognised, so the CWD override was silently
# skipped. Fixed generally via _split_glued_git_option rather than special-
# casing the two names. This is the tester's own live repro.
# ---------------------------------------------------------------------------

assert_blocked "D#1746 round2: glued git-dir alone (--git-dir=<path>)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git --git-dir=$MAIN_REPO/.git push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 round2: glued work-tree alone (--work-tree=<path>)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git --work-tree=$MAIN_REPO push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_blocked "D#1746 round2: tester's exact repro — glued --git-dir + --work-tree, push --force" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git --git-dir=$MAIN_REPO/.git --work-tree=$MAIN_REPO push --force\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "blocked by sandbox: git write-verb outside worktree"

assert_allowed "D#1746 round2: glued git-dir on a read-only verb still allowed (no over-block)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git --git-dir=$MAIN_REPO/.git log -5\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#1746 round2: glued work-tree pointing inside worktree still allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git --work-tree=$WT_CLAUDE commit -m wip\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# D#2070 — run_in_background deny for non-Team-Lead tiers.
# Nothing re-invokes a sub-agent when a backgrounded job finishes, so the
# flag is denied at the boundary instead of leaving the agent parked.
# ---------------------------------------------------------------------------

assert_blocked "D#2070 AC1: worktree sub-agent backgrounding pytest is denied" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"pytest tests/\",\"run_in_background\":true},\"cwd\":\"$WT_CLAUDE\"}" \
  "background_run_forbidden"

assert_blocked "D#2070 AC2: rejection names the bounded alternative (timeout)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"pytest tests/\",\"run_in_background\":true},\"cwd\":\"$WT_CLAUDE\"}" \
  "timeout"

assert_allowed "D#2070 AC3: Team Lead's backgrounded merge-and-hook.sh is unaffected" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"bash scripts/merge-and-hook.sh --pr 1\",\"run_in_background\":true},\"cwd\":\"$MAIN_REPO\"}"

assert_allowed "D#2070 AC4: bounded foreground verification (run_in_background:false) is unaffected" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"timeout 600 pytest tests/\",\"run_in_background\":false},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2070 AC5: ordinary command with no run_in_background key at all is unaffected" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"pytest tests/\"},\"cwd\":\"$WT_CLAUDE\"}"

# AC6: the deny is counted through the existing telemetry path, not a bare
# sys.exit — confirm a matching row lands in today's blocks file.
BLOCKS_FILE="$MAIN_REPO/.autonomous-team/hook-events/blocks-$(date +%F).jsonl"
if [[ -f "$BLOCKS_FILE" ]] && grep -q "background_run_forbidden" "$BLOCKS_FILE"; then
  echo "PASS: D#2070 AC6: block is counted in $BLOCKS_FILE"
  PASS=$((PASS + 1))
else
  echo "FAIL: D#2070 AC6: no background_run_forbidden row found in $BLOCKS_FILE"
  FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# D#2248 — shell-level backgrounding inside the command string. The
# run_in_background flag was covered by D#2070 above; a sub-agent can park
# itself just as effectively with `cmd &`, `nohup ... &`, `setsid ... &`, or
# `... & disown` — same trap, different spelling.
# ---------------------------------------------------------------------------

assert_blocked "D#2248: worktree sub-agent backgrounding with trailing & is denied" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"pytest tests/ &\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "background_run_forbidden"

assert_blocked "D#2248: nohup with redirect and trailing & is denied" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"nohup pytest tests/ > out.log 2>&1 &\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "background_run_forbidden"

assert_blocked "D#2248: setsid is denied" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"setsid bash run.sh &\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "background_run_forbidden"

assert_blocked "D#2248: trailing & followed by disown is denied" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"pytest tests/ > out.log 2>&1 & disown\"},\"cwd\":\"$WT_CLAUDE\"}" \
  "background_run_forbidden"

assert_allowed "D#2248: Team Lead can still shell-background a command (exemption ordering pinned)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"bash scripts/merge-and-hook.sh --pr 1 &\"},\"cwd\":\"$MAIN_REPO\"}"

assert_allowed "D#2248: && chain is not mistaken for backgrounding (no over-block)" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"make lint && make test\"},\"cwd\":\"$WT_CLAUDE\"}"

assert_allowed "D#2248: quoted URL & inside a gh api call is not mistaken for backgrounding" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"gh api \\\"repos/o/r/issues?a=1&b=2\\\"\"},\"cwd\":\"$WT_CLAUDE\"}"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
