#!/usr/bin/env bash
# test_reaper_removal_cap.sh — D#1917 acceptance tests (items 1-7).
#
# D#1917 found that WORKTREE_REAP_MAX_PER_PASS only bounded Step 6
# (git-tracked removal, opt-in only, off by default). Step 5 -- the
# no-registry-entry back-compat path that runs live on every invocation
# (reap-worktrees.sh --quiet, called after every agent completion) -- was
# uncapped. This file proves Step 5 is now capped by the SAME shared
# counter/cap as Step 6 (renamed removal_cap / removed_this_pass, was
# gt_removal_cap / git_tracked_removed_this_pass), and that a run with
# nothing to evaluate is distinguishable from a run that evaluated
# candidates and capped them.
#
# Every fixture is a throwaway git repo under $TMPDIR -- never this checkout.
# No item removes a directory the test did not itself create.
#
# Exit code: 0 = all tests passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_REAL="$(cd "$SCRIPT_DIR/.." && pwd)"
REGISTRY_LIB="${REPO_ROOT_REAL}/scripts/lib/worktree-registry.sh"

# ---------------------------------------------------------------------------
# Minimal test framework (matches tests/test_reaper_git_tracked_removal.sh)
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

_assert_exit0()        { [[ "$1" -eq 0 ]] && _pass "$2" || _fail "$2 (exit=$1)"; }
_assert_contains()     { echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (missing: '$2')"; }
_assert_not_contains() { ! echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (unexpected: '$2')"; }
_assert_eq()            { [[ "$1" == "$2" ]] && _pass "$3" || _fail "$3 (got '$1', expected '$2')"; }
_assert_dir_exists()    { [[ -d "$1" ]] && _pass "$2" || _fail "$2 (missing dir: $1)"; }
_assert_dir_missing()   { [[ ! -d "$1" ]] && _pass "$2" || _fail "$2 (should not exist: $1)"; }
_assert_file_missing()  { [[ ! -f "$1" ]] && _pass "$2" || _fail "$2 (should not exist: $1)"; }

# ===========================================================================
# Item 1: the old git-tracked-only variable name is fully retired.
# ===========================================================================
echo ""
echo "=== Item 1: gt_removal_cap is gone; a step-neutral name replaces it ==="

if grep -qn 'gt_removal_cap' "$REGISTRY_LIB"; then
  _fail "Item 1: gt_removal_cap still present in ${REGISTRY_LIB}"
else
  _pass "Item 1: grep for gt_removal_cap returns nothing"
fi

if grep -qn 'git_tracked_removed_this_pass' "$REGISTRY_LIB"; then
  _fail "Item 1: git_tracked_removed_this_pass still present in ${REGISTRY_LIB}"
else
  _pass "Item 1: grep for git_tracked_removed_this_pass returns nothing"
fi

if grep -qn 'local removal_cap=' "$REGISTRY_LIB" && grep -qn 'local removed_this_pass=' "$REGISTRY_LIB"; then
  _pass "Item 1: step-neutral removal_cap / removed_this_pass are present"
else
  _fail "Item 1: expected step-neutral removal_cap / removed_this_pass not found"
fi

# ===========================================================================
# Fixture plumbing (mirrors tests/test_reaper_git_tracked_removal.sh)
# ===========================================================================
TMPDIR_ROOT=$(mktemp -d /tmp/test-wtr-removal-cap-XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

ORIGIN="$TMPDIR_ROOT/origin.git"
git init --quiet --bare "$ORIGIN"

REPO="$TMPDIR_ROOT/repo"
git init --quiet -b main "$REPO"
git -C "$REPO" config user.email "test@test.com"
git -C "$REPO" config user.name "Test"
echo hello > "$REPO/README.md"
git -C "$REPO" add README.md
git -C "$REPO" commit --quiet -m init
git -C "$REPO" remote add origin "$ORIGIN"
git -C "$REPO" push --quiet -u origin main

WORKTREES_DIR="${REPO}/.claude/worktrees"
ARCHIVE_DIR="${REPO}/archive/orphan-diffs"
AUTONOMOUS_TEAM_DIR="${REPO}/.autonomous-team"
AUDIT_DIR="${TMPDIR_ROOT}/state-dir"
mkdir -p "$WORKTREES_DIR" "$ARCHIVE_DIR" "$AUTONOMOUS_TEAM_DIR" "$AUDIT_DIR"
printf '[]\n' > "${AUTONOMOUS_TEAM_DIR}/worktrees.json"

OLD_TS="202001010000"

# _add_wt <id> -- a real `git worktree add`, aged past any TTL used below.
# Step 6 (git-tracked removal) candidate. Matches
# test_reaper_git_tracked_removal.sh's fixture helper.
_add_wt() {
  local id="$1"
  git -C "$REPO" worktree add -q "${WORKTREES_DIR}/${id}" -b "branch-${id}" >/dev/null 2>&1
  touch -t "$OLD_TS" "${WORKTREES_DIR}/${id}"
}

# _make_step5_clean <dir> -- a standalone git repo directly under
# WORKTREES_DIR, never registered as a linked worktree of $REPO (so it is
# absent from `git worktree list --porcelain`) and absent from the registry
# (worktrees.json is always []). Clean + fully pushed -> Step 5's
# "all four conditions satisfied" removal branch. Matches
# tests/test_reaper_safety_gates.sh's _make_clean_pushed_repo.
_make_step5_clean() {
  local dir="$1"
  mkdir -p "$dir"
  git -C "$dir" init --quiet
  git -C "$dir" config user.email "test@test.com"
  git -C "$dir" config user.name "Test"
  git -C "$dir" remote add origin "$ORIGIN" 2>/dev/null || true
  echo "content" > "${dir}/file.txt"
  git -C "$dir" add file.txt
  git -C "$dir" commit --quiet -m "initial"
  git -C "$dir" push --quiet --force origin "HEAD:refs/heads/step5-$(basename "$dir")" 2>/dev/null || true
  git -C "$dir" fetch --quiet origin 2>/dev/null || true
}

# _make_step5_dirty <dir> -- same as above, then modifies the one tracked
# file WITHOUT adding any untracked (??) file, so the untracked-files guard
# (skipped-unsafe (has-untracked-files)) never fires and the fixture reaches
# Step 5's archive-then-prune branch instead.
_make_step5_dirty() {
  local dir="$1"
  _make_step5_clean "$dir"
  echo "dirty modification" >> "${dir}/file.txt"
}

source "$REGISTRY_LIB"

# _run_reaper <extra _cmd_reap args...> -- runs _cmd_reap in a clean
# subshell against the fixture repo. Captures combined stdout+stderr, since
# the skip-breakdown/candidate/cap-skip lines are stderr and the summary
# line is stdout.
_run_reaper() {
  _WTR_REPO_ROOT="$REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
  _WTR_AUDIT_DIR="$AUDIT_DIR" \
  _WTR_AUDIT_FILE="${AUDIT_DIR}/audit.jsonl" \
  WTR_TEST_MODE=1 \
  WTR_OPEN_PR_BRANCHES_OVERRIDE="${WTR_OPEN_PR_BRANCHES_OVERRIDE:-}" \
  WTR_OPEN_PR_HEAD_SHAS_OVERRIDE="${WTR_OPEN_PR_HEAD_SHAS_OVERRIDE:-}" \
  WORKTREE_REAP_MAX_PER_PASS="${WORKTREE_REAP_MAX_PER_PASS:-25}" \
    bash -c "
set -uo pipefail
source '${REGISTRY_LIB}'
_WTR_REPO_ROOT='${REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_WTR_AUDIT_DIR='${AUDIT_DIR}'
_WTR_AUDIT_FILE='${AUDIT_DIR}/audit.jsonl'
_cmd_reap --ttl-min 1 $*
" 2>&1
}

_reaped_count() {
  echo "$1" | grep '^worktrees:' | grep -oE '[0-9]+ reaped' | grep -oE '^[0-9]+'
}

_step5_cap_skip_names() {
  echo "$1" | grep -E '^  skipped-cap-reached: ' | sed -E 's/^  skipped-cap-reached: //' | sort
}

_step6_cap_skip_names() {
  echo "$1" | grep -E '^  skipped-cap-reached \(git-tracked\): ' \
    | sed -E 's/^  skipped-cap-reached \(git-tracked\): //' \
    | sed -E 's/ \(branch=.*\)$//' \
    | sort
}

# ===========================================================================
# Item 2: under the cap, Step 5 still removes everything, no cap-skip line.
# ===========================================================================
echo ""
echo "=== Item 2: under the cap, Step 5 removes everything (default cap 25) ==="

for i in 1 2 3; do _make_step5_clean "${WORKTREES_DIR}/item2-${i}"; done

OUT2=$(_run_reaper)
RC2=$?
_assert_exit0 "$RC2" "Item 2: reaper exits 0"
for i in 1 2 3; do
  _assert_dir_missing "${WORKTREES_DIR}/item2-${i}" "Item 2: item2-${i} removed"
done
_assert_eq "$(_reaped_count "$OUT2")" "3" "Item 2: reaped count is 3"
_assert_not_contains "$OUT2" "skipped-cap-reached" "Item 2: no cap-skip line under the default cap"

# ===========================================================================
# Item 3: at the cap, Step 5 stops and names the surviving dirs.
# ===========================================================================
echo ""
echo "=== Item 3: at the cap, Step 5 stops and reports the surviving dirs ==="

for i in 1 2 3 4 5; do _make_step5_clean "${WORKTREES_DIR}/item3-${i}"; done

OUT3=$(WORKTREE_REAP_MAX_PER_PASS=2 _run_reaper)
RC3=$?
_assert_exit0 "$RC3" "Item 3: reaper exits 0"
_assert_eq "$(_reaped_count "$OUT3")" "2" "Item 3: reaped count is exactly 2"

REMOVED3=0
SURVIVING3=()
for i in 1 2 3 4 5; do
  if [[ -d "${WORKTREES_DIR}/item3-${i}" ]]; then
    SURVIVING3+=("item3-${i}")
  else
    REMOVED3=$((REMOVED3 + 1))
  fi
done
_assert_eq "$REMOVED3" "2" "Item 3: exactly 2 dirs removed"
_assert_eq "${#SURVIVING3[@]}" "3" "Item 3: exactly 3 dirs survive"

CAP_SKIP_COUNT3=$(echo "$OUT3" | grep -cE '^  skipped-cap-reached: ')
_assert_eq "$CAP_SKIP_COUNT3" "3" "Item 3: exactly 3 cap-skip lines"

for name in "${SURVIVING3[@]}"; do
  _assert_contains "$OUT3" "skipped-cap-reached: ${name}" "Item 3: surviving dir ${name} named on a cap-skip line"
done

_assert_not_contains "$OUT3" "skipped-unsafe" "Item 3: cap-skip line distinct from skipped-unsafe"
_assert_not_contains "$OUT3" "path-guard-refused" "Item 3: cap-skip line distinct from path-guard-refused"
_assert_not_contains "$OUT3" "self-exclusion-refused" "Item 3: cap-skip line distinct from self-exclusion-refused"

for i in 1 2 3 4 5; do rm -rf "${WORKTREES_DIR}/item3-${i}"; done

# ===========================================================================
# Item 4: zero candidates is distinguishable from capped-at-zero.
# ===========================================================================
echo ""
echo "=== Item 4: zero Step-5 candidates reports as zero, not as capped ==="

for i in 1 2 3; do _add_wt "item4-${i}"; done

OUT4=$(WORKTREE_REAP_MAX_PER_PASS=2 WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper)
RC4=$?
_assert_exit0 "$RC4" "Item 4: reaper exits 0 (no opt-in)"
_assert_eq "$(_reaped_count "$OUT4")" "0" "Item 4: zero dirs removed"
_assert_not_contains "$OUT4" "skipped-cap-reached" "Item 4: zero cap-skip lines"
_assert_contains "$OUT4" "step5-candidates=0" "Item 4: summary states a Step-5 candidate count of 0"

for i in 1 2 3; do
  git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/item4-${i}" >/dev/null 2>&1 || true
done

# ===========================================================================
# Item 5: the archive-then-prune branch is capped too -- a capped candidate
# is neither archived nor removed (no patch file implying a removal that
# never happened).
# ===========================================================================
echo ""
echo "=== Item 5: archive-then-prune branch is capped -- no orphan patch for a capped dir ==="

for i in 1 2 3 4; do _make_step5_dirty "${WORKTREES_DIR}/item5-${i}"; done

OUT5=$(WORKTREE_REAP_MAX_PER_PASS=2 _run_reaper)
RC5=$?
_assert_exit0 "$RC5" "Item 5: reaper exits 0"

REMOVED5=0
SURVIVING5=()
for i in 1 2 3 4; do
  if [[ -d "${WORKTREES_DIR}/item5-${i}" ]]; then
    SURVIVING5+=("item5-${i}")
  else
    REMOVED5=$((REMOVED5 + 1))
  fi
done
_assert_eq "$REMOVED5" "2" "Item 5: exactly 2 dirs removed"
_assert_eq "${#SURVIVING5[@]}" "2" "Item 5: exactly 2 dirs survive"

PATCH_COUNT5=$(find "$ARCHIVE_DIR" -maxdepth 1 -name 'item5-*.patch' | wc -l | tr -d ' ')
_assert_eq "$PATCH_COUNT5" "2" "Item 5: exactly 2 patches archived"

for name in "${SURVIVING5[@]}"; do
  FOUND5=$(find "$ARCHIVE_DIR" -maxdepth 1 -name "${name}-*.patch" | wc -l | tr -d ' ')
  _assert_eq "$FOUND5" "0" "Item 5: capped survivor ${name} has no patch file"
done

for i in 1 2 3 4; do rm -rf "${WORKTREES_DIR}/item5-${i}"; done
find "$ARCHIVE_DIR" -maxdepth 1 -name 'item5-*.patch' -delete

# ===========================================================================
# Item 6: one shared budget across both steps -- Step 5 is charged first.
# ===========================================================================
echo ""
echo "=== Item 6: one budget across Step 5 and Step 6, Step 5 charged first ==="

_make_step5_clean "${WORKTREES_DIR}/item6-s5-1"
_make_step5_clean "${WORKTREES_DIR}/item6-s5-2"
_add_wt "item6-s6-1"
_add_wt "item6-s6-2"
_add_wt "item6-s6-3"

OUT6=$(WORKTREE_REAP_MAX_PER_PASS=3 WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --enable-git-tracked-removal)
RC6=$?
_assert_exit0 "$RC6" "Item 6: reaper exits 0"
_assert_eq "$(_reaped_count "$OUT6")" "3" "Item 6: exactly 3 total removals across both steps"

_assert_dir_missing "${WORKTREES_DIR}/item6-s5-1" "Item 6: Step-5 candidate 1 removed"
_assert_dir_missing "${WORKTREES_DIR}/item6-s5-2" "Item 6: Step-5 candidate 2 removed"

S6_REMOVED=0
for i in 1 2 3; do
  [[ -d "${WORKTREES_DIR}/item6-s6-${i}" ]] || S6_REMOVED=$((S6_REMOVED + 1))
done
_assert_eq "$S6_REMOVED" "1" "Item 6: exactly 1 of 3 Step-6 candidates removed -- Step 5's 2 were charged first"

for i in 1 2 3; do
  if [[ -d "${WORKTREES_DIR}/item6-s6-${i}" ]]; then
    git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/item6-s6-${i}" >/dev/null 2>&1 || true
  fi
done

# ===========================================================================
# Item 7: dry-run parity on the new path -- items 3 and 6's fixtures, run
# twice each (--dry-run first, then real, so the real run sees the
# unmutated population dry-run already observed), asserting the set of
# cap-skipped basenames is identical between the two.
# ===========================================================================
echo ""
echo "=== Item 7: dry-run and real agree on which candidates are cap-skipped ==="

for i in 1 2 3 4 5; do _make_step5_clean "${WORKTREES_DIR}/item7a-${i}"; done

OUT7A_DRY=$(WORKTREE_REAP_MAX_PER_PASS=2 _run_reaper --dry-run)
_assert_dir_exists "${WORKTREES_DIR}/item7a-1" "Item 7a: --dry-run mutated nothing (fixture 1 still present)"
OUT7A_REAL=$(WORKTREE_REAP_MAX_PER_PASS=2 _run_reaper)

NAMES7A_DRY=$(_step5_cap_skip_names "$OUT7A_DRY")
NAMES7A_REAL=$(_step5_cap_skip_names "$OUT7A_REAL")
_assert_eq "$NAMES7A_DRY" "$NAMES7A_REAL" "Item 7a: item-3-shape fixture — cap-skipped basenames identical, dry-run vs real"

for i in 1 2 3 4 5; do rm -rf "${WORKTREES_DIR}/item7a-${i}"; done

_make_step5_clean "${WORKTREES_DIR}/item7b-s5-1"
_make_step5_clean "${WORKTREES_DIR}/item7b-s5-2"
_add_wt "item7b-s6-1"
_add_wt "item7b-s6-2"
_add_wt "item7b-s6-3"

OUT7B_DRY=$(WORKTREE_REAP_MAX_PER_PASS=3 WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --dry-run --enable-git-tracked-removal)
_assert_dir_exists "${WORKTREES_DIR}/item7b-s5-1" "Item 7b: --dry-run mutated nothing (Step-5 fixture still present)"
_assert_dir_exists "${WORKTREES_DIR}/item7b-s6-1" "Item 7b: --dry-run mutated nothing (Step-6 fixture still present)"
OUT7B_REAL=$(WORKTREE_REAP_MAX_PER_PASS=3 WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --enable-git-tracked-removal)

NAMES7B_DRY=$( { _step5_cap_skip_names "$OUT7B_DRY"; _step6_cap_skip_names "$OUT7B_DRY"; } | sort)
NAMES7B_REAL=$( { _step5_cap_skip_names "$OUT7B_REAL"; _step6_cap_skip_names "$OUT7B_REAL"; } | sort)
_assert_eq "$NAMES7B_DRY" "$NAMES7B_REAL" "Item 7b: item-6-shape mixed fixture — cap-skipped basenames identical, dry-run vs real"

for i in 1 2; do rm -rf "${WORKTREES_DIR}/item7b-s5-${i}"; done
for i in 1 2 3; do
  if [[ -d "${WORKTREES_DIR}/item7b-s6-${i}" ]]; then
    git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/item7b-s6-${i}" >/dev/null 2>&1 || true
  fi
done

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "==========================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "==========================================="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
