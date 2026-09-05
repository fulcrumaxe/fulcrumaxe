#!/usr/bin/env bash
# test_reaper_clean_generated_wiki.sh — Gate 1 tests for the --clean-generated-wiki flag.
#
# Tests all five acceptance criteria:
#   AC-6 (happy path)     — only the 2 named wiki files modified → cleaned + reaped
#   AC-4 (other tracked)  — 2 wiki files + any OTHER modified tracked file → preserved
#   AC-3 (untracked)      — 2 wiki files + an untracked file → preserved
#   AC-5 (unpushed)       — 2 wiki files but unpushed commit → preserved
#   AC-2 (flag absent)    — flag OFF → no rescue, default behavior unchanged
#
# Also confirms the strict predicate: only "M "/" M" status codes allowed,
# no ??  entries, no third paths.
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
_assert_contains()      { echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (missing: '$2' in output)"; }
_assert_not_contains()  { ! echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (unexpected: '$2' in output)"; }
_assert_dir_exists()    { [[ -d "$1" ]] && _pass "$2" || _fail "$2 (missing dir: $1)"; }
_assert_dir_missing()   { [[ ! -d "$1" ]] && _pass "$2" || _fail "$2 (should not exist: $1)"; }

# ---------------------------------------------------------------------------
# Setup: shared temp directory, fake origin, and main repo
# ---------------------------------------------------------------------------
TMPDIR_ROOT=$(mktemp -d /tmp/test-clean-wiki-XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

FAKE_ORIGIN="${TMPDIR_ROOT}/origin.git"
MAIN_REPO="${TMPDIR_ROOT}/main-repo"

git init --bare --quiet "$FAKE_ORIGIN"
git clone --quiet "$FAKE_ORIGIN" "$MAIN_REPO" 2>/dev/null || true
cd "$MAIN_REPO"
git config user.email "test@test.com"
git config user.name "Test"
# Seed a README so the repo has a commit
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
# Helper: create a standalone git repo inside the worktrees dir, with a fake
# remote so rev-list HEAD --not --remotes is empty (fully pushed baseline).
# The repo includes the two wiki files as committed tracked content.
# ---------------------------------------------------------------------------
_make_wiki_repo() {
  local dir="$1"
  mkdir -p "$dir/wiki"
  git -C "$dir" init --quiet
  git -C "$dir" config user.email "test@test.com"
  git -C "$dir" config user.name "Test"
  git -C "$dir" remote add origin "$FAKE_ORIGIN" 2>/dev/null || true
  echo "# Status" > "${dir}/wiki/Project-Status.md"
  echo "# Drift" > "${dir}/wiki/Corpus-Drift-Report.md"
  echo "content" > "${dir}/file.txt"
  git -C "$dir" add .
  git -C "$dir" commit --quiet -m "initial"
  local branch="test-$(basename "$dir")"
  git -C "$dir" push --quiet --force origin "HEAD:refs/heads/${branch}" 2>/dev/null || true
  git -C "$dir" fetch --quiet origin 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Run the reaper against the test environment
# ---------------------------------------------------------------------------
_run_reaper() {
  local extra_args="${*:-}"
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
# AC-6 (happy path): only the 2 named wiki files modified → cleaned + reaped
# ---------------------------------------------------------------------------
echo ""
echo "=== AC-6 (happy path): only 2 wiki files modified → rescued + reaped ==="

HAPPY_ID="wiki-happy-t1-$$"
HAPPY_DIR="${WORKTREES_DIR}/${HAPPY_ID}"
_make_wiki_repo "$HAPPY_DIR"

# Dirty ONLY the two named wiki files (simulate spurious report writes)
echo "updated status" >> "${HAPPY_DIR}/wiki/Project-Status.md"
echo "updated drift" >> "${HAPPY_DIR}/wiki/Corpus-Drift-Report.md"

# Confirm setup
STATUS_HAPPY=$(git -C "$HAPPY_DIR" status --porcelain 2>/dev/null || true)
[[ -n "$STATUS_HAPPY" ]] && _pass "setup AC-6: dir is dirty" || _fail "setup AC-6: dir should be dirty"
UNPUSHED_HAPPY=$(git -C "$HAPPY_DIR" rev-list HEAD --not --remotes 2>/dev/null || true)
[[ -z "$UNPUSHED_HAPPY" ]] && _pass "setup AC-6: no unpushed commits" || _fail "setup AC-6: should be fully pushed"

OUT=$(_run_reaper --clean-generated-wiki)
RC=$?

_assert_exit0 $RC "AC-6: reaper exits 0"
_assert_contains "$OUT" "cleaned-generated-wiki: ${HAPPY_ID}" "AC-6: rescue logged"
_assert_dir_missing "$HAPPY_DIR" "AC-6: rescued dir was removed"

# ---------------------------------------------------------------------------
# AC-6 dry-run: flag + --dry-run reports intent without mutating
# ---------------------------------------------------------------------------
echo ""
echo "=== AC-6 dry-run: --clean-generated-wiki --dry-run reports without mutating ==="

DRYRUN_ID="wiki-dryrun-t2-$$"
DRYRUN_DIR="${WORKTREES_DIR}/${DRYRUN_ID}"
_make_wiki_repo "$DRYRUN_DIR"
echo "updated status" >> "${DRYRUN_DIR}/wiki/Project-Status.md"

ARCHIVE_BEFORE=$(find "${ARCHIVE_DIR}" -name "*.patch" 2>/dev/null | wc -l | tr -d ' ')
OUT=$(_run_reaper --clean-generated-wiki --dry-run)
RC=$?
ARCHIVE_AFTER=$(find "${ARCHIVE_DIR}" -name "*.patch" 2>/dev/null | wc -l | tr -d ' ')

_assert_exit0 $RC "AC-6 dry-run: exits 0"
_assert_contains "$OUT" "would-clean-generated-wiki: ${DRYRUN_ID}" "AC-6 dry-run: shows would-clean-generated-wiki"
_assert_dir_exists "$DRYRUN_DIR" "AC-6 dry-run: dir NOT removed"
[[ "${ARCHIVE_BEFORE}" -eq "${ARCHIVE_AFTER}" ]] && \
  _pass "AC-6 dry-run: no patch files created" || \
  _fail "AC-6 dry-run: patch files were created (mutations occurred)"

# Cleanup: remove the dry-run dir to keep subsequent tests clean
rm -rf "$DRYRUN_DIR"

# ---------------------------------------------------------------------------
# AC-4 (other tracked guard): 2 wiki files + another modified tracked file → preserved
# ---------------------------------------------------------------------------
echo ""
echo "=== AC-4: 2 wiki files + another tracked file → preserved (not rescued) ==="

OTHER_ID="wiki-other-t3-$$"
OTHER_DIR="${WORKTREES_DIR}/${OTHER_ID}"
_make_wiki_repo "$OTHER_DIR"

# Dirty the two wiki files AND the extra tracked file
echo "updated status" >> "${OTHER_DIR}/wiki/Project-Status.md"
echo "updated drift"  >> "${OTHER_DIR}/wiki/Corpus-Drift-Report.md"
echo "also dirty"     >> "${OTHER_DIR}/file.txt"  # third tracked file — must block rescue

STATUS_OTHER=$(git -C "$OTHER_DIR" status --porcelain 2>/dev/null || true)
[[ -n "$STATUS_OTHER" ]] && _pass "setup AC-4: dir is dirty" || _fail "setup AC-4: dir should be dirty"

OUT=$(_run_reaper --clean-generated-wiki)
RC=$?

_assert_exit0 $RC "AC-4: reaper exits 0"
_assert_not_contains "$OUT" "cleaned-generated-wiki: ${OTHER_ID}" "AC-4: rescue NOT applied"
_assert_contains "$OUT" "skipped-unsafe" "AC-4: dir logged as skipped-unsafe"
_assert_dir_exists "$OTHER_DIR" "AC-4: dir with third file was PRESERVED"

# ---------------------------------------------------------------------------
# AC-3 (untracked guard): 2 wiki files + an untracked file → preserved
# ---------------------------------------------------------------------------
echo ""
echo "=== AC-3: 2 wiki files + untracked file → preserved (not rescued) ==="

UNTRACKED_ID="wiki-untracked-t4-$$"
UNTRACKED_DIR="${WORKTREES_DIR}/${UNTRACKED_ID}"
_make_wiki_repo "$UNTRACKED_DIR"

# Dirty the two wiki files + leave an untracked file
echo "updated status" >> "${UNTRACKED_DIR}/wiki/Project-Status.md"
echo "scratch notes"  >  "${UNTRACKED_DIR}/scratch.txt"  # untracked — must block rescue

STATUS_UNTRACKED=$(git -C "$UNTRACKED_DIR" status --porcelain 2>/dev/null || true)
[[ "$STATUS_UNTRACKED" == *"??"* ]] && _pass "setup AC-3: has untracked file" || _fail "setup AC-3: no untracked file found in status"

OUT=$(_run_reaper --clean-generated-wiki)
RC=$?

_assert_exit0 $RC "AC-3: reaper exits 0"
_assert_not_contains "$OUT" "cleaned-generated-wiki: ${UNTRACKED_ID}" "AC-3: rescue NOT applied"
_assert_contains "$OUT" "skipped-unsafe" "AC-3: dir logged as skipped-unsafe"
_assert_dir_exists "$UNTRACKED_DIR" "AC-3: dir with untracked file was PRESERVED"

# ---------------------------------------------------------------------------
# AC-5 (unpushed guard): 2 wiki files but an unpushed commit → preserved
# ---------------------------------------------------------------------------
echo ""
echo "=== AC-5: 2 wiki files but unpushed commit → preserved (not rescued) ==="

UNPUSHED_ID="wiki-unpushed-t5-$$"
UNPUSHED_DIR="${WORKTREES_DIR}/${UNPUSHED_ID}"
_make_wiki_repo "$UNPUSHED_DIR"

# Commit something new WITHOUT pushing — creates an unpushed commit
echo "new real work" >> "${UNPUSHED_DIR}/file.txt"
git -C "$UNPUSHED_DIR" add file.txt
git -C "$UNPUSHED_DIR" commit --quiet -m "real work — do not discard"

# Now also dirty the two wiki files (so is_clean=false triggers rescue path)
echo "updated status" >> "${UNPUSHED_DIR}/wiki/Project-Status.md"
echo "updated drift"  >> "${UNPUSHED_DIR}/wiki/Corpus-Drift-Report.md"

UNPUSHED_COMMITS=$(git -C "$UNPUSHED_DIR" rev-list HEAD --not --remotes 2>/dev/null || true)
[[ -n "$UNPUSHED_COMMITS" ]] && _pass "setup AC-5: has unpushed commits" || _fail "setup AC-5: no unpushed commits detected"

OUT=$(_run_reaper --clean-generated-wiki)
RC=$?

_assert_exit0 $RC "AC-5: reaper exits 0"
_assert_not_contains "$OUT" "cleaned-generated-wiki: ${UNPUSHED_ID}" "AC-5: rescue NOT applied"
_assert_dir_exists "$UNPUSHED_DIR" "AC-5: dir with unpushed commit was PRESERVED"

# ---------------------------------------------------------------------------
# AC-2 (default unchanged): flag OFF → 2 wiki files dirty → preserved+skipped
# ---------------------------------------------------------------------------
echo ""
echo "=== AC-2: flag absent → wiki-dirty dir is preserved (default behavior) ==="

DEFAULT_ID="wiki-default-t6-$$"
DEFAULT_DIR="${WORKTREES_DIR}/${DEFAULT_ID}"
_make_wiki_repo "$DEFAULT_DIR"

# Dirty ONLY the two named wiki files
echo "updated status" >> "${DEFAULT_DIR}/wiki/Project-Status.md"
echo "updated drift"  >> "${DEFAULT_DIR}/wiki/Corpus-Drift-Report.md"

# Run WITHOUT --clean-generated-wiki
OUT=$(_run_reaper)
RC=$?

_assert_exit0 $RC "AC-2: reaper exits 0 without flag"
_assert_not_contains "$OUT" "cleaned-generated-wiki" "AC-2: no rescue log (flag absent)"
_assert_contains "$OUT" "skipped-unsafe" "AC-2: dir logged as skipped-unsafe"
_assert_dir_exists "$DEFAULT_DIR" "AC-2: wiki-dirty dir PRESERVED when flag absent"

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
