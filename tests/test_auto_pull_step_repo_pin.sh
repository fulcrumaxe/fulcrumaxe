#!/usr/bin/env bash
# tests/test_auto_pull_step_repo_pin.sh — D#2487: scripts/lib/auto-pull-step.sh
# reads ${_REPO:-} at six `gh issue` call sites and never assigned _REPO, so
# every one of the six ran as `gh ... --repo ""`. `gh --repo ""` exits 0 and
# silently resolves from the checkout's git remote instead of failing — and
# the six sites still grep as pinned, because `${_REPO:-}` reads as a guard.
#
# This test does not merely check that `gh` was invoked (that passed today,
# empty string and all). It asserts the *resolved slug* that actually reaches
# `--repo`, computed two independent ways:
#   1. `_REPO`, as the shipping lib resolves it via _resolve_discussion_repo
#      at source time (scripts/lib/repo-resolve.sh).
#   2. A plain read of .autonomous-team/config.json's discussion_repo/repo
#      field, done here with no dependency on repo-resolve.sh at all.
# If those two disagree, or either is empty, the test fails before it even
# gets to `gh`.
#
# Hermetic: every fixture is a throwaway git repo pair under `mktemp -d`.
# `gh` is stubbed on PATH; the six call sites' escalation-Issue calls are the
# only `gh` invocations the shipping lib makes, so a real `gh` on PATH would
# otherwise reach the checkout's actual git remote (see the header of
# tests/test_post_merge_hook_pull.sh — that happened once during that file's
# development and filed a live Issue). Nothing here touches the operator's
# checkout, the network, or the GitHub API.
#
# Mutation check performed while writing this test (not automated here —
# see the PR body for the result): with the `_REPO="$(_resolve_discussion_repo)"`
# assignment in scripts/lib/auto-pull-step.sh commented out, "repo_pin:
# unmerged-paths escalation (fresh issue) passes the resolved slug to gh
# issue create" goes red, because the guarded `${_REPO:?...}` form aborts the
# command substitution before `gh` ever runs — the call log stays empty.
#
# Run: bash tests/test_auto_pull_step_repo_pin.sh

set -uo pipefail
export LC_ALL=C

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Resolved once, before any PATH stubbing, so every fixture's git stub passes
# through to the real binary rather than chaining through an earlier fixture's
# stub (each new_fixture prepends a fresh stub dir onto PATH without removing
# the previous one).
REAL_GIT="$(command -v git)"

PASS=0
FAIL=0
ERRORS=()
FIXTURES=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then
    pass "$label"
  else
    fail "$label — got [$got], expected [$want]"
  fi
}

assert_ne_empty() {
  local label="$1" got="$2"
  if [[ -n "$got" ]]; then
    pass "$label"
  else
    fail "$label — value was empty"
  fi
}

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label — expected to find: $needle"
    echo "    Haystack was: $haystack" >&2
  fi
}

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

# ── The shipping lib, sourced as-is — no copy, no heredoc ────────────────────
# shellcheck source=scripts/lib/auto-pull-step.sh
source "${REAL_REPO_ROOT}/scripts/lib/auto-pull-step.sh"

# ── Part 1: the resolved slug itself, checked two independent ways ───────────
echo "Part 1: _REPO resolves to a real slug, independently confirmed"

INDEPENDENT_EXPECTED="$(python3 -c "
import json
d = json.load(open('${REAL_REPO_ROOT}/.autonomous-team/config.json'))
print(d.get('discussion_repo') or d.get('repo') or '')
" 2>/dev/null || echo "")"

assert_ne_empty "part1: independent config.json read resolves a non-empty repo" "$INDEPENDENT_EXPECTED"
# ${_REPO:-} rather than $_REPO here on purpose: this assertion is exactly
# what must fail cleanly (not crash the whole harness under this file's own
# `set -u`) if the shipping lib's source-time assignment is ever removed.
assert_ne_empty "part1: _REPO (as resolved at source time by the shipping lib) is non-empty" "${_REPO:-}"
assert_eq "part1: _REPO matches the independently-computed slug" "${_REPO:-}" "$INDEPENDENT_EXPECTED"

EXPECTED_REPO="${_REPO:-}"

# ── Fixtures ──────────────────────────────────────────────────────────────────

