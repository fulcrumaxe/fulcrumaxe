#!/usr/bin/env bash
# test_reaper_git_tracked_removal.sh — D#2001 PR2 acceptance tests (AC-7..AC-15).
#
# PR1 (#2117) added Step 6 as a read-only enumeration + skip-reason report.
# PR2 makes it the git-tracked-worktree removal handler: a worktree that is
# git-tracked, old enough, clean, fully pushed, not self, and has no open PR
# on its branch is reported by `--dry-run` as a removal candidate and, with
# the explicit --enable-git-tracked-removal opt-in, actually removed via
# `git worktree remove --force`.
#
# That opt-in exists because reap-worktrees.sh is invoked LIVE (no
# --dry-run) after every agent completion (post-agent-hook.sh:533) --
# shipping git-tracked removal enabled there by default would silently
# start deleting real worktrees on the very next agent completion after
# this merges. --dry-run reporting is unconditional (read-only, no
# mutation risk); real removal requires the opt-in on top of a real run.
#
# WTR_TEST_MODE=1 + WTR_OPEN_PR_BRANCHES_OVERRIDE (set, even empty) makes
# the open-PR guard use a fixed list instead of a real `gh pr list` call --
# every test below sets both, so this file never depends on network or
# `gh` auth state. Both are required (D#2001 PR2 fix-cycle 1): the override
# alone is deliberately not enough to disable the guard, so that exporting
# it empty by accident in a real shell can't silently do the same thing.
#
# Every fixture is a throwaway git repo under $TMPDIR -- never this checkout.
#
# Exit code: 0 = all tests passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_REAL="$(cd "$SCRIPT_DIR/.." && pwd)"
REGISTRY_LIB="${REPO_ROOT_REAL}/scripts/lib/worktree-registry.sh"

# ---------------------------------------------------------------------------
# Minimal test framework (matches tests/test_reaper_safety_gates.sh)
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

