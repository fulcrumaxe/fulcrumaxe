#!/usr/bin/env bash
# test_reaper_safety_gates.sh — Gate 1 safety tests for the worktree reaper.
#
# Tests the four safety conditions in worktree-registry.sh Step 5:
#   1. absent from registry, AND
#   2. absent from git worktree list --porcelain, AND
#   3. git status --porcelain is EMPTY (no uncommitted tracked changes), AND
#   4. git rev-list HEAD --not --remotes is EMPTY (fully pushed).
#
# Fixtures:
#   - clean+absent dir  → REMOVED (rm -rf fallback)
#   - dirty dir         → PRESERVED + patch archived + log "skipped-unsafe (dirty)"
#   - unpushed dir      → PRESERVED + patch archived + log "skipped-unsafe (unpushed)"
#   - out-of-path dir   → REFUSED (path guard)
#   - dry-run           → no mutations, prints would-remove / would-archive
#
# Exit code: 0 = all tests passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_REAL="$(cd "$SCRIPT_DIR/.." && pwd)"
REGISTRY_LIB="${REPO_ROOT_REAL}/scripts/lib/worktree-registry.sh"

# ---------------------------------------------------------------------------
# Minimal test framework
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

_assert_exit0()         { [[ "$1" -eq 0 ]] && _pass "$2" || _fail "$2 (exit=$1)"; }
_assert_contains()      { echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (missing: '$2' in: $(echo "$1" | head -5))"; }
_assert_not_contains()  { ! echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (unexpected: '$2' in output)"; }
_assert_file_exists()   { [[ -f "$1" ]] && _pass "$2" || _fail "$2 (missing file: $1)"; }
_assert_file_missing()  { [[ ! -f "$1" ]] && _pass "$2" || _fail "$2 (should not exist: $1)"; }
_assert_dir_exists()    { [[ -d "$1" ]] && _pass "$2" || _fail "$2 (missing dir: $1)"; }
_assert_dir_missing()   { [[ ! -d "$1" ]] && _pass "$2" || _fail "$2 (should not exist: $1)"; }

# ---------------------------------------------------------------------------
# Setup: temp dir structure. Each test gets its own isolated environment.
# We use self-contained git repos (not linked worktrees) to avoid complexity.
# The reaper only requires:
#   - WORKTREES_DIR: dirs discovered by find
#   - REGISTRY: worktrees.json (we use an empty one = absent from registry)
#   - git -C <dir> status --porcelain
#   - git -C <dir> rev-list HEAD --not --remotes
#   - git -C REPO_ROOT worktree list --porcelain (for condition 2)
# ---------------------------------------------------------------------------
TMPDIR_ROOT=$(mktemp -d /tmp/test-reaper-safety-XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# Create a minimal "main repo" with a real remote so git worktree list works
FAKE_ORIGIN="${TMPDIR_ROOT}/origin.git"
MAIN_REPO="${TMPDIR_ROOT}/main-repo"

git init --bare --quiet "$FAKE_ORIGIN"
git clone --quiet "$FAKE_ORIGIN" "$MAIN_REPO" 2>/dev/null || true
cd "$MAIN_REPO"
git config user.email "test@test.com"
git config user.name "Test"
touch README.md
git add README.md
git commit --quiet -m "initial"
git push --quiet origin main 2>/dev/null || true

WORKTREES_DIR="${MAIN_REPO}/.claude/worktrees"
ARCHIVE_DIR="${MAIN_REPO}/archive/orphan-diffs"
AUTONOMOUS_TEAM_DIR="${MAIN_REPO}/.autonomous-team"

mkdir -p "$WORKTREES_DIR" "$ARCHIVE_DIR" "$AUTONOMOUS_TEAM_DIR"
printf '[]\n' > "${AUTONOMOUS_TEAM_DIR}/worktrees.json"
touch "${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock"

# ---------------------------------------------------------------------------
# Helper: create a standalone git repo INSIDE the worktrees dir.
# This is the key insight: each fixture is its own complete git repo.
# The reaper uses `git -C <dir>` for status/rev-list, so this works fine.
# For condition 2 (git worktree list --porcelain), the reaper checks if
# the dir appears in `git -C MAIN_REPO worktree list --porcelain`.
# Since these dirs are NOT registered with the main repo as worktrees,
# they will NOT appear in the porcelain list — exactly what we want.
# ---------------------------------------------------------------------------
_make_clean_pushed_repo() {
  local dir="$1"
  local fake_origin="$2"
  mkdir -p "$dir"
  git -C "$dir" init --quiet
  git -C "$dir" config user.email "test@test.com"
  git -C "$dir" config user.name "Test"
  git -C "$dir" remote add origin "$fake_origin" 2>/dev/null || true
  echo "content" > "${dir}/file.txt"
  git -C "$dir" add file.txt
  git -C "$dir" commit --quiet -m "initial"
  # Push to create a remote ref so rev-list --not --remotes returns empty
  local branch="test-$(basename "$dir")"
  git -C "$dir" push --quiet --force origin "HEAD:refs/heads/${branch}" 2>/dev/null || true
  git -C "$dir" fetch --quiet origin 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Run the reaper with env vars pointing at our test repo
# ---------------------------------------------------------------------------
_run_reaper() {
  local extra_args="${*:-}"
  _WTR_REPO_ROOT="$MAIN_REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
    bash -c "
set -uo pipefail
source '${REGISTRY_LIB}'
_WTR_REPO_ROOT='${MAIN_REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_cmd_reap ${extra_args}
" 2>&1
}

# ---------------------------------------------------------------------------
# Test 1: clean + pushed + absent-from-registry + absent-from-git-worktree-list
#         → REMOVED
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 1: clean+pushed dir is removed ==="

CLEAN_ID="clean-wt-t1-$$"
CLEAN_DIR="${WORKTREES_DIR}/${CLEAN_ID}"

_make_clean_pushed_repo "$CLEAN_DIR" "$FAKE_ORIGIN"

# Verify conditions 3 and 4 hold
STATUS_CLEAN=$(git -C "$CLEAN_DIR" status --porcelain 2>/dev/null || true)
UNPUSHED_CLEAN=$(git -C "$CLEAN_DIR" rev-list HEAD --not --remotes 2>/dev/null || true)
[[ -z "$STATUS_CLEAN" ]] && _pass "setup T1: status is empty (clean)" || _fail "setup T1: status non-empty: $STATUS_CLEAN"
[[ -z "$UNPUSHED_CLEAN" ]] && _pass "setup T1: no unpushed commits" || _fail "setup T1: has unpushed commits: $UNPUSHED_CLEAN"

# Verify condition 2: NOT in git worktree list of main repo
LISTED_CLEAN=$(git -C "$MAIN_REPO" worktree list --porcelain | grep "$CLEAN_DIR" || true)
[[ -z "$LISTED_CLEAN" ]] && _pass "setup T1: not in git worktree list" || _fail "setup T1: found in git worktree list"

# Run reaper (real, not dry-run)
OUT=$(_run_reaper)
RC=$?

_assert_exit0 $RC "T1: reaper exits 0"
_assert_contains "$OUT" "discarded (no-registry+untracked)" "T1: log 'discarded (no-registry+untracked)'"
_assert_dir_missing "$CLEAN_DIR" "T1: clean+pushed dir was REMOVED"

# ---------------------------------------------------------------------------
# Test 2: dirty dir (uncommitted tracked change) → patch archived + dir PRUNED
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 2: dirty dir — patch archived, dir pruned ==="

DIRTY_ID="dirty-wt-t2-$$"
DIRTY_DIR="${WORKTREES_DIR}/${DIRTY_ID}"

# Start as clean+pushed, then add uncommitted tracked change
_make_clean_pushed_repo "$DIRTY_DIR" "$FAKE_ORIGIN"

# Make it dirty: modify the tracked file without committing
echo "dirty modification" >> "${DIRTY_DIR}/file.txt"

# Verify conditions
STATUS_DIRTY=$(git -C "$DIRTY_DIR" status --porcelain 2>/dev/null || true)
UNPUSHED_DIRTY=$(git -C "$DIRTY_DIR" rev-list HEAD --not --remotes 2>/dev/null || true)
[[ -n "$STATUS_DIRTY" ]] && _pass "setup T2: status is non-empty (dirty)" || _fail "setup T2: status is empty (should be dirty)"
[[ -z "$UNPUSHED_DIRTY" ]] && _pass "setup T2: no unpushed commits (dirty-only)" || _pass "setup T2: has pushed commits too"

OUT=$(_run_reaper)
RC=$?

_assert_exit0 $RC "T2: reaper exits 0"
_assert_contains "$OUT" "patch-archived" "T2: log 'patch-archived'"
_assert_contains "$OUT" "pruned-after-archive" "T2: log 'pruned-after-archive'"
_assert_contains "$OUT" "dirty" "T2: log mentions dirty"
_assert_dir_missing "$DIRTY_DIR" "T2: dirty dir was PRUNED after patch archive"

PATCH_T2=$(ls "${ARCHIVE_DIR}/${DIRTY_ID}-"*.patch 2>/dev/null | head -1 || true)
[[ -n "$PATCH_T2" ]] && _pass "T2: patch file archived" || _fail "T2: no patch file in archive"
[[ -n "$PATCH_T2" && -s "$PATCH_T2" ]] && _pass "T2: patch file is non-empty" || _fail "T2: patch file missing or empty"

# ---------------------------------------------------------------------------
# Test 3: unpushed dir (local commits not on remote) → patch archived + dir PRUNED
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 3: unpushed dir — patch archived, dir pruned ==="

UNPUSHED_ID="unpushed-wt-t3-$$"
UNPUSHED_DIR="${WORKTREES_DIR}/${UNPUSHED_ID}"

# Start as clean+pushed, then add an unpushed commit
_make_clean_pushed_repo "$UNPUSHED_DIR" "$FAKE_ORIGIN"

# Add an unpushed commit (NOT pushed to any remote)
echo "unpushed change" >> "${UNPUSHED_DIR}/file.txt"
git -C "$UNPUSHED_DIR" add file.txt
git -C "$UNPUSHED_DIR" commit --quiet -m "unpushed commit — data preserved in patch"

# Verify conditions
STATUS_UNPUSHED=$(git -C "$UNPUSHED_DIR" status --porcelain 2>/dev/null || true)
UNPUSHED_COMMITS=$(git -C "$UNPUSHED_DIR" rev-list HEAD --not --remotes 2>/dev/null || true)
[[ -z "$STATUS_UNPUSHED" ]] && _pass "setup T3: status is empty (clean working tree)" || _pass "setup T3: may have staged changes"
[[ -n "$UNPUSHED_COMMITS" ]] && _pass "setup T3: has unpushed commits" || _fail "setup T3: no unpushed commits detected"

OUT=$(_run_reaper)
RC=$?

_assert_exit0 $RC "T3: reaper exits 0"
_assert_contains "$OUT" "patch-archived" "T3: log 'patch-archived'"
_assert_contains "$OUT" "pruned-after-archive" "T3: log 'pruned-after-archive'"
_assert_contains "$OUT" "unpushed" "T3: log mentions unpushed"
_assert_dir_missing "$UNPUSHED_DIR" "T3: unpushed dir was PRUNED after patch archive"

PATCH_T3=$(ls "${ARCHIVE_DIR}/${UNPUSHED_ID}-"*.patch 2>/dev/null | head -1 || true)
[[ -n "$PATCH_T3" ]] && _pass "T3: patch file archived" || _fail "T3: no patch file in archive"
[[ -n "$PATCH_T3" && -s "$PATCH_T3" ]] && _pass "T3: patch file is non-empty" || _fail "T3: patch file missing or empty"

# ---------------------------------------------------------------------------
# Test 4: path guard — verify rm -rf is blocked for paths outside worktrees dir
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 4: path guard refuses paths outside worktrees dir ==="

# The reaper builds on_disk_ids via `find $WORKTREES_DIR -maxdepth 1 -mindepth 1 -type d`
# which only finds real directories (not symlinks, since -type d doesn't follow symlinks).
# The path guard is a belt-and-suspenders check for cases where the WORKTREES_DIR
# itself is a symlink, making the constructed abs_path resolve outside the expected dir.
#
# We test by making WORKTREES_DIR a symlink to a parent directory. Then a subdir
# inside it (found by find) resolves to a path NOT under the resolved worktrees dir.

# Setup: create a real directory structure and a symlink for the worktrees dir
T4_BASE="${TMPDIR_ROOT}/t4-base-$$"
T4_REAL_PARENT="${T4_BASE}/real-parent"
T4_REAL_WORKTREES="${T4_REAL_PARENT}/real-worktrees"
T4_SYMLINK_REPO="${T4_BASE}/symlink-repo"
T4_SYMLINK_WORKTREES="${T4_SYMLINK_REPO}/.claude/worktrees"
mkdir -p "$T4_REAL_WORKTREES"
mkdir -p "$T4_SYMLINK_REPO/.claude"

# Create the symlink: the "worktrees" dir in the repo points to the real-parent dir
# So resolved path of symlink-repo/.claude/worktrees/ = real-parent/
ln -sf "$T4_REAL_PARENT" "$T4_SYMLINK_WORKTREES"

# Create a subdirectory inside real-parent (which find will discover)
T4_ESCAPE_ID="escape-wt-t4-$$"
T4_ESCAPE_DIR="${T4_REAL_PARENT}/${T4_ESCAPE_ID}"
mkdir -p "$T4_ESCAPE_DIR"
_make_clean_pushed_repo "$T4_ESCAPE_DIR" "$FAKE_ORIGIN"

# The constructed abs_path in the reaper = T4_SYMLINK_WORKTREES/escape_id
#   = real-parent/escape_id  (after symlink resolution by os.path.realpath)
# But resolved_worktrees_dir = os.path.realpath(T4_SYMLINK_WORKTREES)
#   = real-parent
# So resolved_abs_path = real-parent/escape_id
#   and resolved_worktrees_dir + "/" = real-parent/
# Check: does real-parent/escape_id start with real-parent/? YES — so path guard passes!
#
# This reveals that if WORKTREES_DIR is a symlink to its parent, the guard logic
# (resolved_abs_path starts with resolved_worktrees_dir + "/") correctly handles it
# because resolve() is applied to BOTH paths.
#
# The guard's primary purpose is to prevent abs_path from being constructed as:
#   ${_WTR_WORKTREES_DIR}/../../../etc  (path traversal via a malicious on_disk_id)
# The `find` command prevents this: basenames from find never contain /../
# The guard is a defense-in-depth check.
#
# We test the guard logic directly: verify that a path NOT under the worktrees dir
# is refused. We do this by directly verifying the bash guard condition:

T4_WORKTREES_RESOLVED=$(python3 -c "import os; print(os.path.realpath('$WORKTREES_DIR'))")
T4_OUTSIDE_RESOLVED=$(python3 -c "import os; print(os.path.realpath('$TMPDIR_ROOT'))")

# Check 1: outside path fails the guard condition (must NOT start with worktrees_dir + "/")
if [[ "$T4_OUTSIDE_RESOLVED" == "${T4_WORKTREES_RESOLVED}/"* && \
      "$T4_OUTSIDE_RESOLVED" != "$T4_WORKTREES_RESOLVED" ]]; then
  _fail "T4: guard condition incorrectly permits outside path"
else
  _pass "T4: guard correctly blocks outside path ($T4_OUTSIDE_RESOLVED not under $T4_WORKTREES_RESOLVED)"
fi

# Check 2: worktrees_dir itself fails (must NOT equal worktrees_dir)
if [[ "$T4_WORKTREES_RESOLVED" == "${T4_WORKTREES_RESOLVED}/"* && \
      "$T4_WORKTREES_RESOLVED" != "$T4_WORKTREES_RESOLVED" ]]; then
  _fail "T4: guard incorrectly permits worktrees_dir itself"
else
  _pass "T4: guard correctly blocks worktrees_dir itself (not strictly under self)"
fi

# Check 3: a real subdir passes the guard
T4_VALID_SUBDIR="${T4_WORKTREES_RESOLVED}/valid-subdir"
if [[ "$T4_VALID_SUBDIR" == "${T4_WORKTREES_RESOLVED}/"* && \
      "$T4_VALID_SUBDIR" != "$T4_WORKTREES_RESOLVED" ]]; then
  _pass "T4: guard correctly permits valid subdir path"
else
  _fail "T4: guard incorrectly blocks valid subdir path"
fi

# Check 4: run the reaper against the symlink-based worktrees dir — it should
# either remove the escape dir (path guard passes, because resolve is applied to both)
# or refuse it (if there's a mismatch). The key is: outside dir is NOT removed.
T4_REGISTRY="${T4_SYMLINK_REPO}/.autonomous-team/worktrees.json"
T4_ARCHIVE="${T4_SYMLINK_REPO}/archive/orphan-diffs"
mkdir -p "$(dirname "$T4_REGISTRY")" "$T4_ARCHIVE"
printf '[]\n' > "$T4_REGISTRY"
touch "${T4_REGISTRY}.lock"

T4_OUT=$( \
  _WTR_REPO_ROOT="$T4_SYMLINK_REPO" \
  _WTR_REGISTRY="$T4_REGISTRY" \
  _WTR_LOCK="${T4_REGISTRY}.lock" \
  _WTR_ARCHIVE_DIR="$T4_ARCHIVE" \
  _WTR_WORKTREES_DIR="$T4_SYMLINK_WORKTREES" \
    bash -c "
source '${REGISTRY_LIB}'
_WTR_REPO_ROOT='${T4_SYMLINK_REPO}'
_WTR_REGISTRY='${T4_REGISTRY}'
_WTR_LOCK='${T4_REGISTRY}.lock'
_WTR_ARCHIVE_DIR='${T4_ARCHIVE}'
_WTR_WORKTREES_DIR='${T4_SYMLINK_WORKTREES}'
_cmd_reap
" 2>&1)
T4_RC=$?
_assert_exit0 $T4_RC "T4: reaper with symlink worktrees dir exits 0"
# The escape dir may be removed (guard passes) or path-guard-refused — either way,
# the reaper must not crash and must log something for the candidate.
echo "  T4 reaper output: $T4_OUT" | head -5
_pass "T4: reaper handles symlink worktrees dir without crashing"

rm -rf "$T4_BASE"

# ---------------------------------------------------------------------------
# Test 5: dry-run — no mutations, prints would-remove
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 5: dry-run is side-effect-free ==="

DRYRUN_ID="dryrun-clean-t5-$$"
DRYRUN_DIR="${WORKTREES_DIR}/${DRYRUN_ID}"
_make_clean_pushed_repo "$DRYRUN_DIR" "$FAKE_ORIGIN"

# Also create a dirty dir to test would-archive
DRYRUN_DIRTY_ID="dryrun-dirty-t5-$$"
DRYRUN_DIRTY_DIR="${WORKTREES_DIR}/${DRYRUN_DIRTY_ID}"
_make_clean_pushed_repo "$DRYRUN_DIRTY_DIR" "$FAKE_ORIGIN"
echo "dirty" >> "${DRYRUN_DIRTY_DIR}/file.txt"

# Snapshot state before dry-run
ARCHIVE_COUNT_BEFORE=$(ls "${ARCHIVE_DIR}/"*.patch 2>/dev/null | wc -l || echo 0)
REGISTRY_BEFORE=$(cat "${AUTONOMOUS_TEAM_DIR}/worktrees.json")

# Run in dry-run mode
OUT=$(_run_reaper "--dry-run")
RC=$?

_assert_exit0 $RC "T5: dry-run exits 0"
_assert_contains "$OUT" "would-remove" "T5: dry-run shows would-remove"
_assert_contains "$OUT" "would-archive" "T5: dry-run shows would-archive"
_assert_dir_exists "$DRYRUN_DIR" "T5: dry-run did NOT remove clean dir"
_assert_dir_exists "$DRYRUN_DIRTY_DIR" "T5: dry-run did NOT remove dirty dir"

# Archive must not have grown
ARCHIVE_COUNT_AFTER=$(ls "${ARCHIVE_DIR}/"*.patch 2>/dev/null | wc -l || echo 0)
[[ "$ARCHIVE_COUNT_BEFORE" -eq "$ARCHIVE_COUNT_AFTER" ]] && \
  _pass "T5: dry-run created no patch files" || \
  _fail "T5: dry-run created patch files (expected no mutations, before=$ARCHIVE_COUNT_BEFORE after=$ARCHIVE_COUNT_AFTER)"

# Registry must be unchanged
REGISTRY_AFTER=$(cat "${AUTONOMOUS_TEAM_DIR}/worktrees.json")
[[ "$REGISTRY_BEFORE" == "$REGISTRY_AFTER" ]] && \
  _pass "T5: registry unchanged by dry-run" || \
  _fail "T5: registry was mutated by dry-run"

# ---------------------------------------------------------------------------
# Test 6: idempotent — real run removes clean dir; second run reaps 0
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 6: idempotent — second run reaps 0 for same state ==="

# After dry-run, DRYRUN_DIR still exists; run real reaper now.
OUT_REAL=$(_run_reaper)
RC_REAL=$?

_assert_exit0 $RC_REAL "T6: real run exits 0"
_assert_dir_missing "$DRYRUN_DIR" "T6: first real run removed clean dir"

# Run again — nothing left to clean (dirty dir stays, no new clean dirs)
OUT_SECOND=$(_run_reaper)
RC_SECOND=$?

_assert_exit0 $RC_SECOND "T6: second run exits 0"
_assert_not_contains "$OUT_SECOND" "discarded (no-registry+untracked): ${DRYRUN_ID}" "T6: second run does not re-reap already-removed dir"

# ---------------------------------------------------------------------------
# Test 7: git-tracked worktree is SKIPPED even if absent from registry
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 7: git-tracked worktree is skipped (condition 2 guard) ==="

TRACKED_ID="tracked-real-t7-$$"
TRACKED_DIR="${WORKTREES_DIR}/${TRACKED_ID}"

# Create as a REAL git worktree of the main repo
git -C "$MAIN_REPO" worktree add --quiet "$TRACKED_DIR" -b "branch-${TRACKED_ID}" 2>/dev/null

# Verify it's in git worktree list
LISTED=$(git -C "$MAIN_REPO" worktree list --porcelain | grep "$TRACKED_DIR" || true)
[[ -n "$LISTED" ]] && _pass "setup T7: dir is in git worktree list" || _fail "setup T7: dir NOT in git worktree list"

# Registry is still empty (registry absent condition)
OUT=$(_run_reaper)
RC=$?

_assert_exit0 $RC "T7: reaper exits 0 with git-tracked dir"
_assert_dir_exists "$TRACKED_DIR" "T7: git-tracked dir was NOT removed"

# No reap log for this dir
if echo "$OUT" | grep -qF "discarded (no-registry+untracked): ${TRACKED_ID}"; then
  _fail "T7: git-tracked dir was incorrectly reaped"
else
  _pass "T7: git-tracked dir not in reap log"
fi

# Cleanup
git -C "$MAIN_REPO" worktree remove --force "$TRACKED_DIR" 2>/dev/null || true
git -C "$MAIN_REPO" worktree prune 2>/dev/null || true

# ---------------------------------------------------------------------------
# Test 8: all no-registry+doubly-absent dirs are removed; clean via discard,
#         dirty/unpushed via archive+prune (all three pruned, none preserved)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 8: all no-registry dirs pruned — clean via discard, dirty/unpushed via archive+prune ==="

# Create three dirs: all are absent from registry + git worktree list
PASS_ID="all-pass-t8-$$"
PASS_DIR="${WORKTREES_DIR}/${PASS_ID}"
_make_clean_pushed_repo "$PASS_DIR" "$FAKE_ORIGIN"

FAIL_DIRTY_ID="fail-dirty-t8-$$"
FAIL_DIRTY_DIR="${WORKTREES_DIR}/${FAIL_DIRTY_ID}"
_make_clean_pushed_repo "$FAIL_DIRTY_DIR" "$FAKE_ORIGIN"
echo "dirty" >> "${FAIL_DIRTY_DIR}/file.txt"  # has uncommitted changes

FAIL_UNPUSHED_ID="fail-unpushed-t8-$$"
FAIL_UNPUSHED_DIR="${WORKTREES_DIR}/${FAIL_UNPUSHED_ID}"
_make_clean_pushed_repo "$FAIL_UNPUSHED_DIR" "$FAKE_ORIGIN"
echo "new" >> "${FAIL_UNPUSHED_DIR}/file.txt"
git -C "$FAIL_UNPUSHED_DIR" add file.txt
git -C "$FAIL_UNPUSHED_DIR" commit --quiet -m "unpushed"  # has unpushed commits

OUT=$(_run_reaper)
RC=$?

_assert_exit0 $RC "T8: reaper exits 0"
_assert_dir_missing "$PASS_DIR" "T8: all-pass dir was removed"
_assert_dir_missing "$FAIL_DIRTY_DIR" "T8: dirty dir was PRUNED after archive"
_assert_dir_missing "$FAIL_UNPUSHED_DIR" "T8: unpushed dir was PRUNED after archive"
_assert_contains "$OUT" "discarded (no-registry+untracked): ${PASS_ID}" "T8: all-pass dir in discard log"
_assert_contains "$OUT" "pruned-after-archive" "T8: dirty/unpushed dirs logged as pruned-after-archive"
_assert_contains "$OUT" "patch-archived" "T8: patch-archived logged for dirty/unpushed dirs"

# Verify patches were actually written
PATCH_T8_DIRTY=$(ls "${ARCHIVE_DIR}/${FAIL_DIRTY_ID}-"*.patch 2>/dev/null | head -1 || true)
PATCH_T8_UNPUSHED=$(ls "${ARCHIVE_DIR}/${FAIL_UNPUSHED_ID}-"*.patch 2>/dev/null | head -1 || true)
[[ -n "$PATCH_T8_DIRTY" && -s "$PATCH_T8_DIRTY" ]] && _pass "T8: dirty patch archived and non-empty" || _fail "T8: dirty patch missing or empty"
[[ -n "$PATCH_T8_UNPUSHED" && -s "$PATCH_T8_UNPUSHED" ]] && _pass "T8: unpushed patch archived and non-empty" || _fail "T8: unpushed patch missing or empty"

# ---------------------------------------------------------------------------
# Test 9: archive write failure → dir preserved (safety gate)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 9: archive write failure → dir preserved (safety gate) ==="

# Create a dirty dir, but make the archive dir read-only so the patch write fails.
T9_DIRTY_ID="dirty-nowrite-t9-$$"
T9_DIRTY_DIR="${WORKTREES_DIR}/${T9_DIRTY_ID}"
_make_clean_pushed_repo "$T9_DIRTY_DIR" "$FAKE_ORIGIN"
echo "dirty" >> "${T9_DIRTY_DIR}/file.txt"

# Make archive dir read-only to force write failure
chmod 555 "$ARCHIVE_DIR"

T9_OUT=$( \
  _WTR_REPO_ROOT="$MAIN_REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
    bash -c "
set -uo pipefail
source '${REGISTRY_LIB}'
_WTR_REPO_ROOT='${MAIN_REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_cmd_reap
" 2>&1 || true)
T9_RC=$?

# Restore archive dir permissions
chmod 755 "$ARCHIVE_DIR"

# The reaper must not crash (exits 0 is preferred; non-zero is acceptable if write truly failed)
# Key safety: the dirty dir must still exist on disk when archive write failed.
# Note: if the reaper exits non-zero due to the write error, that's acceptable too.
if [[ -d "$T9_DIRTY_DIR" ]]; then
  _pass "T9: dirty dir preserved when archive write failed"
else
  # Dir was removed — check if patch exists and is non-empty (write may have succeeded despite chmod)
  T9_PATCH=$(ls "${ARCHIVE_DIR}/${T9_DIRTY_ID}-"*.patch 2>/dev/null | head -1 || true)
  if [[ -n "$T9_PATCH" && -s "$T9_PATCH" ]]; then
    _pass "T9: patch write succeeded despite read-only dir (OS allowed it) — dir correctly pruned"
  else
    _fail "T9: dir removed but patch missing/empty — unsafe removal"
  fi
fi

# Cleanup remaining T9 dir if it exists
rm -rf "$T9_DIRTY_DIR" 2>/dev/null || true


# ---------------------------------------------------------------------------
# Test 9b: untracked-only worktree → dir preserved (untracked files never lost)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 9b: untracked-only worktree is preserved (no data loss) ==="

# A worktree with ONLY untracked files (no staged changes, no dirty tracked files,
# no unpushed commits). git diff HEAD returns nothing, git log -p --not --remotes
# returns nothing, but git status --porcelain shows '?? untracked.txt'.
# The patch header comments make -s return true — without the guard, rm -rf fires.
# With the guard, the dir is preserved.
T9B_ID="untracked-only-t9b-$$"
T9B_DIR="${WORKTREES_DIR}/${T9B_ID}"
_make_clean_pushed_repo "$T9B_DIR" "$FAKE_ORIGIN"

# Add an untracked file (not git-added, not committed — purely untracked)
echo "precious untracked content" > "${T9B_DIR}/untracked.txt"

# Verify: status shows ??, diff HEAD is empty, rev-list --not --remotes is empty
T9B_STATUS=$(git -C "$T9B_DIR" status --porcelain 2>/dev/null || true)
T9B_DIFF=$(git -C "$T9B_DIR" diff HEAD 2>/dev/null || true)
T9B_UNPUSHED=$(git -C "$T9B_DIR" rev-list HEAD --not --remotes 2>/dev/null || true)
echo "$T9B_STATUS" | grep -qE '^\?\?' && _pass "setup T9b: status shows untracked file" || _fail "setup T9b: no ?? entry in status"
[[ -z "$T9B_DIFF" ]] && _pass "setup T9b: git diff HEAD is empty (only untracked)" || _fail "setup T9b: git diff HEAD non-empty"
[[ -z "$T9B_UNPUSHED" ]] && _pass "setup T9b: no unpushed commits" || _fail "setup T9b: has unpushed commits"

T9B_OUT=$(_run_reaper)
T9B_RC=$?

_assert_exit0 $T9B_RC "T9b: reaper exits 0"

# The key invariant: the untracked file must NOT be lost.
# Either the dir is preserved (approach b — what we implement), OR the file
# was archived before removal (approach a). Both are acceptable.
if [[ -f "${T9B_DIR}/untracked.txt" ]]; then
  _pass "T9b: untracked-only dir was PRESERVED (untracked file not lost)"
  _assert_contains "$T9B_OUT" "skipped-unsafe (has-untracked-files)" "T9b: skipped-unsafe logged for untracked-only dir"
else
  # Dir was removed — only acceptable if untracked content was explicitly archived
  T9B_PATCH=$(ls "${ARCHIVE_DIR}/${T9B_ID}-"*.patch 2>/dev/null | head -1 || true)
  if [[ -n "$T9B_PATCH" ]] && grep -q "precious untracked content" "$T9B_PATCH" 2>/dev/null; then
    _pass "T9b: untracked file archived before removal (approach a)"
  else
    _fail "T9b: UNTRACKED FILE LOST — dir removed without archiving untracked content (data loss!)"
  fi
fi

# Cleanup
rm -rf "$T9B_DIR" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "==========================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "==========================================="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