setup_fake_origin() {
  local origin_dir="$1"
  git -C "$origin_dir" init --initial-branch=main -q
  git -C "$origin_dir" config user.email "test@test.com"
  git -C "$origin_dir" config user.name "Test"
  echo "file1" > "$origin_dir/file1.txt"
  git -C "$origin_dir" add .
  git -C "$origin_dir" commit -m "init" -q
}

setup_fake_local() {
  local local_dir="$1" origin_dir="$2"
  git clone "$origin_dir" "$local_dir" -q --local
  git -C "$local_dir" config user.email "test@test.com"
  git -C "$local_dir" config user.name "Test"
}

new_fixture() {
  T="$(mktemp -d)"
  FIXTURES+=("$T")
  T_ORIGIN="$T/origin"
  T_LOCAL="$T/local"
  TEAMLOG="$T/teamlog.txt"
  mkdir -p "$T_ORIGIN"
  : > "$TEAMLOG"
  setup_fake_origin "$T_ORIGIN"
  setup_fake_local "$T_LOCAL" "$T_ORIGIN"

  # Fresh state dir per fixture — the guard's dedup marker must not leak
  # between tests in this file.
  AUTONOMOUS_TEAM_STATE_DIR="$T/state"
  mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"
  export AUTONOMOUS_TEAM_STATE_DIR
}

add_tracked_file_and_sync() {
  local relpath="$1" content="$2" dir
  dir="$(dirname "$relpath")"
  [[ "$dir" != "." ]] && mkdir -p "$T_ORIGIN/$dir"
  printf '%s\n' "$content" > "$T_ORIGIN/$relpath"
  git -C "$T_ORIGIN" add -- "$relpath"
  git -C "$T_ORIGIN" commit -m "add $relpath" -q
  git -C "$T_LOCAL" pull -q --ff-only origin main
}

# Seam the lib exposes: rotate-team-log.sh writes go through this function.
auto_pull_step_teamlog() { printf 'TEAMLOG: %s\n' "$1" >> "$TEAMLOG"; }

# gh stub: logs every invocation verbatim, then answers per the scripted
# LIST_REPLY for `issue list` so the caller controls list/create vs.
# list/comment.
GH_STUB_DIR=""
install_gh_stub() {
  local list_reply="$1"   # "null" → dedup branch opens a new issue
  GH_STUB_DIR="$T/bin"
  mkdir -p "$GH_STUB_DIR"
  GH_CALL_LOG="$T/gh-calls.txt"
  : > "$GH_CALL_LOG"
  cat > "$GH_STUB_DIR/gh" <<GHSTUB
#!/usr/bin/env bash
echo "\$*" >> "${GH_CALL_LOG}"
if [[ "\$*" == *"issue create"* ]]; then
  echo "https://github.com/fixture/repo/issues/1"
  exit 0
fi
if [[ "\$*" == *"issue list"* ]]; then
  echo "${list_reply}"
  exit 0
fi
if [[ "\$*" == *"issue comment"* ]]; then
  exit 0
fi
exit 0
GHSTUB
  chmod +x "$GH_STUB_DIR/gh"
  export GH_CALL_LOG
  export PATH="$GH_STUB_DIR:$PATH"
}

# git stub: intercepts `diff --name-only --diff-filter=U` to fake an
# already-unmerged index (the unmerged-paths guard reads that call
# pre-emptively, before attempting any pull — reproducing a real UU state
# would mean manufacturing an actual conflicted merge, which this technique
# avoids). Everything else passes through to the real git.
install_git_unmerged_stub() {
  local fake_file="$1"
  cat > "$GH_STUB_DIR/git" <<GITSTUB
#!/usr/bin/env bash
if [[ "\$*" == *"diff --name-only --diff-filter=U"* ]]; then
  echo "${fake_file}"
  exit 0
fi
exec "${REAL_GIT}" "\$@"
GITSTUB
  chmod +x "$GH_STUB_DIR/git"
}

extract_repo_args() {
  # Every `--repo <value>` token pair across the whole call log, one per line.
  grep -oE -- '--repo [^ ]+' "$GH_CALL_LOG" | sed 's/^--repo //'
}