_assert_exit0()        { [[ "$1" -eq 0 ]] && _pass "$2" || _fail "$2 (exit=$1)"; }
_assert_contains()     { echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (missing: '$2')"; }
_assert_not_contains() { ! echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (unexpected: '$2')"; }
_assert_dir_exists()   { [[ -d "$1" ]] && _pass "$2" || _fail "$2 (missing dir: $1)"; }
_assert_dir_missing()  { [[ ! -d "$1" ]] && _pass "$2" || _fail "$2 (should not exist: $1)"; }

TMPDIR_ROOT=$(mktemp -d /tmp/test-wtr-git-tracked-removal-XXXXXX)
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

# _add_wt <id> [extra branch-add args...] -- a real `git worktree add`
# checked out from the already-pushed main tip, aged past any TTL used
# below. A fresh branch cut from a pushed commit is "pushed" by
# construction (rev-list HEAD --not --remotes looks at commit reachability,
# not branch existence).
_add_wt() {
  local id="$1"
  git -C "$REPO" worktree add -q "${WORKTREES_DIR}/${id}" -b "branch-${id}" >/dev/null 2>&1
  touch -t "$OLD_TS" "${WORKTREES_DIR}/${id}"
}

# _add_detached_wt <id> -- D#2129: a real `git worktree add --detach`
# checked out from the already-pushed main tip, aged past any TTL used
# below. Same "pushed by construction" property as _add_wt (main tip is
# already on origin), but with an empty branch -- the class
# `_wtr_branch_has_open_pr` used to wave through unconditionally
# (worktree-registry.sh:296-298, pre-D#2129), and the only class
# `pr_tree_provision` (scripts/lib/pr-tree.sh:89) ever produces.
# <ref> defaults to the current main tip; pass an explicit commit-ish to
# detach at a different (also pushed) commit -- needed so two detached
# fixtures in the same test can carry different HEAD shas.
_add_detached_wt() {
  local id="$1" ref="${2:-}"
  if [[ -n "$ref" ]]; then
    git -C "$REPO" worktree add -q --detach "${WORKTREES_DIR}/${id}" "$ref" >/dev/null 2>&1
  else
    git -C "$REPO" worktree add -q --detach "${WORKTREES_DIR}/${id}" >/dev/null 2>&1
  fi
  touch -t "$OLD_TS" "${WORKTREES_DIR}/${id}"
}

source "$REGISTRY_LIB"

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

# ===========================================================================
# Test 1 (AC-7, AC-8): clean+old+pushed+no-open-PR git-tracked worktree is
# reported as a would-remove candidate by --dry-run, and --dry-run removes
# nothing.
# ===========================================================================
echo ""
echo "=== Test 1: clean git-tracked worktree is a --dry-run candidate; --dry-run removes nothing ==="

_add_wt "clean-t1"
CLEAN_T1_DIR="${WORKTREES_DIR}/clean-t1"

BEFORE_COUNT=$(find "$WORKTREES_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l)

# D#2149: the would-remove preview for a git-tracked candidate is only
# reached under the --enable-git-tracked-removal opt-in now -- a bare
# --dry-run classifies the same as a real run (see
# test_reaper_dryrun_parity.sh for that no-opt-in behaviour).
WTR_OPEN_PR_BRANCHES_OVERRIDE="" OUT1=$(_run_reaper --dry-run --enable-git-tracked-removal)
RC1=$?

_assert_exit0 "$RC1" "T1: --dry-run exits 0"
_assert_contains "$OUT1" "would-remove (git-tracked): clean-t1" "T1: clean-t1 reported as would-remove candidate (AC-7)"
_assert_dir_exists "$CLEAN_T1_DIR" "T1: --dry-run did not remove clean-t1 (AC-8)"

AFTER_COUNT=$(find "$WORKTREES_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l)
[[ "$BEFORE_COUNT" -eq "$AFTER_COUNT" ]] && \
  _pass "T1: directory count under .claude/worktrees/ unchanged by --dry-run (AC-8)" || \
  _fail "T1: directory count changed by --dry-run (before=$BEFORE_COUNT after=$AFTER_COUNT)"

# Clean up clean-t1 now that both AC-7 assertions are done -- it is
# eligible for real removal, and leaving it on disk would make every later
# real (non-dry-run) test in this file remove it too, throwing off audit-row
# and cap counts. The AC-7 mutation proof (end of file) creates its own
# fresh clean-t1-shaped fixture instead of relying on this one.
git -C "$REPO" worktree remove --force "$CLEAN_T1_DIR" >/dev/null 2>&1 || true

# ===========================================================================
# Test 2 (AC-9): dirty tracked change -- never removed, skipped-dirty.
# ===========================================================================
echo ""
echo "=== Test 2: dirty git-tracked worktree is skipped-dirty, never removed ==="

_add_wt "dirty-t2"
DIRTY_T2_DIR="${WORKTREES_DIR}/dirty-t2"
echo "dirty change" >> "${DIRTY_T2_DIR}/README.md"

OUT2=$(_run_reaper --enable-git-tracked-removal)
RC2=$?

_assert_exit0 "$RC2" "T2: real run exits 0"
_assert_dir_exists "$DIRTY_T2_DIR" "T2: dirty git-tracked worktree was NOT removed (AC-9)"
_assert_contains "$OUT2" "skipped-dirty=" "T2: skip-breakdown reports skipped-dirty"

# ===========================================================================
# Test 3 (AC-10): unpushed commit -- never removed, skipped-unpushed.
# ===========================================================================
echo ""
echo "=== Test 3: unpushed git-tracked worktree is skipped-unpushed, never removed ==="

_add_wt "unpushed-t3"
UNPUSHED_T3_DIR="${WORKTREES_DIR}/unpushed-t3"
echo "local only" > "${UNPUSHED_T3_DIR}/extra.txt"
git -C "$UNPUSHED_T3_DIR" add extra.txt
git -C "$UNPUSHED_T3_DIR" commit --quiet -m "unpushed commit"
touch -t "$OLD_TS" "$UNPUSHED_T3_DIR"  # re-age after the commit bumped mtime

OUT3=$(_run_reaper --enable-git-tracked-removal)
RC3=$?

_assert_exit0 "$RC3" "T3: real run exits 0"
_assert_dir_exists "$UNPUSHED_T3_DIR" "T3: unpushed git-tracked worktree was NOT removed (AC-10)"
_assert_contains "$OUT3" "skipped-unpushed=" "T3: skip-breakdown reports skipped-unpushed"

# ===========================================================================
# Test 4 (AC-11): branch backs an open PR (head or base ref) -- never
# removed, skipped-open-pr.
# ===========================================================================
echo ""
echo "=== Test 4: worktree whose branch backs an open PR is skipped-open-pr, never removed ==="

_add_wt "openpr-t4"
OPENPR_T4_DIR="${WORKTREES_DIR}/openpr-t4"

# D#2149: opt-in added so this dry-run reaches the open-PR guard at all
# (see the T1 note above).
OUT4=$(WTR_OPEN_PR_BRANCHES_OVERRIDE=$'branch-openpr-t4\nsome-other-branch' _run_reaper --dry-run --enable-git-tracked-removal)
RC4=$?

_assert_exit0 "$RC4" "T4: --dry-run exits 0"
_assert_contains "$OUT4" "skipped-open-pr" "T4: reported under skipped-open-pr (AC-11)"
_assert_not_contains "$OUT4" "would-remove (git-tracked): openpr-t4" "T4: not reported as a would-remove candidate"
_assert_dir_exists "$OPENPR_T4_DIR" "T4: worktree backing an open PR was NOT removed (AC-11)"

# Also prove it as a base ref (the D#2001 Spec's #1786-on-#1771 stacked-PR case).
OUT4B=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="branch-openpr-t4" _run_reaper --enable-git-tracked-removal)
RC4B=$?
_assert_exit0 "$RC4B" "T4b: real run with open-PR override exits 0"
_assert_dir_exists "$OPENPR_T4_DIR" "T4b: real run does not remove a worktree whose branch is protected"

# Clean up now that both open-PR assertions are done -- without an override
# in a later test, this worktree is otherwise eligible and would throw off
# that test's removal/audit counts.
git -C "$REPO" worktree remove --force "$OPENPR_T4_DIR" >/dev/null 2>&1 || true

# ===========================================================================
# Test 5 (opt-in gating): a real run WITHOUT --enable-git-tracked-removal
# never removes a git-tracked worktree, even one that is otherwise eligible
# -- this is the hot-path default (post-agent-hook.sh invokes exactly this).
# ===========================================================================
echo ""
echo "=== Test 5: real run without the opt-in flag never removes a git-tracked worktree ==="

_add_wt "noopt-t5"
NOOPT_T5_DIR="${WORKTREES_DIR}/noopt-t5"

OUT5=$(_run_reaper)  # real mode, no --dry-run, no --enable-git-tracked-removal
RC5=$?

_assert_exit0 "$RC5" "T5: real run (no opt-in) exits 0"
_assert_dir_exists "$NOOPT_T5_DIR" "T5: eligible worktree left alone without the opt-in flag"
_assert_not_contains "$OUT5" "removed (git-tracked): noopt-t5" "T5: no removal action logged for it"

# Clean up T5's fixture now that it is confirmed to have survived --
# leaving it in place would make it an eligible candidate in every test run.
git -C "$REPO" worktree remove --force "$NOOPT_T5_DIR" >/dev/null 2>&1 || true

# ===========================================================================
# Test 6 (AC-15): a real, opted-in removal appends exactly one audit row.
# ===========================================================================
echo ""
echo "=== Test 6: real opted-in removal writes one audit.jsonl row ==="

_add_wt "audit-t6"
AUDIT_T6_DIR="${WORKTREES_DIR}/audit-t6"

AUDIT_LINES_BEFORE=0
[[ -f "${AUDIT_DIR}/audit.jsonl" ]] && AUDIT_LINES_BEFORE=$(wc -l < "${AUDIT_DIR}/audit.jsonl")

OUT6=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --enable-git-tracked-removal)
RC6=$?

_assert_exit0 "$RC6" "T6: real opted-in run exits 0"
_assert_dir_missing "$AUDIT_T6_DIR" "T6: eligible worktree was actually removed"
_assert_contains "$OUT6" "removed (git-tracked): audit-t6" "T6: removal logged"

AUDIT_LINES_AFTER=$(wc -l < "${AUDIT_DIR}/audit.jsonl")
AUDIT_DELTA=$(( AUDIT_LINES_AFTER - AUDIT_LINES_BEFORE ))
[[ "$AUDIT_DELTA" -eq 1 ]] && \
  _pass "T6: audit.jsonl grew by exactly one row (AC-15)" || \
  _fail "T6: audit.jsonl grew by ${AUDIT_DELTA} rows, expected 1"
grep -q '"kind": "worktree_reap_git_tracked_removed"' "${AUDIT_DIR}/audit.jsonl" && \
  _pass "T6: audit row carries a worktree-removal kind" || \
  _fail "T6: no worktree_reap_git_tracked_removed row found"

# ===========================================================================
# Test 7 (AC-14): the removal path never writes to archive/orphan-diffs/.
# ===========================================================================
echo ""
echo "=== Test 7: removal path never grows archive/orphan-diffs/ ==="

ARCHIVE_COUNT_BEFORE=$(find "$ARCHIVE_DIR" -maxdepth 1 -type f | wc -l)

_add_wt "noarchive-t7"
NOARCHIVE_T7_DIR="${WORKTREES_DIR}/noarchive-t7"
OUT7=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --enable-git-tracked-removal)
RC7=$?

_assert_exit0 "$RC7" "T7: real opted-in run exits 0"
_assert_dir_missing "$NOARCHIVE_T7_DIR" "T7: eligible worktree was removed"

ARCHIVE_COUNT_AFTER=$(find "$ARCHIVE_DIR" -maxdepth 1 -type f | wc -l)
[[ "$ARCHIVE_COUNT_BEFORE" -eq "$ARCHIVE_COUNT_AFTER" ]] && \
  _pass "T7: archive/orphan-diffs/ file count unchanged (AC-14)" || \
  _fail "T7: archive/orphan-diffs/ grew (before=$ARCHIVE_COUNT_BEFORE after=$ARCHIVE_COUNT_AFTER)"

# ===========================================================================
# Test 8 (AC-13): per-pass cap. Three eligible worktrees, cap=2 -- exactly
# 2 removed, 1 left behind, cap-reached reported.
# ===========================================================================
echo ""
echo "=== Test 8: per-pass removal cap ==="

_add_wt "cap-a-t8"
_add_wt "cap-b-t8"
_add_wt "cap-c-t8"

OUT8=$(WORKTREE_REAP_MAX_PER_PASS=2 WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --enable-git-tracked-removal)
RC8=$?

_assert_exit0 "$RC8" "T8: capped real run exits 0"

REMOVED_COUNT=0
for id in cap-a-t8 cap-b-t8 cap-c-t8; do
  [[ -d "${WORKTREES_DIR}/${id}" ]] || REMOVED_COUNT=$((REMOVED_COUNT + 1))
done
[[ "$REMOVED_COUNT" -eq 2 ]] && \
  _pass "T8: exactly 2 of 3 eligible worktrees removed under cap=2 (AC-13)" || \
  _fail "T8: expected 2 removed, got ${REMOVED_COUNT}"
_assert_contains "$OUT8" "skipped-cap-reached" "T8: cap-reached is reported"

# Clean up whichever fixture(s) survived the cap so they don't leak into
# later tests / summary counts.
for id in cap-a-t8 cap-b-t8 cap-c-t8; do
  if [[ -d "${WORKTREES_DIR}/${id}" ]]; then
    git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/${id}" >/dev/null 2>&1 || true
  fi
done

# ===========================================================================
# Test 9 (security review fix-cycle 1, blocking finding 1): a corrupted
# .git/index makes `git status --porcelain` exit non-zero with EMPTY
# stdout (the error lands on stderr only). The classifier must not read
# that silence as "clean" -- it must route to a new "unknown" bucket that
# is skipped and reported, never removed. This is the exact construction
# both the security reviewer and the code reviewer independently
# reproduced against a disposable fixture and watched destroy content.
# ===========================================================================
echo ""
echo "=== Test 9: corrupted .git/index must not read as clean ==="

_add_wt "corrupt-idx-t9"
CORRUPT_T9_DIR="${WORKTREES_DIR}/corrupt-idx-t9"

# The content that must not be lost: an uncommitted tracked modification.
echo "precious uncommitted change" >> "${CORRUPT_T9_DIR}/README.md"

# Corrupt this worktree's OWN index -- a linked worktree's index lives
# under the main repo's .git/worktrees/<id>/index, not under the
# worktree's own .git (which is only a gitdir pointer file).
T9_GITDIR=$(git -C "$CORRUPT_T9_DIR" rev-parse --git-dir)
echo "not a valid git index" > "${T9_GITDIR}/index"

# Confirm the corruption actually reproduces what was measured: non-zero
# exit, EMPTY stdout, error on stderr only. If a git version ever changes
# this behavior, these assertions catch that before the real ones below
# would otherwise pass for the wrong reason.
T9_STDERR_FILE="${TMPDIR_ROOT}/t9-stderr-$$"
T9_STATUS_OUT=$(git -C "$CORRUPT_T9_DIR" status --porcelain 2>"$T9_STDERR_FILE")
T9_STATUS_RC=$?
T9_STATUS_ERR=$(cat "$T9_STDERR_FILE" 2>/dev/null)
rm -f "$T9_STDERR_FILE"
[[ "$T9_STATUS_RC" -ne 0 ]] && _pass "setup T9: corrupted index makes git status exit non-zero" || _fail "setup T9: git status did not fail (rc=$T9_STATUS_RC)"
[[ -z "$T9_STATUS_OUT" ]] && _pass "setup T9: git status stdout is empty despite the failure (the trap this closes)" || _fail "setup T9: git status produced stdout -- not the scenario under test"
[[ -n "$T9_STATUS_ERR" ]] && _pass "setup T9: git status wrote to stderr" || _fail "setup T9: no stderr produced -- unexpected"

OUT9=$(_run_reaper --dry-run)
RC9=$?
_assert_exit0 "$RC9" "T9: --dry-run exits 0 despite the corrupted worktree"
_assert_not_contains "$OUT9" "would-remove (git-tracked): corrupt-idx-t9" "T9: corrupted worktree is NOT reported as a would-remove candidate"
_assert_contains "$OUT9" "skipped-unknown" "T9: corrupted worktree is reported under skipped-unknown"

OUT9B=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --enable-git-tracked-removal)
RC9B=$?
_assert_exit0 "$RC9B" "T9b: real opted-in run exits 0"
_assert_dir_exists "$CORRUPT_T9_DIR" "T9b: corrupted worktree survives a real opted-in run -- content is not lost"

