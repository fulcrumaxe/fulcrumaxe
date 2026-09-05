#!/usr/bin/env bash
# tests/test_post_merge_hook_pull.sh — tests for the auto_pull step of
# scripts/post-merge-hook.sh, which ships as scripts/lib/auto-pull-step.sh.
#
# Run: bash tests/test_post_merge_hook_pull.sh
# Expects: all assertions pass, exit 0
#
# These tests source and call the *shipping* function. They do not copy it into
# a heredoc. The previous version of this file did, and the copy sat unresynced
# through 37 commits to the hook: by the end it was asserting the `rm -f` on
# filenames-parsed-from-stderr that D#1911 / PR #1954 deleted, and asserting a
# message string with zero occurrences in the real script. Every assertion was
# green the whole time. That is the failure mode this rewrite exists to remove
# (D#1948), and it is why tests/test_no_heredoc_hook_copies.sh now guards it.
#
# Hermetic: every fixture is a throwaway pair of git repos under `mktemp -d`,
# built at run time. Nothing here touches the operator's checkout, the network,
# or the GitHub API:
#
#   * the function under test takes repo_root as an argument, so it is pointed
#     at the fixture and cannot reach the real repo;
#   * team-log writes go through auto_pull_step_teamlog, which is redefined
#     below to append to a temp file — rotate-team-log.sh is invoked by absolute
#     path inside the lib, so a PATH stub would not have reached it;
#   * AUTONOMOUS_TEAM_STATE_DIR is repointed at a temp dir, because the
#     pull-success path clears a stale auto-pull-blocked marker and would
#     otherwise delete the operator's real one;
#   * `gh` is stubbed on PATH for the whole file and `_REPO` is pointed at a
#     fixture string — the modified-file collision's escalation path (D#2301)
#     shells out to `gh issue list/create/comment` directly, unlike the
#     team-log write above, which is a function-level seam. Do not remove this
#     stub even for a test that "shouldn't" reach that branch: a real `gh` on
#     PATH with no `--repo` override falls back to the invoking directory's
#     git remote, which is this actual repo — that happened once while this
#     file was being developed and filed a live Issue (closed as noise);
#   * the only `git` remote in play is a local directory.

set -uo pipefail

# The step routes on git's English error text. The two pulls force LC_ALL=C
# themselves (D#1911 item 9); the fetch in Test 7 does not, so pin the locale
# here to keep that assertion deterministic. The production gap on that one
# fetch is real and deliberately untouched — this change is a move, not a fix.
export LC_ALL=C

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The shipping lib, sourced as-is — no copy, no heredoc.
# shellcheck source=scripts/lib/auto-pull-step.sh
source "${REAL_REPO_ROOT}/scripts/lib/auto-pull-step.sh"

PASS=0
FAIL=0
ERRORS=()
FIXTURES=()

TMP_STATE="$(mktemp -d)"
FIXTURES+=("$TMP_STATE")
export AUTONOMOUS_TEAM_STATE_DIR="$TMP_STATE/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"

# _REPO is what the unmerged-paths guard and (now) the modified-file collision
# guard pass to `gh --repo`. It is a plain global in the real hook (set once in
# post-merge-hook.sh, read by the sourced lib) — set it here for the same
# reason, and to a fixture value so a stub-miss is loud rather than silently
# reaching a real repo.
_REPO="fixture-org/fixture-repo"

# gh is stubbed on PATH for the whole file — see the header comment above for
# why this one is not optional. Calls are appended to $GH_CALL_LOG.
GH_STUB_DIR="$TMP_STATE/bin"
mkdir -p "$GH_STUB_DIR"
GH_CALL_LOG="$TMP_STATE/gh-calls.txt"
: > "$GH_CALL_LOG"
cat > "$GH_STUB_DIR/gh" <<'GHSTUB'
#!/usr/bin/env bash
echo "$*" >> "${GH_CALL_LOG:?GH_CALL_LOG must be set for the gh stub}"
if [[ "$*" == *"issue create"* ]]; then
  echo "https://github.com/fixture-org/fixture-repo/issues/1"
  exit 0
