#!/usr/bin/env bash
# tests/test_run_pr_tests_tree_guard.sh — unit tests for the tree guard added
# to scripts/run-pr-tests.sh (D#2365).
#
# Before this guard, run-pr-tests.sh routed a PR's changed files to test
# suites via `gh`, then ran those suites against whatever tree the process
# happened to be invoked from -- never checking that tree actually contained
# the PR. This suite is separate from tests/test_run_pr_tests_routing.sh on
# purpose (Spec item 7): that file is denylisted as flaky in
# scripts/run-pr-tests.sh's own BASH_SUITE_DENYLIST, and assertions added
# there would inherit that flakiness.
#
# `gh` and `python3 -m pytest` are stubbed on PATH, same idiom as
# test_run_pr_tests_routing.sh. `git` is also stubbed here, but as a pure
# passthrough to the real binary unless STUB_ANCESTOR_ALWAYS_TRUE=1 (used
# only by the mutation-check test) -- every other test exercises the real
# `git merge-base --is-ancestor` against a real repo built in $TEST_DIR.
#
# Usage: bash tests/test_run_pr_tests_tree_guard.sh -- exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REAL_PYTHON3="$(command -v python3)"
REAL_GIT="$(command -v git)"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

setup() {
  TEST_DIR=$(mktemp -d)
  mkdir -p "$TEST_DIR/scripts/lib" "$TEST_DIR/tests" "$TEST_DIR/bin"
  cp "$REPO_ROOT/scripts/run-pr-tests.sh" "$TEST_DIR/scripts/"
  cp "$REPO_ROOT/scripts/lib/repo-resolve.sh" "$REPO_ROOT/scripts/lib/worktree-ground-check.sh" "$TEST_DIR/scripts/lib/"

  git init -q "$TEST_DIR"
  git -C "$TEST_DIR" config user.email "test@example.com"
  git -C "$TEST_DIR" config user.name "test"
  echo "seed" >"$TEST_DIR/seed.txt"
  git -C "$TEST_DIR" add seed.txt
  git -C "$TEST_DIR" commit -q -m "seed"

  # gh stub: `pr view --json headRefOid --jq '.headRefOid'` returns
  # $GH_HEAD_SHA; `pr diff --name-only` / `pr view --json files` return
  # $GH_FILES. Checked in that order since a real invocation's args always
  # contain "pr view" for both shapes -- headRefOid must be checked first.
  cat >"$TEST_DIR/bin/gh" <<'GHEOF'
#!/usr/bin/env bash
argstr="$*"
if [[ "$argstr" == *"headRefOid"* ]]; then
  printf '%s\n' "${GH_HEAD_SHA:-}"
  exit 0
fi
if [[ "$argstr" == *"pr diff"* || "$argstr" == *"pr view"* ]]; then
  printf '%s\n' "${GH_FILES:-}"
  exit 0
fi
echo "[]"
GHEOF
  chmod +x "$TEST_DIR/bin/gh"

  # python3 stub: intercepts `-m pytest` (sleeps if FAKE_PYTEST_SLEEP is
  # set), delegates everything else -- including the guard's own
  # MEASURED_TREE_JSON build -- to the real interpreter.
  cat >"$TEST_DIR/bin/python3" <<PYEOF
#!/usr/bin/env bash
if [ "\$1" = "-m" ] && [ "\$2" = "pytest" ]; then
  [ "\${FAKE_PYTEST_SLEEP:-0}" != "0" ] && sleep "\${FAKE_PYTEST_SLEEP}"
  exit "\${FAKE_PYTEST_EXIT:-0}"
fi
exec "$REAL_PYTHON3" "\$@"
PYEOF
  chmod +x "$TEST_DIR/bin/python3"

  # git stub: pure passthrough unless STUB_ANCESTOR_ALWAYS_TRUE=1, in which
  # case any `--is-ancestor` invocation short-circuits to success -- this is
  # the mutation-check test's way of "breaking the guard" (Spec item 5)
  # without touching run-pr-tests.sh's own source.
  cat >"$TEST_DIR/bin/git" <<'GITEOF'
#!/usr/bin/env bash
is_ancestor_call=0
for a in "$@"; do
  [ "$a" = "--is-ancestor" ] && is_ancestor_call=1
done
if [ "$is_ancestor_call" = "1" ] && [ "${STUB_ANCESTOR_ALWAYS_TRUE:-0}" = "1" ]; then
  exit 0
fi
exec "$REAL_GIT_BIN" "$@"
GITEOF
  chmod +x "$TEST_DIR/bin/git"
}