# Clean up directly (rm -rf, not git) -- the index is corrupted, so git
# operations against this worktree are not reliable for teardown either.
rm -rf "$CORRUPT_T9_DIR"
git -C "$REPO" worktree prune >/dev/null 2>&1 || true

# ===========================================================================
# Test 10 (security review fix-cycle 1, blocking finding 2): a git-tracked
# worktree living OUTSIDE .claude/worktrees/ must never be reported or
# removed, even if it otherwise clears every other condition. Only Step
# 5's rm -rf fallback had this guard; Step 6 never applied it.
# ===========================================================================
echo ""
echo "=== Test 10: worktree outside the worktrees dir is refused by the path guard ==="

OUTSIDE_T10_DIR="${TMPDIR_ROOT}/outside-worktrees-t10"
git -C "$REPO" worktree add -q "$OUTSIDE_T10_DIR" -b branch-outside-t10 >/dev/null 2>&1
touch -t "$OLD_TS" "$OUTSIDE_T10_DIR"

# D#2149: opt-in added so this dry-run reaches the path guard at all (see
# the T1 note above).
OUT10=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --dry-run --enable-git-tracked-removal)
RC10=$?
_assert_exit0 "$RC10" "T10: --dry-run exits 0"
_assert_not_contains "$OUT10" "would-remove (git-tracked): outside-worktrees-t10" "T10: out-of-tree worktree is NOT reported as a would-remove candidate"
_assert_contains "$OUT10" "path-guard-refused" "T10: out-of-tree worktree is refused by the path guard"