fi
if [[ "$*" == *"issue list"* ]]; then
  echo "null"
  exit 0
fi
if [[ "$*" == *"issue comment"* ]]; then
  exit 0
fi
exit 0
GHSTUB
chmod +x "$GH_STUB_DIR/gh"
export GH_CALL_LOG
export PATH="$GH_STUB_DIR:$PATH"

TEAMLOG=""

# The seam. Overrides the lib's definition for the rest of this process.
auto_pull_step_teamlog() { printf 'TEAMLOG: %s\n' "$1" >> "$TEAMLOG"; }

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

# ── Assertions ────────────────────────────────────────────────────────────────

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label — expected to find: $needle"
    echo "    Output was: $haystack" >&2
  fi
}

assert_rc() {
  local label="$1" rc="$2" want="$3"
  if [[ "$rc" == "$want" ]]; then
    pass "$label"
  else
    fail "$label — return code was $rc (expected $want)"
  fi
}

assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then
    pass "$label"
  else
    fail "$label — got [$got], expected [$want]"
  fi
}

assert_ne() {
  local label="$1" got="$2" unwanted="$3"
  if [[ "$got" != "$unwanted" ]]; then
    pass "$label"
  else
    fail "$label — got [$got], expected anything else"
  fi
}

assert_file() {
  local label="$1" path="$2"
  if [[ -f "$path" ]]; then
    pass "$label"
  else
    fail "$label — missing file: $path"
  fi
}

assert_no_file() {
  local label="$1" path="$2"
  if [[ ! -e "$path" ]]; then
    pass "$label"
  else
    fail "$label — file should be gone: $path"
  fi
}

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

# Builds $T / $T_ORIGIN / $T_LOCAL and a fresh, empty team-log capture file.
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
}

# Runs the shipping function against the fixture. Sets RC, OUT, COMBINED.
run_step() {
  OUT="$(auto_pull_step "$T_LOCAL" 2>&1)" && RC=0 || RC=$?
  COMBINED="$OUT
$(cat "$TEAMLOG" 2>/dev/null || true)"
}

head_of() { git -C "$1" rev-parse HEAD 2>/dev/null || echo ""; }
branch_of() { git -C "$1" branch --show-current 2>/dev/null || echo ""; }

# ── Test 1: clean pull — local is one commit behind origin ───────────────────
echo "Test 1: Clean pull case"
new_fixture
echo "new-content" > "$T_ORIGIN/newfile.txt"
git -C "$T_ORIGIN" add .
git -C "$T_ORIGIN" commit -m "advance origin" -q

run_step
assert_rc "test1: returns 0 (caller marks the step done)" "$RC" "0"
assert_contains "test1: pulled main successfully" "$COMBINED" "auto-pull: pulled main successfully"
assert_eq "test1: HEAD advanced to match origin" "$(head_of "$T_LOCAL")" "$(head_of "$T_ORIGIN")"

# ── Test 2: untracked-collision case ─────────────────────────────────────────
# The old copy asserted "removed N stale untracked files and pulled cleanly" —
# a string the real hook has never emitted, from a `rm -f` the real hook no
# longer does. The shipping behaviour is a move into archive/ (D#1911).
echo "Test 2: Untracked-collision case"
new_fixture
echo "from-origin" > "$T_ORIGIN/conflict.txt"
git -C "$T_ORIGIN" add .
git -C "$T_ORIGIN" commit -m "add conflict.txt" -q
echo "local-stale" > "$T_LOCAL/conflict.txt"   # untracked, collides with the incoming add

run_step
assert_rc "test2: returns 0 after recovering and retrying" "$RC" "0"
assert_contains "test2: reports an untracked collision was recovered" "$COMBINED" \
  "auto-pull recovered an untracked collision"
assert_eq "test2: HEAD advanced after the retry" "$(head_of "$T_LOCAL")" "$(head_of "$T_ORIGIN")"
DISPLACED="$(find "$T_LOCAL/archive" -name 'conflict.txt' -type f 2>/dev/null | head -1)"
assert_file "test2: the colliding file was displaced into archive/, not deleted" "$DISPLACED"
assert_eq "test2: the displaced file kept its contents" "$(cat "$DISPLACED" 2>/dev/null)" "local-stale"