teardown() { rm -rf "$TEST_DIR"; }

# Appends a commit to $TEST_DIR's current branch, prints the new HEAD sha.
_commit() {
  echo "$1-$RANDOM" >>"$TEST_DIR/log.txt"
  git -C "$TEST_DIR" add -A >/dev/null
  git -C "$TEST_DIR" commit -q -m "$1" >/dev/null
  git -C "$TEST_DIR" rev-parse HEAD
}

# A commit sha that exists in a *different* repo, never fetched into
# $TEST_DIR -- used for the "object doesn't exist here at all" refusal case
# (exit 128 from `--is-ancestor`, distinct from "exists but not an
# ancestor", exit 1).
_foreign_sha() {
  local d sha
  d=$(mktemp -d)
  git init -q "$d"
  git -C "$d" config user.email "x@x.test"
  git -C "$d" config user.name "x"
  echo "x" >"$d/x.txt"
  git -C "$d" add x.txt
  git -C "$d" commit -q -m "x" >/dev/null
  sha=$(git -C "$d" rev-parse HEAD)
  rm -rf "$d"
  printf '%s' "$sha"
}

_run_script_env() {
  export PATH="$TEST_DIR/bin:$PATH" AUTONOMOUS_TEAM_REPO="test/repo"
  export GH_FILES="${GH_FILES:-}" GH_HEAD_SHA="${GH_HEAD_SHA:-}"
  export REAL_GIT_BIN="$REAL_GIT"
  export STUB_ANCESTOR_ALWAYS_TRUE="${STUB_ANCESTOR_ALWAYS_TRUE:-0}"
  export FAKE_PYTEST_SLEEP="${FAKE_PYTEST_SLEEP:-0}" FAKE_PYTEST_EXIT="${FAKE_PYTEST_EXIT:-0}"
  export RUN_PR_TESTS_PYTEST_TIMEOUT="${RUN_PR_TESTS_PYTEST_TIMEOUT:-}"
  export RUN_PR_TESTS_BASH_TIMEOUT="${RUN_PR_TESTS_BASH_TIMEOUT:-}"
  export FLAKY_HISTORY_PATH="$TEST_DIR/flaky-history.jsonl"
}

run_script() {
  ( _run_script_env; bash "$TEST_DIR/scripts/run-pr-tests.sh" "$1" ) 2>"$TEST_DIR/stderr.log"
}

# Same real-world shape as reviewer-measured mid-run kills: the whole script
# runs under an enclosing `timeout` without --foreground.
run_script_bounded() {
  local bound="$1" pr="$2"
  ( _run_script_env; timeout --kill-after=5s "$bound" bash "$TEST_DIR/scripts/run-pr-tests.sh" "$pr" ) 2>"$TEST_DIR/stderr.log"
}

manifest_json() {
  printf '%s' "$1" | "$REAL_PYTHON3" -c '
import sys
s = sys.stdin.read()
idx = s.rfind("{\"routing\"")
print(s[idx:] if idx >= 0 else "")
'
}

# $1 = stdout, $2 = dotted field path (e.g. "measured_tree.head_sha")
mtree_field() {
  manifest_json "$1" | "$REAL_PYTHON3" -c "
import json, sys
d = json.load(sys.stdin)
cur = d
for part in '$2'.split('.'):
    cur = cur.get(part) if isinstance(cur, dict) else None
print(cur if cur is not None else '__MISSING__')
"
}

test_item1_script_resolves_headRefOid() {
  if grep -c 'headRefOid' "$REPO_ROOT/scripts/run-pr-tests.sh" | grep -qv '^0$'; then
    pass "item1: script resolves the PR's head sha via headRefOid"
  else
    fail "item1: script resolves the PR's head sha via headRefOid" "no headRefOid reference found"
  fi
}