OUT10B=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --enable-git-tracked-removal)
RC10B=$?
_assert_exit0 "$RC10B" "T10b: real opted-in run exits 0"
_assert_dir_exists "$OUTSIDE_T10_DIR" "T10b: out-of-tree worktree survives a real opted-in run"

git -C "$REPO" worktree remove --force "$OUTSIDE_T10_DIR" >/dev/null 2>&1 || true

# ===========================================================================
# Test 11 (D#2129 Spec item 2): a DETACHED worktree whose HEAD commit is an
# open PR's head commit is skipped-open-pr, never removed -- this is the
# gap the pre-D#2129 branch-only guard left open (`_wtr_branch_has_open_pr`
# returned "not protected" for any empty branch, and every detached tree,
# including everything `pr_tree_provision` creates, has an empty branch).
# ===========================================================================
echo ""
echo "=== Test 11: detached worktree whose HEAD is an open PR's head commit is skipped-open-pr, never removed ==="

_add_detached_wt "openpr-t11"
OPENPR_T11_DIR="${WORKTREES_DIR}/openpr-t11"
OPENPR_T11_SHA=$(git -C "$OPENPR_T11_DIR" rev-parse HEAD)

# D#2149: opt-in added so this dry-run reaches the open-PR guard at all
# (see the T1 note above).
OUT11=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="" WTR_OPEN_PR_HEAD_SHAS_OVERRIDE="${OPENPR_T11_SHA}" _run_reaper --dry-run --enable-git-tracked-removal)
RC11=$?