# ── Test 3: modified-file case, kill switch on ───────────────────────────────
# This is the exact fixture and the exact assertions this test always had.
# Under the default (non-kill-switch) behaviour added by D#2301, this fixture
# no longer skips — both sides append a new final line after the same last
# common line, which is genuinely ambiguous to a 3-way merge (verified via
# `git merge-tree` before writing this), so it now takes the *overlapping*
# path (see Test 11) and no longer leaves HEAD untouched. That is an
# intentional behaviour change, not a regression, but it means this exact
# fixture is no longer a legitimate way to exercise "declined, nothing
# touched" on its own — it is, however, exactly what AC-8 needs: proof that
# the opt-out env var reproduces *today's* behaviour bit-for-bit. So this test
# keeps its original fixture and its original assertions completely unchanged,
# with only the kill switch added around it.
echo "Test 3: Modified-file case (AUTO_PULL_STASH_RECOVER_DISABLE=1 — AC-8)"
new_fixture
echo "origin-update" >> "$T_ORIGIN/file1.txt"
git -C "$T_ORIGIN" add .
git -C "$T_ORIGIN" commit -m "update file1" -q
echo "local-modification" >> "$T_LOCAL/file1.txt"   # tracked + dirty: ff-only will refuse
BEFORE_HEAD="$(head_of "$T_LOCAL")"

export AUTO_PULL_STASH_RECOVER_DISABLE=1
run_step
unset AUTO_PULL_STASH_RECOVER_DISABLE
assert_rc "test3: returns 1 (declined — caller must not mark the step)" "$RC" "1"
assert_ne "test3: not fatal, so the hook runs its remaining steps" "$RC" "2"
assert_contains "test3: warning reached the team log" "$COMBINED" "auto-pull SKIPPED"
assert_eq "test3: HEAD did not advance (pull correctly skipped)" "$(head_of "$T_LOCAL")" "$BEFORE_HEAD"
STASH_COUNT_T3="$(git -C "$T_LOCAL" stash list | wc -l | tr -d ' ')"
assert_eq "test3: kill switch creates no stash entry" "$STASH_COUNT_T3" "0"

# ── Test 4: wrong branch, clean tree — must switch to main and pull ──────────
echo "Test 4: Wrong-branch case (clean tree)"
new_fixture
echo "new-content-t4" > "$T_ORIGIN/newfile-t4.txt"
git -C "$T_ORIGIN" add .
git -C "$T_ORIGIN" commit -m "advance origin for test4" -q
git -C "$T_LOCAL" checkout -b feature/test-branch -q   # simulates an executor checkout leak

run_step
assert_rc "test4: returns 0" "$RC" "0"
assert_contains "test4: contamination warning emitted" "$COMBINED" "instead of main"
assert_contains "test4: reports switching back" "$COMBINED" "Switched from"
assert_contains "test4: pulled after the branch switch" "$COMBINED" "pulled main successfully"
assert_eq "test4: HEAD advanced to match origin" "$(head_of "$T_LOCAL")" "$(head_of "$T_ORIGIN")"
assert_eq "test4: fixture ended up on main" "$(branch_of "$T_LOCAL")" "main"

# ── Test 5: wrong branch, dirty tree — fatal, and nothing is clobbered ──────
# The old copy called hook_event_mark_step here before exiting, which would have
# suppressed the retry the real hook deliberately preserves. rc 2 is what keeps
# the caller exiting *without* marking.
echo "Test 5: Wrong-branch case (dirty tree)"
new_fixture
git -C "$T_LOCAL" checkout -b feature/dirty-branch -q
echo "uncommitted" >> "$T_LOCAL/file1.txt"
BEFORE_HEAD="$(head_of "$T_LOCAL")"

