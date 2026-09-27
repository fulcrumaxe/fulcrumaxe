#!/usr/bin/env bash
# tests/test_spawn_agent_pr_amend_e2e.sh — e2e coverage for the pr_amend
# route rendered by scripts/spawn-agent.sh + backend/prompt_builder.py (D#2546)
#
# Run: bash tests/test_spawn_agent_pr_amend_e2e.sh   (expects exit 0)
#
# backend/tests/test_prompt_builder.py already proves the pr_amend guidance
# STRING renders correctly — it calls SpawnPrompt.render() directly with a
# synthetic worktree_unprovisioned_reason="pr_amend". That is a claim about a
# function's return value, not about the wiring: it says nothing about
# whether scripts/spawn-agent.sh actually reaches that code path from real
# argv, and nothing about whether the route the guidance prescribes (fetch
# by URL from a tree that starts unrelated to the PR) actually lands on the
# PR's head. This suite makes both of those the assertion instead of trusting
# the rendered text.
#
# Item 1: invoke the real script (not prompt_builder directly) with
# --pr <N> --isolation worktree and assert against its real stdout.
#
# Item 2 (the binding one): extract the two commands between the
# "<!-- PR_AMEND_ROUTE:BEGIN/END -->" markers backend/prompt_builder.py now
# emits, out of that real stdout — not out of a fixture — run them for real
# from a throwaway clone of the code plane's own main (a tree that starts
# unrelated to the PR, per D#2542 — see "the unrelated tree" below), and
# assert the fetch lands on the PR's real head sha, resolved independently
# via `gh pr list` so the assertion is not circular against the very thing
# it is checking.
#
# Item 6: every network call is wrapped in `timeout --kill-after=5s`. If
# GitHub is unreachable, every item below is SKIPPED with a named reason
# (loud, its own counter, distinct from PASS/FAIL) rather than silently
# reported as green — same convention tests/test_tree_capability.sh already
# uses for its own environment-gap leg (item 14 there). A silent pass here
# would be a fifth instance of the pattern named in D#2492/D#2493/D#2463/
# D#2545: a guard reporting success having checked nothing.
#
# "The unrelated tree": a plain `git clone` of the code plane's own default
# branch into a scratch directory. Its history has nothing to do with the PR
# under test (its head is main's tip, not the PR's branch) — exactly the
# D#2542 starting condition, where a --pr worktree spawn is handed a tree
# the Agent() tool provisioned off main, never the PR's own branch. The
# clone carries no .autonomous-team/ (deliberately untracked on the code
# plane — see scripts/lib/repo-resolve.sh's own header), so this script
# writes a minimal config.json into it, the same way
# tests/test_tree_capability.sh's real-tree leg does, purely so the
# prescribed `_resolve_code_repo` call has something to resolve — a real
# agent's own worktree already carries this file.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPAWN_AGENT="$REPO_ROOT/scripts/spawn-agent.sh"

PASS=0
FAIL=0
SKIP=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL: $1"; shift; [ $# -gt 0 ] && echo "        $*"; FAIL=$((FAIL + 1)); }
skip() { echo "  SKIP: $1"; SKIP=$((SKIP + 1)); }
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain: $2"; fi
}
assert_not_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then bad "$1" "expected NOT to contain: $2"; else ok "$1"; fi
}

summarize_and_exit() {
  echo
  echo "PASS: $PASS  FAIL: $FAIL  SKIP: $SKIP"
  [ "$FAIL" -eq 0 ] || exit 1
  exit 0
}

WORK="$(mktemp -d)"
cleanup() { chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

export AUTONOMOUS_TEAM_STATE_DIR="$WORK/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"

# shellcheck source=scripts/lib/repo-resolve.sh
source "$REPO_ROOT/scripts/lib/repo-resolve.sh"
CODE_REPO="$(_resolve_code_repo)" || {
  skip "could not resolve the code plane repo from this checkout — nothing to test the route against"
  summarize_and_exit
}

echo "=== network probe ==="
if ! timeout --kill-after=5s 10 git ls-remote "https://github.com/${CODE_REPO}.git" HEAD >/dev/null 2>&1; then
  skip "GitHub (${CODE_REPO}) is unreachable from this environment — spawn-agent.sh's own --pr resolution and item 2's route-following both need it, so every item below is skipped rather than silently reported as passing"
  summarize_and_exit
fi

echo "=== pick a real PR to test the route against ==="
PR_JSON="$(timeout --kill-after=5s 20 gh pr list --repo "$CODE_REPO" --state all --limit 1 --json number,headRefOid 2>&1)"
PR_RC=$?
if [ "$PR_RC" -ne 0 ]; then
  skip "gh pr list failed (rc=$PR_RC) — cannot pick a live PR to test the route against: $(printf '%s' "$PR_JSON" | head -c 300)"
  summarize_and_exit
fi
PR_NUM="$(printf '%s' "$PR_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d[0]["number"] if d else "")' 2>/dev/null)"
PR_HEAD_EXPECTED="$(printf '%s' "$PR_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d[0]["headRefOid"] if d else "")' 2>/dev/null)"
if [ -z "$PR_NUM" ] || [ -z "$PR_HEAD_EXPECTED" ]; then
  skip "no PR exists on $CODE_REPO to test the route against"
  summarize_and_exit