_assert_exit0 "$RC11" "T11: --dry-run exits 0"
_assert_contains "$OUT11" "skipped-open-pr" "T11: reported under skipped-open-pr"
_assert_not_contains "$OUT11" "would-remove (git-tracked): openpr-t11" "T11: not reported as a would-remove candidate"
_assert_dir_exists "$OPENPR_T11_DIR" "T11: detached worktree backing an open PR was NOT removed by --dry-run"

# Real opted-in run must also leave it alone -- --dry-run agreeing is not
# enough; this is the path that actually calls `git worktree remove --force`.
OUT11B=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="" WTR_OPEN_PR_HEAD_SHAS_OVERRIDE="${OPENPR_T11_SHA}" _run_reaper --enable-git-tracked-removal)
RC11B=$?
_assert_exit0 "$RC11B" "T11b: real opted-in run exits 0"
_assert_dir_exists "$OPENPR_T11_DIR" "T11b: real opted-in run does not remove a detached worktree whose HEAD is protected"

# Negative control within the same test: a detached worktree whose HEAD is
# NOT in the open-PR SHA list is still reaped -- proves the SHA arm isn't
# just waving every detached tree through (that would be the blanket
# "never remove a detached worktree" the Spec explicitly forbids). Needs a
# HEAD sha that actually differs from OPENPR_T11_SHA, so cut a second
# pushed commit on a throwaway branch rather than reusing the main tip --
# every _add_wt/_add_detached_wt fixture without an explicit <ref> shares
# that one tip commit, which would make this negative control vacuous.
git -C "$REPO" checkout -q -b other-commit-t11
echo "second commit for T11" >> "$REPO/README.md"
git -C "$REPO" add README.md
git -C "$REPO" commit --quiet -m "t11 differentiation commit"
git -C "$REPO" push --quiet -u origin other-commit-t11
OTHER_T11_SHA=$(git -C "$REPO" rev-parse HEAD)
git -C "$REPO" checkout -q main

_add_detached_wt "nopr-t11" "$OTHER_T11_SHA"
NOPR_T11_DIR="${WORKTREES_DIR}/nopr-t11"

OUT11C=$(WTR_OPEN_PR_BRANCHES_OVERRIDE="" WTR_OPEN_PR_HEAD_SHAS_OVERRIDE="${OPENPR_T11_SHA}" _run_reaper --enable-git-tracked-removal)
RC11C=$?
_assert_exit0 "$RC11C" "T11c: real opted-in run exits 0"
_assert_dir_missing "$NOPR_T11_DIR" "T11c: detached worktree with no matching open-PR SHA is still reaped"
_assert_dir_exists "$OPENPR_T11_DIR" "T11c: detached worktree WITH a matching open-PR SHA is still protected in the same pass"

git -C "$REPO" worktree remove --force "$OPENPR_T11_DIR" >/dev/null 2>&1 || true

# ===========================================================================
# Mutation proofs (rule: every new test must be proven able to fail)
# ===========================================================================
echo ""
echo "=== Mutation proofs ==="

# --- AC-7 negative control: revert the candidate branch to an unconditional
#     skip (the Spec's own negative control for this Discussion) and prove
#     the would-remove assertion goes red. A fresh fixture is used since
#     the original clean-t1 was already removed above. --------------------
_add_wt "clean-t1"

MUTATED_LIB="${TMPDIR_ROOT}/worktree-registry.mutated.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB"

python3 - "$MUTATED_LIB" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()

marker = '        git-tracked)\n          _gt_branch="${_gt_branch_map[$_gt_path_i]:-}"\n'
replacement = '        git-tracked)\n          skip_git_tracked=$((skip_git_tracked + 1)); continue\n          _gt_branch="${_gt_branch_map[$_gt_path_i]:-}"\n'
assert marker in text, "AC-7 mutation anchor not found -- source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