run_step
assert_rc "test5: returns 2 (fatal — caller exits 1 without marking)" "$RC" "2"
assert_contains "test5: uncommitted-changes error emitted" "$COMBINED" "uncommitted changes"
assert_eq "test5: stayed on the feature branch (no clobber)" "$(branch_of "$T_LOCAL")" "feature/dirty-branch"
assert_eq "test5: HEAD untouched" "$(head_of "$T_LOCAL")" "$BEFORE_HEAD"

# ── Test 6: already current — success without touching anything ─────────────
echo "Test 6: Already-current case"
new_fixture
BEFORE_HEAD="$(head_of "$T_LOCAL")"

run_step
assert_rc "test6: returns 0 (already current still counts as done)" "$RC" "0"
assert_eq "test6: HEAD unchanged" "$(head_of "$T_LOCAL")" "$BEFORE_HEAD"
assert_eq "test6: nothing written to the team log" "$(cat "$TEAMLOG")" ""

# ── Test 7: fetch reports no such ref — the force-reset recovery ───────────
# This branch does a destructive `checkout -B main origin/main`. It had no
# coverage at all before the extraction, because there was no way to run it
# against anything but the operator's own checkout.
echo "Test 7: Fetch reports no such ref"
new_fixture
echo "second" > "$T_ORIGIN/second.txt"
git -C "$T_ORIGIN" add .
git -C "$T_ORIGIN" commit -m "advance origin" -q
# Local learns origin/main, but its own main stays a commit behind. That gap is
# what makes this discriminating: the recovery has to move HEAD, and with the
# remote ref gone nothing else in the step can.
git -C "$T_LOCAL" fetch origin main -q
git -C "$T_ORIGIN" branch -m main trunk    # origin no longer has a main ref
TARGET="$(head_of "$T_ORIGIN")"

run_step
assert_rc "test7: returns 0 after the force-reset recovery" "$RC" "0"
assert_contains "test7: reports the forced reset" "$COMBINED" "forcing reset to origin/main"
assert_eq "test7: still on main afterwards" "$(branch_of "$T_LOCAL")" "main"
assert_eq "test7: HEAD was reset onto the last known origin/main" "$(head_of "$T_LOCAL")" "$TARGET"

# ── D#2301: the modified-file branch gets the same two patterns its siblings
# already have — bounded, non-destructive self-remediation (like the
# untracked-collision branch above) and, when that isn't safe, escalation
# (like the unmerged-paths branch). Tests 8-13 exercise this through the real
# auto_pull_step, per D#2149 — no dry-run, no pasted copy of the step.

# add_tracked_file_and_sync <relpath> <content> — commits a new file to
# T_ORIGIN and pulls it into T_LOCAL so both sides start in sync before they
# diverge. Requires an active new_fixture (T_ORIGIN / T_LOCAL set).
add_tracked_file_and_sync() {
  local relpath="$1" content="$2" dir
  dir="$(dirname "$relpath")"
  [[ "$dir" != "." ]] && mkdir -p "$T_ORIGIN/$dir"
  printf '%s\n' "$content" > "$T_ORIGIN/$relpath"
  git -C "$T_ORIGIN" add -- "$relpath"
  git -C "$T_ORIGIN" commit -m "add $relpath" -q
  git -C "$T_LOCAL" pull -q --ff-only origin main
}

# ── Tests 8-10: AC-3 / AC-4 — non-overlapping divergence is remediated,
# file-agnostically. Local edits hunk A (last line), upstream edits hunk B
# (first line) of the same file — a genuinely non-conflicting 3-way merge,
# confirmed with `git merge-tree` while this test was written.
run_nonoverlap_fixture() {
  local relpath="$1"
  new_fixture
  add_tracked_file_and_sync "$relpath" $'alpha\nbeta\ngamma'
  printf 'alpha-upstream\nbeta\ngamma\n' > "$T_ORIGIN/$relpath"
  git -C "$T_ORIGIN" commit -am "upstream edits $relpath (hunk B)" -q
  printf 'alpha\nbeta\ngamma-local\n' > "$T_LOCAL/$relpath"   # hunk A, uncommitted

  run_step
  assert_rc "nonoverlap($relpath): returns 0" "$RC" "0"
  assert_eq "nonoverlap($relpath): HEAD equals origin/main" "$(head_of "$T_LOCAL")" "$(head_of "$T_ORIGIN")"
  local content
  content="$(cat "$T_LOCAL/$relpath")"
  assert_contains "nonoverlap($relpath): keeps local hunk A" "$content" "gamma-local"
  assert_contains "nonoverlap($relpath): picks up upstream hunk B" "$content" "alpha-upstream"
  local stash_left
  stash_left="$(git -C "$T_LOCAL" stash list | wc -l | tr -d ' ')"
  assert_eq "nonoverlap($relpath): no stash left behind" "$stash_left" "0"
}