test_item2_success_path_measured_tree() {
  setup
  local sha
  sha=$(git -C "$TEST_DIR" rev-parse HEAD)
  GH_HEAD_SHA="$sha"
  GH_FILES="docs/unrouted.md"

  local out path head pr_head
  out=$(run_script 9002)
  path=$(mtree_field "$out" "measured_tree.path")
  head=$(mtree_field "$out" "measured_tree.head_sha")
  pr_head=$(mtree_field "$out" "measured_tree.pr_head_sha")

  if [ "$path" = "$TEST_DIR" ]; then pass "item2: measured_tree.path names the invoking tree"
  else fail "item2: measured_tree.path names the invoking tree" "got: $path"; fi
  if [ "$head" = "$sha" ]; then pass "item2: measured_tree.head_sha is the tree's HEAD"
  else fail "item2: measured_tree.head_sha is the tree's HEAD" "got: $head"; fi
  if [ "$pr_head" = "$sha" ]; then pass "item2: measured_tree.pr_head_sha is the PR's head"
  else fail "item2: measured_tree.pr_head_sha is the PR's head" "got: $pr_head"; fi
  teardown
}

test_item3_partial_path_carries_measured_tree() {
  setup
  local sha
  sha=$(git -C "$TEST_DIR" rev-parse HEAD)
  GH_HEAD_SHA="$sha"
  GH_FILES="backend/foo.py"
  FAKE_PYTEST_SLEEP=60

  local out partial head
  out=$(run_script_bounded 6 9003)
  partial=$(manifest_json "$out" | "$REAL_PYTHON3" -c 'import json,sys; print(json.load(sys.stdin).get("partial"))')
  head=$(mtree_field "$out" "measured_tree.head_sha")

  if [ "$partial" = "True" ]; then pass "item3: a killed run's manifest carries partial:true"
  else fail "item3: a killed run's manifest carries partial:true" "got: $partial"; fi
  if [ "$head" = "$sha" ]; then pass "item3: the partial manifest also carries measured_tree"
  else fail "item3: the partial manifest also carries measured_tree" "got head_sha: $head"; fi
  teardown
}

test_item4_refusal_when_head_not_an_ancestor() {
  setup
  # branch-b diverges from branch-a's tip: branch-b's commit is a real,
  # resolvable object, but not present in branch-a's history -- the
  # "reviewer running from main, PR branch not merged" shape.
  git -C "$TEST_DIR" checkout -q -b branch-a
  local base
  base=$(_commit "base")
  git -C "$TEST_DIR" checkout -q -b branch-b "$base"
  local other
  other=$(_commit "other")
  git -C "$TEST_DIR" checkout -q branch-a

  GH_HEAD_SHA="$other"
  GH_FILES="docs/unrouted.md"

  local out rc stderr_out
  out=$(run_script 9004)
  rc=$?
  stderr_out=$(cat "$TEST_DIR/stderr.log")

  if [ "$rc" -eq 3 ]; then pass "item4: refusal exits 3"
  else fail "item4: refusal exits 3" "got rc=$rc"; fi

  if [ -z "$out" ] || ! printf '%s' "$out" | grep -q 'tests_run'; then
    pass "item4: refusal emits no tests_run entries"
  else
    fail "item4: refusal emits no tests_run entries" "stdout: $out"
  fi

  if printf '%s' "$stderr_out" | grep -q "$other" && printf '%s' "$stderr_out" | grep -q "$base"; then
    pass "item4: refusal names both the tree HEAD sha and the PR head sha on stderr"
  else
    fail "item4: refusal names both the tree HEAD sha and the PR head sha on stderr" "stderr: $stderr_out"
  fi
  teardown
}

test_item4b_refusal_when_head_unknown_to_tree() {
  setup
  local foreign
  foreign=$(_foreign_sha)
  GH_HEAD_SHA="$foreign"
  GH_FILES="docs/unrouted.md"

  local out rc
  out=$(run_script 9005)
  rc=$?

  if [ "$rc" -eq 3 ]; then pass "item4b: refusal exits 3 when the PR head is unknown to this tree's object graph"
  else fail "item4b: refusal exits 3 when the PR head is unknown to this tree's object graph" "got rc=$rc"; fi
  teardown
}