fi
echo "  using PR #$PR_NUM (expected head $PR_HEAD_EXPECTED) on $CODE_REPO"

echo "=== item 1 — render the real brief, not a fixture ==="
RENDERED="$(timeout --kill-after=5s 30 bash "$SPAWN_AGENT" --role executor --task-prompt "e2e probe (D#2546)" --pr "$PR_NUM" --isolation worktree --no-register 2>"$WORK/spawn.err")"
SPAWN_RC=$?
if [ "$SPAWN_RC" -ne 0 ]; then
  bad "spawn-agent.sh --pr $PR_NUM --isolation worktree exited non-zero" "rc=$SPAWN_RC stderr: $(cat "$WORK/spawn.err")"
else
  ok "spawn-agent.sh rendered a prompt to real stdout (rc=0)"
fi
assert_contains "rendered stdout carries the pr_amend route markers" "<!-- PR_AMEND_ROUTE:BEGIN -->" "$RENDERED"
assert_contains "rendered stdout names PR #${PR_NUM}'s pull ref" "pull/${PR_NUM}/head" "$RENDERED"
assert_not_contains "rendered stdout never asserts a false YOUR WORKTREE path for this --pr spawn" "YOUR WORKTREE" "$RENDERED"

echo "=== item 2 — follow the route, do not just read it ==="
ROUTE="$(printf '%s\n' "$RENDERED" | sed -n '/<!-- PR_AMEND_ROUTE:BEGIN -->/,/<!-- PR_AMEND_ROUTE:END -->/p' | sed '1d;$d')"
if [ -z "$ROUTE" ]; then
  bad "no route commands were found between the markers in real stdout — cannot follow what was never rendered"
else
  ok "extracted $(printf '%s\n' "$ROUTE" | grep -c .) route command line(s) verbatim from real stdout"

  # The unrelated tree (D#2542's realistic starting state): a plain clone of
  # the code plane's own main, never the PR's branch.
  ROUTE_DIR="$WORK/unrelated-tree"
  CLONE_ERR="$WORK/clone.err"
  timeout --kill-after=5s 60 git clone --quiet "https://github.com/${CODE_REPO}.git" "$ROUTE_DIR" 2>"$CLONE_ERR"
  CLONE_RC=$?
  if [ "$CLONE_RC" -ne 0 ]; then
    bad "could not clone $CODE_REPO to build the starting tree the route is supposed to work from" "$(cat "$CLONE_ERR")"
  else
    ok "built the unrelated starting tree: a plain clone of ${CODE_REPO}'s main, distinct from PR #${PR_NUM}'s branch"
    mkdir -p "$ROUTE_DIR/.autonomous-team"
    printf '{"code_repo": "%s"}\n' "$CODE_REPO" > "$ROUTE_DIR/.autonomous-team/config.json"

    ROUTE_SCRIPT="$WORK/route.sh"
    {
      echo '#!/usr/bin/env bash'
      echo 'set -uo pipefail'
      printf '%s\n' "$ROUTE"
    } > "$ROUTE_SCRIPT"

    ROUTE_LOG="$WORK/route.log"
    ( cd "$ROUTE_DIR" && timeout --kill-after=5s 30 bash "$ROUTE_SCRIPT" ) >"$ROUTE_LOG" 2>&1
    ROUTE_RC=$?
    if [ "$ROUTE_RC" -ne 0 ]; then
      bad "the prescribed route did not complete (the fetch step failed)" "rc=$ROUTE_RC log: $(cat "$ROUTE_LOG")"
    else
      ok "the prescribed route ran to completion from the unrelated tree"
      ACTUAL_HEAD="$(git -C "$ROUTE_DIR" rev-parse FETCH_HEAD 2>/dev/null || true)"
      if [ "$ACTUAL_HEAD" = "$PR_HEAD_EXPECTED" ]; then
        ok "FETCH_HEAD after following the route equals PR #${PR_NUM}'s real head sha ($ACTUAL_HEAD)"
      else
        bad "FETCH_HEAD after following the route does not equal PR #${PR_NUM}'s real head sha (the route does not arrive)" "expected $PR_HEAD_EXPECTED got ${ACTUAL_HEAD:-<none>}"
      fi
    fi
  fi
fi

echo "=== item 7 — existing unit-test coverage is not replaced by this file ==="
if [ -f "$REPO_ROOT/backend/tests/test_prompt_builder.py" ]; then
  ok "backend/tests/test_prompt_builder.py still exists alongside this suite"
else
  bad "backend/tests/test_prompt_builder.py is missing — item 7 requires this suite to add a claim, not retire one"
fi

summarize_and_exit