echo "Test 8: Non-overlapping modified-file collision is remediated (.autonomous-team/config.json)"
run_nonoverlap_fixture ".autonomous-team/config.json"
echo "Test 9: Non-overlapping modified-file collision is remediated (CLAUDE.md)"
run_nonoverlap_fixture "CLAUDE.md"
echo "Test 10: Non-overlapping modified-file collision is remediated (.autonomous-team/agent-profiles.json)"
run_nonoverlap_fixture ".autonomous-team/agent-profiles.json"

# ── Test 11: AC-5 — overlapping divergence loses nothing ────────────────────
# Local and upstream edit the exact same line. `git merge-tree` (used inside
# auto_pull_recover_modified) proves this would conflict, so the stash is
# pushed and the pull lands, but the reapply is declined rather than risking a
# conflicted `git stash pop` — which would leave "UU" entries the operator did
# not have before the call.
echo "Test 11: Overlapping modified-file collision loses nothing"
new_fixture
add_tracked_file_and_sync ".autonomous-team/config.json" $'alpha\nbeta\ngamma'
printf 'alpha-upstream\nbeta\ngamma\n' > "$T_ORIGIN/.autonomous-team/config.json"
git -C "$T_ORIGIN" commit -am "upstream edits config.json" -q
printf 'alpha-local\nbeta\ngamma\n' > "$T_LOCAL/.autonomous-team/config.json"   # same line as upstream

run_step
assert_ne "test11: returns non-zero (declined)" "$RC" "0"
STASH_LIST_T11="$(git -C "$T_LOCAL" stash list)"
if [[ -n "$STASH_LIST_T11" ]]; then pass "test11: stash list is non-empty"; else fail "test11: stash list is non-empty — got none"; fi
assert_contains "test11: message names the stash ref" "$COMBINED" "stash@{"
UNMERGED_T11="$(git -C "$T_LOCAL" status --porcelain | grep -c '^UU' || true)"
assert_eq "test11: no unmerged ('UU') entries introduced" "$UNMERGED_T11" "0"
RECOVERED_T11="$(git -C "$T_LOCAL" stash show -p stash@{0} 2>/dev/null | grep -c 'alpha-local' || true)"
if [[ "$RECOVERED_T11" != "0" ]]; then
  pass "test11: original content is recoverable from the stash"
else
  fail "test11: original content is recoverable from the stash — not found in stash@{0}"
fi
assert_contains "test11: no stash drop occurred (entry still present)" "$(git -C "$T_LOCAL" stash list)" "stash@{0}"

# ── Test 12: AC-6 / AC-9 — escalation fires once, not per merge, and reports
# drift depth. Forces a *pre-flight* decline that git's own `pull --ff-only`
# still reaches (unlike a real MERGE_HEAD, which git refuses to even attempt
# a pull against, before this code ever sees it): an unrelated file is staged
# so the pre-flight staged-index gate declines every call identically, while
# the colliding file's plain unstaged local edit is still what makes git's
# pull fail with the "local changes" error this branch matches on.
echo "Test 12: Escalation fires once, not per merge"
# Test 11 above also declines through the real escalation path and so also
# writes this marker — clear it so this test starts from a genuinely fresh
# "first occurrence" state rather than inheriting Test 11's.
rm -f "${AUTONOMOUS_TEAM_STATE_DIR}/auto-pull-blocked-modified"
new_fixture
add_tracked_file_and_sync ".autonomous-team/config.json" $'alpha\nbeta'
add_tracked_file_and_sync "unrelated.txt" "unrelated-base"
printf 'alpha-upstream\nbeta\n' > "$T_ORIGIN/.autonomous-team/config.json"
git -C "$T_ORIGIN" commit -am "upstream edits config.json" -q
printf 'alpha-local\nbeta\n' > "$T_LOCAL/.autonomous-team/config.json"   # unstaged, collides with upstream
printf 'unrelated-staged\n' > "$T_LOCAL/unrelated.txt"
git -C "$T_LOCAL" add -- unrelated.txt   # staged, uncommitted — persistent pre-flight decline