test_item5_mutation_check_guard_removal_changes_outcome() {
  setup
  git -C "$TEST_DIR" checkout -q -b branch-a
  local base
  base=$(_commit "base")
  git -C "$TEST_DIR" checkout -q -b branch-b "$base"
  local other
  other=$(_commit "other")
  git -C "$TEST_DIR" checkout -q branch-a

  GH_HEAD_SHA="$other"
  GH_FILES="docs/unrouted.md"

  local broken_rc
  STUB_ANCESTOR_ALWAYS_TRUE=1 run_script 9006 >/dev/null
  broken_rc=$?
  if [ "$broken_rc" -ne 3 ]; then
    pass "item5: with the ancestry check forced to succeed, the script no longer refuses (rc=$broken_rc)"
  else
    fail "item5: with the ancestry check forced to succeed, the script no longer refuses" "still got rc=3 -- the guard is not what produces the refusal"
  fi

  local restored_rc
  STUB_ANCESTOR_ALWAYS_TRUE=0 run_script 9006 >/dev/null
  restored_rc=$?
  if [ "$restored_rc" -eq 3 ]; then
    pass "item5: restoring the real ancestry check, the refusal returns (rc=3)"
  else
    fail "item5: restoring the real ancestry check, the refusal returns" "got rc=$restored_rc"
  fi
  echo "  (mutation check exit codes: broken=$broken_rc restored=$restored_rc)"
  teardown
}

test_item6_worktree_at_pr_head_runs_unregressed() {
  setup
  local sha
  cat >"$TEST_DIR/tests/test_merge_gate.sh" <<'FIXEOF'
#!/usr/bin/env bash
echo "fixture suite ran"
exit 0
FIXEOF
  sha=$(_commit "add fixture suite")
  GH_HEAD_SHA="$sha"
  GH_FILES="tests/test_merge_gate.sh"

  local out exit_code
  out=$(run_script 9007)
  exit_code=$(mtree_field "$out" "measured_tree.head_sha")
  local suite_exit
  suite_exit=$(manifest_json "$out" | "$REAL_PYTHON3" -c "
import json, sys
d = json.load(sys.stdin)
e = next((e for e in d['tests_run'] if 'test_merge_gate.sh' in e['command']), None)
print(e.get('exit_code') if e else '__MISSING__')
")
  if [ "$suite_exit" = "0" ]; then
    pass "item6: from a tree at the PR head, the routed suite still runs and exits 0 as before"
  else
    fail "item6: from a tree at the PR head, the routed suite still runs and exits 0 as before" "got: $suite_exit"
  fi
  teardown
}

test_item8_ancestry_descendant_accepted() {
  setup
  local base child
  base=$(git -C "$TEST_DIR" rev-parse HEAD)
  child=$(_commit "child")
  # PR head is the earlier commit; the tree's HEAD is a descendant of it --
  # must be accepted (a tree with the PR merged in).
  GH_HEAD_SHA="$base"
  GH_FILES="docs/unrouted.md"

  local out rc
  out=$(run_script 9008)
  rc=$?
  if [ "$rc" -eq 0 ] && [ "$(mtree_field "$out" "measured_tree.head_sha")" = "$child" ]; then
    pass "item8: a tree HEAD that is a descendant of the PR head is accepted"
  else
    fail "item8: a tree HEAD that is a descendant of the PR head is accepted" "rc=$rc out=$out"
  fi
  teardown
}

test_item1_script_resolves_headRefOid
test_item2_success_path_measured_tree
test_item3_partial_path_carries_measured_tree
test_item4_refusal_when_head_not_an_ancestor
test_item4b_refusal_when_head_unknown_to_tree
test_item5_mutation_check_guard_removal_changes_outcome
test_item6_worktree_at_pr_head_runs_unregressed
test_item8_ancestry_descendant_accepted

echo ""
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