# assert_repo_pin <label> <want_call_count> — the one assertion that actually
# proves the resolved slug reached `gh`, not merely that `gh` ran. Collapses
# "how many --repo args were logged" and "did every one of them match the
# resolved slug" into a single deterministic pass/fail so it cannot silently
# no-op: with _REPO unset (the D#2487 defect, or the guarded form aborting
# before gh ever runs), extract_repo_args() returns nothing, both counts land
# at 0, and this reports a clean FAIL under this exact name rather than
# crashing the harness or skipping quietly.
assert_repo_pin() {
  local label="$1" want_count="$2" args total matched r
  args="$(extract_repo_args)"
  total=0
  matched=0
  while IFS= read -r r; do
    [[ -z "$r" ]] && continue
    total=$((total + 1))
    [[ "$r" == "$EXPECTED_REPO" ]] && matched=$((matched + 1))
  done <<< "$args"
  assert_eq "$label" "count=${total} matched=${matched}" "count=${want_count} matched=${want_count}"
}

# ── Test A: unmerged-paths escalation, fresh issue (create) ──────────────────
# Exercises the first of the two duplicated blocks (scripts/lib/auto-pull-
# step.sh:~150-179): gh issue list, then gh issue create.
echo "Test A: unmerged-paths escalation, no existing issue (list + create)"
new_fixture
# The unmerged-paths check only runs once local and origin/main have
# diverged (LOCAL == REMOTE short-circuits straight to the up-to-date path,
# never reaching it) — advance origin by one commit first.
echo "new-content" > "$T_ORIGIN/newfile.txt"
git -C "$T_ORIGIN" add .
git -C "$T_ORIGIN" commit -m "advance origin" -q
install_gh_stub "null"
install_git_unmerged_stub "conflicted-file.txt"

OUT="$(auto_pull_step "$T_LOCAL" 2>&1)" || true

assert_contains "testA: escalation warning fired" "$(cat "$TEAMLOG")" "needs-boss"
assert_contains "testA: gh issue create was invoked" "$(cat "$GH_CALL_LOG")" "issue create"
# 2 = gh issue list + gh issue create
assert_repo_pin "repo_pin: unmerged-paths escalation (fresh issue) passes the resolved slug to every gh --repo call" 2

# ── Test B: unmerged-paths escalation, existing issue (comment) ──────────────
# Same block, other branch: gh issue list finds an open issue → gh issue
# comment, not gh issue create.
echo "Test B: unmerged-paths escalation, existing issue found (list + comment)"
new_fixture
echo "new-content" > "$T_ORIGIN/newfile.txt"
git -C "$T_ORIGIN" add .
git -C "$T_ORIGIN" commit -m "advance origin" -q
install_gh_stub "42"
install_git_unmerged_stub "another-conflicted-file.txt"

OUT="$(auto_pull_step "$T_LOCAL" 2>&1)" || true

assert_contains "testB: gh issue comment was invoked (not create)" "$(cat "$GH_CALL_LOG")" "issue comment"
# 2 = gh issue list + gh issue comment
assert_repo_pin "repo_pin: unmerged-paths escalation (existing issue) passes the resolved slug to every gh --repo call" 2

# ── Test C: modified-file collision escalation, fresh issue (create) ────────
# Exercises the second duplicated block (scripts/lib/auto-pull-step.sh:~277-
# 300). Recovery declines via the pre-flight staged-index gate: an unrelated
# staged file persists across the whole flow, so auto_pull_recover_modified
# refuses to touch anything, same fixture shape as the existing
# tests/test_post_merge_hook_pull.sh Test 12.
echo "Test C: modified-file collision escalation, no existing issue (list + create)"
new_fixture
install_gh_stub "null"
add_tracked_file_and_sync ".autonomous-team/config.json" $'alpha\nbeta'
add_tracked_file_and_sync "unrelated.txt" "unrelated-base"
printf 'alpha-upstream\nbeta\n' > "$T_ORIGIN/.autonomous-team/config.json"
git -C "$T_ORIGIN" commit -am "upstream edits config.json" -q
printf 'alpha-local\nbeta\n' > "$T_LOCAL/.autonomous-team/config.json"   # unstaged, collides with upstream
printf 'unrelated-staged\n' > "$T_LOCAL/unrelated.txt"
git -C "$T_LOCAL" add -- unrelated.txt   # staged, uncommitted — persistent pre-flight decline

OUT="$(auto_pull_step "$T_LOCAL" 2>&1)" || true

assert_contains "testC: escalation warning fired" "$(cat "$TEAMLOG")" "needs-boss"
assert_contains "testC: gh issue create was invoked" "$(cat "$GH_CALL_LOG")" "issue create"
# 2 = gh issue list + gh issue create
assert_repo_pin "repo_pin: modified-file collision escalation (fresh issue) passes the resolved slug to every gh --repo call" 2

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
exit 0