: > "$GH_CALL_LOG"
run_step
assert_ne "test12: first call returns non-zero (declined)" "$RC" "0"
assert_contains "test12: first call emits a needs-boss warning" "$COMBINED" "needs-boss"
assert_contains "test12: message reports how many commits behind origin/main" "$COMBINED" "commit(s) behind origin/main"
assert_file "test12: marker written on first call" "${AUTONOMOUS_TEAM_STATE_DIR}/auto-pull-blocked-modified"
assert_contains "test12: first call opened a Bug Issue" "$(cat "$GH_CALL_LOG")" "issue create"
FIRST_CALL_COUNT_T12="$(wc -l < "$GH_CALL_LOG" | tr -d ' ')"

run_step
assert_ne "test12: second call still returns non-zero" "$RC" "0"
assert_contains "test12: second call suppresses the duplicate warning" "$COMBINED" "already reported"
SECOND_CALL_COUNT_T12="$(wc -l < "$GH_CALL_LOG" | tr -d ' ')"
assert_eq "test12: second call opened no additional Bug Issue" "$SECOND_CALL_COUNT_T12" "$FIRST_CALL_COUNT_T12"

# Clear the condition and confirm a later successful pull clears the marker.
git -C "$T_LOCAL" reset -q -- unrelated.txt   # unstage — index reset only, no file content touched
git -C "$T_LOCAL" stash -- .autonomous-team/config.json unrelated.txt >/dev/null 2>&1   # discard local edits cleanly, no reset --hard
git -C "$T_LOCAL" stash drop >/dev/null 2>&1
run_step
assert_rc "test12: recovers once unblocked" "$RC" "0"
assert_no_file "test12: marker cleared after a later successful pull" "${AUTONOMOUS_TEAM_STATE_DIR}/auto-pull-blocked-modified"

# ── Test 13: AC-7 — bound and pre-flight gates decline without acting
# partially. Calls auto_pull_recover_modified directly (it is in scope: this
# file sources auto-pull-step.sh, which sources auto-pull-stash-recover.sh),
# the same way tests/test_auto_pull_recover.sh drives the untracked-collision
# gates directly.
echo "Test 13: Pre-flight gates decline cleanly (bound, mid-merge, staged index)"

snapshot_tree() {
  ( cd "$1" && git status --porcelain=v1 --untracked-files=all \
    && find . -type f ! -path './.git/*' -exec sha256sum {} \; | sort )
}

echo "  13a: collision set over the bound (21 files)"
new_fixture
for i in $(seq -w 1 21); do
  printf 'v1\n' > "$T_ORIGIN/bulk-$i.txt"
done
git -C "$T_ORIGIN" add -A
git -C "$T_ORIGIN" commit -qm "add 21 files"
git -C "$T_LOCAL" pull -q --ff-only origin main
for i in $(seq -w 1 21); do
  printf 'upstream-v2\n' > "$T_ORIGIN/bulk-$i.txt"
done
git -C "$T_ORIGIN" commit -aqm "advance 21 files"
for i in $(seq -w 1 21); do
  printf 'local-v2\n' > "$T_LOCAL/bulk-$i.txt"