MUT_OUT=$( \
  _WTR_REPO_ROOT="$REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
  _WTR_AUDIT_DIR="$AUDIT_DIR" \
  _WTR_AUDIT_FILE="${AUDIT_DIR}/audit.jsonl" \
  WTR_TEST_MODE=1 \
  WTR_OPEN_PR_BRANCHES_OVERRIDE="" \
    bash -c "
set -uo pipefail
source '${MUTATED_LIB}'
_WTR_REPO_ROOT='${REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_WTR_AUDIT_DIR='${AUDIT_DIR}'
_WTR_AUDIT_FILE='${AUDIT_DIR}/audit.jsonl'
_cmd_reap --ttl-min 1 --dry-run --enable-git-tracked-removal
" 2>&1)

if echo "$MUT_OUT" | grep -qF "would-remove (git-tracked): clean-t1"; then
  _fail "AC-7 mutation proof: clean-t1 STILL reported as would-remove after restoring the blanket skip (mutation had no effect)"
else
  _pass "AC-7 mutation proof: restoring the blanket git-tracked skip makes the would-remove assertion go red, as expected"
fi

rm -f "$MUTATED_LIB"

# --- AC-13 negative control: raise the cap above the eligible count and
#     confirm the cap-reached message disappears (proves the cap test
#     actually depends on the cap, not on something else). ----------------
_add_wt "cap-mut-a"
_add_wt "cap-mut-b"
MUT_CAP_OUT=$(WORKTREE_REAP_MAX_PER_PASS=99 WTR_OPEN_PR_BRANCHES_OVERRIDE="" _run_reaper --enable-git-tracked-removal)
if echo "$MUT_CAP_OUT" | grep -qF "skipped-cap-reached"; then
  _fail "AC-13 mutation proof: cap-reached still reported after raising the cap above the eligible count"
else
  _pass "AC-13 mutation proof: raising the cap above the eligible count removes all of them, no cap-reached message"
fi

# --- AC-9 negative control: delete the tracked-changes ("dirty") check. ---
_add_wt "dirty-mut"
DIRTY_MUT_DIR="${WORKTREES_DIR}/dirty-mut"
echo "dirty" >> "${DIRTY_MUT_DIR}/README.md"

MUTATED_LIB2="${TMPDIR_ROOT}/worktree-registry.mutated2.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB2"
python3 - "$MUTATED_LIB2" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()
marker = '''      local status
      status=$(echo "$status_out" | awk '{ code=substr($0,1,2); if (code ~ /[MADRCTU]/) print }')
      if [[ -n "$status" ]]; then
        echo "dirty" > "$outfile"
        return
      fi
'''
assert marker in text, "AC-9 mutation anchor not found -- source shape changed"
text = text.replace(marker, "", 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

MUT9_OUT=$( \
  _WTR_REPO_ROOT="$REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
  _WTR_AUDIT_DIR="$AUDIT_DIR" \
  _WTR_AUDIT_FILE="${AUDIT_DIR}/audit.jsonl" \
  WTR_TEST_MODE=1 \
  WTR_OPEN_PR_BRANCHES_OVERRIDE="" \
    bash -c "
set -uo pipefail
source '${MUTATED_LIB2}'
_WTR_REPO_ROOT='${REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_WTR_AUDIT_DIR='${AUDIT_DIR}'
_WTR_AUDIT_FILE='${AUDIT_DIR}/audit.jsonl'
_cmd_reap --ttl-min 1 --dry-run --enable-git-tracked-removal
" 2>&1)

if echo "$MUT9_OUT" | grep -qF "would-remove (git-tracked): dirty-mut"; then
  _pass "AC-9 mutation proof: deleting the tracked-changes check turns a dirty worktree into a would-remove candidate, as expected"
else
  _fail "AC-9 mutation proof: dirty-mut was NOT turned into a would-remove candidate -- mutation had no effect"
fi
rm -f "$MUTATED_LIB2"
git -C "$REPO" worktree remove --force "$DIRTY_MUT_DIR" >/dev/null 2>&1 || true

# --- AC-10 negative control: delete the unpushed-commit check. -----------
_add_wt "unpushed-mut"
UNPUSHED_MUT_DIR="${WORKTREES_DIR}/unpushed-mut"
echo "local" > "${UNPUSHED_MUT_DIR}/extra2.txt"
git -C "$UNPUSHED_MUT_DIR" add extra2.txt
git -C "$UNPUSHED_MUT_DIR" commit --quiet -m "unpushed"
touch -t "$OLD_TS" "$UNPUSHED_MUT_DIR"

MUTATED_LIB3="${TMPDIR_ROOT}/worktree-registry.mutated3.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB3"
python3 - "$MUTATED_LIB3" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()
marker = '''      local unpushed_out unpushed_rc
      local unpushed_err="${outfile}.unpushed.err"
      unpushed_out=$(git -C "$path" rev-list HEAD --not --remotes 2>"$unpushed_err")
      unpushed_rc=$?

      if [[ ! -r "$unpushed_err" ]]; then
        echo "unknown" > "$outfile"
        return
      fi
      local unpushed_stderr
      unpushed_stderr="$(<"$unpushed_err")"

      if [[ "$unpushed_rc" -ne 0 || -n "$unpushed_stderr" ]]; then
        echo "unknown" > "$outfile"
        return
      fi

      if [[ -n "$unpushed_out" ]]; then
        echo "unpushed" > "$outfile"
      else
        echo "git-tracked" > "$outfile"
      fi
'''
replacement = '      echo "git-tracked" > "$outfile"\n'
assert marker in text, "AC-10 mutation anchor not found -- source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

MUT10_OUT=$( \
  _WTR_REPO_ROOT="$REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
  _WTR_AUDIT_DIR="$AUDIT_DIR" \
  _WTR_AUDIT_FILE="${AUDIT_DIR}/audit.jsonl" \
  WTR_TEST_MODE=1 \
  WTR_OPEN_PR_BRANCHES_OVERRIDE="" \
    bash -c "
set -uo pipefail
source '${MUTATED_LIB3}'
_WTR_REPO_ROOT='${REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_WTR_AUDIT_DIR='${AUDIT_DIR}'
_WTR_AUDIT_FILE='${AUDIT_DIR}/audit.jsonl'
_cmd_reap --ttl-min 1 --dry-run --enable-git-tracked-removal
" 2>&1)

if echo "$MUT10_OUT" | grep -qF "would-remove (git-tracked): unpushed-mut"; then
  _pass "AC-10 mutation proof: deleting the unpushed-commit check turns an unpushed worktree into a would-remove candidate, as expected"
else
  _fail "AC-10 mutation proof: unpushed-mut was NOT turned into a would-remove candidate -- mutation had no effect"
fi
rm -f "$MUTATED_LIB3"
git -C "$REPO" worktree remove --force "$UNPUSHED_MUT_DIR" >/dev/null 2>&1 || true

# --- AC-11 negative control: delete the open-PR branch guard call. -------
_add_wt "openpr-mut"
OPENPR_MUT_DIR="${WORKTREES_DIR}/openpr-mut"

MUTATED_LIB4="${TMPDIR_ROOT}/worktree-registry.mutated4.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB4"
python3 - "$MUTATED_LIB4" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()
marker = '          if _wtr_worktree_has_open_pr "$_gt_branch" "$_gt_head_sha"; then\n'
replacement = '          if false; then\n'
assert marker in text, "AC-11 mutation anchor not found -- source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

MUT11_OUT=$( \
  _WTR_REPO_ROOT="$REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
  _WTR_AUDIT_DIR="$AUDIT_DIR" \
  _WTR_AUDIT_FILE="${AUDIT_DIR}/audit.jsonl" \
  WTR_TEST_MODE=1 \
  WTR_OPEN_PR_BRANCHES_OVERRIDE="branch-openpr-mut" \
    bash -c "
set -uo pipefail
source '${MUTATED_LIB4}'
_WTR_REPO_ROOT='${REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_WTR_AUDIT_DIR='${AUDIT_DIR}'
_WTR_AUDIT_FILE='${AUDIT_DIR}/audit.jsonl'
_cmd_reap --ttl-min 1 --dry-run --enable-git-tracked-removal
" 2>&1)

if echo "$MUT11_OUT" | grep -qF "would-remove (git-tracked): openpr-mut"; then
  _pass "AC-11 mutation proof: deleting the open-PR guard turns a PR-protected worktree into a would-remove candidate, as expected"
else
  _fail "AC-11 mutation proof: openpr-mut was NOT turned into a would-remove candidate -- mutation had no effect"
fi
rm -f "$MUTATED_LIB4"
git -C "$REPO" worktree remove --force "$OPENPR_MUT_DIR" >/dev/null 2>&1 || true

# --- Blocking finding 1 negative control: restore the "empty stdout reads
#     as clean" bug by neutralizing the rc/stderr check that Test 9 exists
#     to prove. Reconstructs the exact corrupted-index fixture and confirms
#     the mutation turns it into a would-remove candidate. -----------------
_add_wt "corrupt-idx-mut"
CORRUPT_MUT_DIR="${WORKTREES_DIR}/corrupt-idx-mut"
echo "precious uncommitted change" >> "${CORRUPT_MUT_DIR}/README.md"
CORRUPT_MUT_GITDIR=$(git -C "$CORRUPT_MUT_DIR" rev-parse --git-dir)
echo "not a valid git index" > "${CORRUPT_MUT_GITDIR}/index"

MUTATED_LIB5="${TMPDIR_ROOT}/worktree-registry.mutated5.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB5"
python3 - "$MUTATED_LIB5" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()
marker = '      if [[ "$status_rc" -ne 0 || -n "$status_stderr" ]]; then\n'
replacement = '      if false; then  # MUTATED: rc/stderr check disabled\n'
assert marker in text, "blocking-1 mutation anchor not found -- source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

MUT_B1_OUT=$( \
  _WTR_REPO_ROOT="$REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
  _WTR_AUDIT_DIR="$AUDIT_DIR" \
  _WTR_AUDIT_FILE="${AUDIT_DIR}/audit.jsonl" \
  WTR_TEST_MODE=1 \
  WTR_OPEN_PR_BRANCHES_OVERRIDE="" \
    bash -c "
set -uo pipefail
source '${MUTATED_LIB5}'
_WTR_REPO_ROOT='${REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_WTR_AUDIT_DIR='${AUDIT_DIR}'
_WTR_AUDIT_FILE='${AUDIT_DIR}/audit.jsonl'
_cmd_reap --ttl-min 1 --dry-run --enable-git-tracked-removal
" 2>&1)

if echo "$MUT_B1_OUT" | grep -qF "would-remove (git-tracked): corrupt-idx-mut"; then
  _pass "Blocking-1 mutation proof: disabling the rc/stderr check turns a corrupted, dirty worktree into a would-remove candidate, as expected"
else
  _fail "Blocking-1 mutation proof: corrupt-idx-mut was NOT turned into a would-remove candidate -- mutation had no effect"
fi
rm -f "$MUTATED_LIB5"
rm -rf "$CORRUPT_MUT_DIR"
git -C "$REPO" worktree prune >/dev/null 2>&1 || true

# --- Blocking finding 2 negative control: remove the worktrees-dir path
#     guard and confirm an out-of-tree worktree becomes a would-remove
#     candidate. --------------------------------------------------------
OUTSIDE_MUT_DIR="${TMPDIR_ROOT}/outside-worktrees-mut"
git -C "$REPO" worktree add -q "$OUTSIDE_MUT_DIR" -b branch-outside-mut >/dev/null 2>&1
touch -t "$OLD_TS" "$OUTSIDE_MUT_DIR"

MUTATED_LIB6="${TMPDIR_ROOT}/worktree-registry.mutated6.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB6"
python3 - "$MUTATED_LIB6" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()
marker = '          if [[ "$_gt_resolved_path" != "${resolved_worktrees_dir}/"* || "$_gt_resolved_path" == "$resolved_worktrees_dir" ]]; then\n'
replacement = '          if false; then  # MUTATED: path guard disabled\n'
assert marker in text, "blocking-2 mutation anchor not found -- source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

MUT_B2_OUT=$( \
  _WTR_REPO_ROOT="$REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
  _WTR_AUDIT_DIR="$AUDIT_DIR" \
  _WTR_AUDIT_FILE="${AUDIT_DIR}/audit.jsonl" \
  WTR_TEST_MODE=1 \
  WTR_OPEN_PR_BRANCHES_OVERRIDE="" \
    bash -c "
set -uo pipefail
source '${MUTATED_LIB6}'
_WTR_REPO_ROOT='${REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_WTR_AUDIT_DIR='${AUDIT_DIR}'
_WTR_AUDIT_FILE='${AUDIT_DIR}/audit.jsonl'
_cmd_reap --ttl-min 1 --dry-run --enable-git-tracked-removal
" 2>&1)

if echo "$MUT_B2_OUT" | grep -qF "would-remove (git-tracked): outside-worktrees-mut"; then
  _pass "Blocking-2 mutation proof: disabling the path guard turns an out-of-tree worktree into a would-remove candidate, as expected"
else
  _fail "Blocking-2 mutation proof: outside-worktrees-mut was NOT turned into a would-remove candidate -- mutation had no effect"
fi
rm -f "$MUTATED_LIB6"
git -C "$REPO" worktree remove --force "$OUTSIDE_MUT_DIR" >/dev/null 2>&1 || true

# --- D#2129 negative control: delete only the SHA arm the new detached-
#     HEAD guard adds, leaving the pre-existing branch arm untouched. This
#     is deliberately narrower than the AC-11 proof above (which deletes
#     the whole guard call and would pass whether or not the SHA arm ever
#     existed) -- it isolates the exact code this Spec adds. -------------
_add_detached_wt "openpr-mut-d2129"
OPENPR_MUT_D2129_DIR="${WORKTREES_DIR}/openpr-mut-d2129"
OPENPR_MUT_D2129_SHA=$(git -C "$OPENPR_MUT_D2129_DIR" rev-parse HEAD)

MUTATED_LIB7="${TMPDIR_ROOT}/worktree-registry.mutated7.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB7"
python3 - "$MUTATED_LIB7" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()
marker = '''  if [[ -n "$sha" && -n "$_WTR_OPEN_PR_HEAD_SHAS_CACHE" ]]; then
    local sline
    while IFS= read -r sline; do
      [[ "$sline" == "$sha" ]] && return 0
    done <<< "$_WTR_OPEN_PR_HEAD_SHAS_CACHE"
  fi
'''
replacement = "  # MUTATED (D#2129): SHA arm removed\n"
assert marker in text, "D#2129 mutation anchor not found -- source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

MUT_D2129_OUT=$( \
  _WTR_REPO_ROOT="$REPO" \
  _WTR_REGISTRY="${AUTONOMOUS_TEAM_DIR}/worktrees.json" \
  _WTR_LOCK="${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock" \
  _WTR_ARCHIVE_DIR="$ARCHIVE_DIR" \
  _WTR_WORKTREES_DIR="$WORKTREES_DIR" \
  _WTR_AUDIT_DIR="$AUDIT_DIR" \
  _WTR_AUDIT_FILE="${AUDIT_DIR}/audit.jsonl" \
  WTR_TEST_MODE=1 \
  WTR_OPEN_PR_BRANCHES_OVERRIDE="" \
  WTR_OPEN_PR_HEAD_SHAS_OVERRIDE="${OPENPR_MUT_D2129_SHA}" \
    bash -c "
set -uo pipefail
source '${MUTATED_LIB7}'
_WTR_REPO_ROOT='${REPO}'
_WTR_REGISTRY='${AUTONOMOUS_TEAM_DIR}/worktrees.json'
_WTR_LOCK='${AUTONOMOUS_TEAM_DIR}/worktrees.json.lock'
_WTR_ARCHIVE_DIR='${ARCHIVE_DIR}'
_WTR_WORKTREES_DIR='${WORKTREES_DIR}'
_WTR_AUDIT_DIR='${AUDIT_DIR}'
_WTR_AUDIT_FILE='${AUDIT_DIR}/audit.jsonl'
_cmd_reap --ttl-min 1 --dry-run --enable-git-tracked-removal
" 2>&1)

if echo "$MUT_D2129_OUT" | grep -qF "would-remove (git-tracked): openpr-mut-d2129"; then
  _pass "D#2129 mutation proof: deleting the detached-HEAD SHA guard turns a PR-protected detached worktree into a would-remove candidate, as expected"
else
  _fail "D#2129 mutation proof: openpr-mut-d2129 was NOT turned into a would-remove candidate -- mutation had no effect"
fi
rm -f "$MUTATED_LIB7"
git -C "$REPO" worktree remove --force "$OPENPR_MUT_D2129_DIR" >/dev/null 2>&1 || true

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