done
BEFORE_SNAP_13A="$(snapshot_tree "$T_LOCAL")"
auto_pull_recover_modified "$T_LOCAL" && REC_RC_13A=0 || REC_RC_13A=$?
assert_ne "13a: bound-exceeded collision declines" "$REC_RC_13A" "0"
assert_contains "13a: reports the bound" "$AUTO_PULL_STASH_SUMMARY" "over the bound of 20"
AFTER_SNAP_13A="$(snapshot_tree "$T_LOCAL")"
assert_eq "13a: tree byte-identical to before the call" "$AFTER_SNAP_13A" "$BEFORE_SNAP_13A"
STASH_COUNT_13A="$(git -C "$T_LOCAL" stash list | wc -l | tr -d ' ')"
assert_eq "13a: nothing stashed" "$STASH_COUNT_13A" "0"

echo "  13b: repo mid-merge/rebase/cherry-pick"
new_fixture
add_tracked_file_and_sync ".autonomous-team/config.json" "a"
printf 'a-upstream\n' > "$T_ORIGIN/.autonomous-team/config.json"
git -C "$T_ORIGIN" commit -am "advance config.json" -q
printf 'a-local\n' > "$T_LOCAL/.autonomous-team/config.json"
touch "$T_LOCAL/.git/MERGE_HEAD"
BEFORE_SNAP_13B="$(snapshot_tree "$T_LOCAL")"
auto_pull_recover_modified "$T_LOCAL" && REC_RC_13B=0 || REC_RC_13B=$?
assert_ne "13b: mid-merge collision declines" "$REC_RC_13B" "0"
assert_contains "13b: reports mid-merge/rebase/cherry-pick" "$AUTO_PULL_STASH_SUMMARY" "mid-merge"
AFTER_SNAP_13B="$(snapshot_tree "$T_LOCAL")"
assert_eq "13b: tree byte-identical to before the call" "$AFTER_SNAP_13B" "$BEFORE_SNAP_13B"
rm -f "$T_LOCAL/.git/MERGE_HEAD"

echo "  13c: staged-but-uncommitted index entries present"
new_fixture
add_tracked_file_and_sync ".autonomous-team/config.json" "a"
printf 'a-upstream\n' > "$T_ORIGIN/.autonomous-team/config.json"
git -C "$T_ORIGIN" commit -am "advance config.json" -q
printf 'a-local\n' > "$T_LOCAL/.autonomous-team/config.json"
git -C "$T_LOCAL" add -- .autonomous-team/config.json   # staged, uncommitted
BEFORE_SNAP_13C="$(snapshot_tree "$T_LOCAL")"
auto_pull_recover_modified "$T_LOCAL" && REC_RC_13C=0 || REC_RC_13C=$?
assert_ne "13c: staged-index collision declines" "$REC_RC_13C" "0"
assert_contains "13c: reports staged entries" "$AUTO_PULL_STASH_SUMMARY" "staged"
AFTER_SNAP_13C="$(snapshot_tree "$T_LOCAL")"
assert_eq "13c: tree byte-identical to before the call" "$AFTER_SNAP_13C" "$BEFORE_SNAP_13C"

# ── Test 14: AC-10 — the colliding set comes from plumbing, not git's prose ──
echo "Test 14: The colliding set comes from plumbing (AC-10)"
if grep -nE 'grep -A20|following files' "${REAL_REPO_ROOT}/scripts/lib/auto-pull-stash-recover.sh" >/dev/null 2>&1; then
  fail "AC-10: scripts/lib/auto-pull-stash-recover.sh derives the collision set from git's prose"
else
  pass "AC-10: scripts/lib/auto-pull-stash-recover.sh derives the collision set from plumbing only"
fi

# ── Test 15: AC-4 — no filename is hardcoded in the implementation ──────────
echo "Test 15: No filename is hardcoded in the implementation (AC-4)"
if grep -nE 'config\.json|CLAUDE\.md|agent-profiles' "${REAL_REPO_ROOT}/scripts/lib/auto-pull-stash-recover.sh" >/dev/null 2>&1; then
  fail "AC-4: scripts/lib/auto-pull-stash-recover.sh names a specific file"
else
  pass "AC-4: scripts/lib/auto-pull-stash-recover.sh is file-agnostic"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
