#!/usr/bin/env bash
# test_reaper_dryrun_parity.sh — D#2149 acceptance tests (items 1, 2).
#
# `scripts/reap-worktrees.sh --dry-run` must be a faithful preview of the
# real run under the same flags. Before this fix, the git-tracked skip at
# worktree-registry.sh was gated on `dry_run == false`, so a bare --dry-run
# never evaluated it and fell through to "would-remove" instead -- and the
# per-pass removal cap (gt_removal_cap) was only checked in the real arm, so
# --dry-run --enable-git-tracked-removal promised unbounded removals while
# the real run capped at WORKTREE_REAP_MAX_PER_PASS.
#
# This file proves parity (item 1) and that both defects are mutation-proof
# (item 2). It does NOT cover the rm -rf path guard differential -- that is
# item 3, and a dry-run structurally cannot exercise it (see
# scripts/reap-worktrees.sh header and D#2149 for why).
#
# WTR_TEST_MODE=1 + WTR_OPEN_PR_BRANCHES_OVERRIDE (set, even empty) makes
# the open-PR guard use a fixed list instead of a real `gh pr list` call --
# every run below sets both, following test_reaper_git_tracked_removal.sh's
# harness, so this file never depends on network or `gh` auth state.
#
# Every fixture is a throwaway git repo under $TMPDIR -- never this checkout.
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

_assert_exit0()    { [[ "$1" -eq 0 ]] && _pass "$2" || _fail "$2 (exit=$1)"; }
_assert_contains() { echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (missing: '$2')"; }
_assert_not_contains() { ! echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (unexpected: '$2')"; }
_assert_eq()        { [[ "$1" == "$2" ]] && _pass "$3" || _fail "$3 (got '$1', expected '$2')"; }

TMPDIR_ROOT=$(mktemp -d /tmp/test-wtr-dryrun-parity-XXXXXX)
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

# _add_wt <id> -- a real `git worktree add` checked out from the
# already-pushed main tip, aged past any TTL used below. A fresh branch cut
# from a pushed commit is "pushed" by construction (rev-list HEAD --not
# --remotes looks at commit reachability, not branch existence). Matches
# test_reaper_git_tracked_removal.sh's fixture helper.
_add_wt() {
  local id="$1"
  git -C "$REPO" worktree add -q "${WORKTREES_DIR}/${id}" -b "branch-${id}" >/dev/null 2>&1
  touch -t "$OLD_TS" "${WORKTREES_DIR}/${id}"
}

source "$REGISTRY_LIB"

# _run_reap <extra _cmd_reap args...> -- runs _cmd_reap in a clean subshell
# against the fixture repo, capturing combined stdout+stderr (the
# skip-breakdown and candidate lines are stderr; the summary line is
# stdout -- a parity check on only one stream would compare against itself
# and pass vacuously, so both are captured here exactly like
# reap-worktrees.sh:122 merges them for its one real caller).
_run_reap() {
  local lib="$1"; shift
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
source '${lib}'
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

# _skip_breakdown <output> -- extracts just the skip-breakdown line, the
# thing acceptance item 1 requires be byte-identical between --dry-run and
# a real run.
_skip_breakdown() { echo "$1" | grep '^skip-breakdown:'; }

# _reaped_count <output> -- extracts the `reaped` count from the final
# summary line ("worktrees: N active, M reaped, ...").
_reaped_count() {
  echo "$1" | grep '^worktrees:' | grep -oE '[0-9]+ reaped' | grep -oE '^[0-9]+'
}

# ===========================================================================
# Test 1 (acceptance item 1, "Parity, no opt-in" + "Candidate line survives"):
# on an identical population with at least 2 git-tracked candidates,
# --dry-run and a real run (both without the opt-in) produce a
# byte-identical skip-breakdown line and the same reaped count -- and the
# candidate is still named on an informational line under --dry-run.
# ===========================================================================
echo ""
echo "=== Test 1: parity without the opt-in (byte-identical skip-breakdown, same reaped) ==="

_add_wt "parity-a"
_add_wt "parity-b"

WTR_OPEN_PR_BRANCHES_OVERRIDE="" OUT_DRY=$(_run_reap "$REGISTRY_LIB" --dry-run)
RC_DRY=$?
WTR_OPEN_PR_BRANCHES_OVERRIDE="" OUT_REAL=$(_run_reap "$REGISTRY_LIB")
RC_REAL=$?

_assert_exit0 "$RC_DRY" "T1: --dry-run exits 0"
_assert_exit0 "$RC_REAL" "T1: real run exits 0"

SB_DRY=$(_skip_breakdown "$OUT_DRY")
SB_REAL=$(_skip_breakdown "$OUT_REAL")
_assert_eq "$SB_DRY" "$SB_REAL" "T1: skip-breakdown line is byte-identical between --dry-run and the real run"
_assert_contains "$SB_DRY" "skipped-git-tracked=2" "T1: both parity-a and parity-b counted under skipped-git-tracked"

REAPED_DRY=$(_reaped_count "$OUT_DRY")
REAPED_REAL=$(_reaped_count "$OUT_REAL")
_assert_eq "${REAPED_DRY:-}" "${REAPED_REAL:-}" "T1: reaped count is identical between --dry-run and the real run"
_assert_eq "${REAPED_DRY:-}" "0" "T1: reaped count is 0 -- no opt-in, no removal candidates"

# "Candidate line survives" -- the preview is not lost even though it no
# longer touches `reaped`.
_assert_contains "$OUT_DRY" "candidate-git-tracked (requires --enable-git-tracked-removal): parity-a" "T1: parity-a named on a candidate-git-tracked line under --dry-run"
_assert_contains "$OUT_DRY" "candidate-git-tracked (requires --enable-git-tracked-removal): parity-b" "T1: parity-b named on a candidate-git-tracked line under --dry-run"
_assert_not_contains "$OUT_DRY" "would-remove (git-tracked)" "T1: --dry-run without the opt-in never says would-remove"

# Directory count unaffected by either run (neither has the opt-in).
DIR_COUNT_AFTER=$(find "$WORKTREES_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l)
[[ "$DIR_COUNT_AFTER" -eq 2 ]] && \
  _pass "T1: both fixtures survive -- no opt-in, no removal in either mode" || \
  _fail "T1: fixture count changed unexpectedly (got $DIR_COUNT_AFTER, expected 2)"

git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/parity-a" >/dev/null 2>&1 || true
git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/parity-b" >/dev/null 2>&1 || true

# ===========================================================================
# Test 2 (acceptance item 1, "Parity, with opt-in"): same fixture,
# --enable-git-tracked-removal on both runs, WORKTREE_REAP_MAX_PER_PASS set
# below the candidate count -- the dry-run's reaped equals the real run's
# reaped (the dry-run respects the cap). Runs --dry-run first (read-only,
# does not mutate the fixture) so the real run afterward sees the identical
# population in the identical order.
# ===========================================================================
echo ""
echo "=== Test 2: parity with the opt-in under a cap (dry-run respects WORKTREE_REAP_MAX_PER_PASS) ==="

_add_wt "cap-a"
_add_wt "cap-b"
_add_wt "cap-c"

WTR_OPEN_PR_BRANCHES_OVERRIDE="" OUT2_DRY=$(WORKTREE_REAP_MAX_PER_PASS=2 _run_reap "$REGISTRY_LIB" --dry-run --enable-git-tracked-removal)
RC2_DRY=$?
_assert_exit0 "$RC2_DRY" "T2: capped --dry-run --enable-git-tracked-removal exits 0"

DIR_COUNT_MID=$(find "$WORKTREES_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l)
[[ "$DIR_COUNT_MID" -eq 3 ]] && \
  _pass "T2: --dry-run removed nothing -- all 3 fixtures still on disk before the real run" || \
  _fail "T2: --dry-run mutated disk state (got $DIR_COUNT_MID dirs, expected 3)"

WTR_OPEN_PR_BRANCHES_OVERRIDE="" OUT2_REAL=$(WORKTREE_REAP_MAX_PER_PASS=2 _run_reap "$REGISTRY_LIB" --enable-git-tracked-removal)
RC2_REAL=$?
_assert_exit0 "$RC2_REAL" "T2: capped real run exits 0"

REAPED2_DRY=$(_reaped_count "$OUT2_DRY")
REAPED2_REAL=$(_reaped_count "$OUT2_REAL")
_assert_eq "${REAPED2_DRY:-}" "2" "T2: dry-run's reaped count respects the cap (2, not 3)"
_assert_eq "${REAPED2_DRY:-}" "${REAPED2_REAL:-}" "T2: dry-run's reaped equals the real run's reaped under the same cap"
_assert_contains "$OUT2_DRY" "skipped-cap-reached (git-tracked)" "T2: dry-run reports skipped-cap-reached for the candidate the cap excludes"
_assert_contains "$OUT2_REAL" "skipped-cap-reached (git-tracked)" "T2: real run reports skipped-cap-reached for the candidate the cap excludes"

REMOVED_COUNT=0
for id in cap-a cap-b cap-c; do
  [[ -d "${WORKTREES_DIR}/${id}" ]] || REMOVED_COUNT=$((REMOVED_COUNT + 1))
done
_assert_eq "$REMOVED_COUNT" "2" "T2: exactly 2 of 3 fixtures actually removed by the capped real run"

for id in cap-a cap-b cap-c; do
  if [[ -d "${WORKTREES_DIR}/${id}" ]]; then
    git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/${id}" >/dev/null 2>&1 || true
  fi
done

# ===========================================================================
# Mutation proof 1 (acceptance item 2): restoring the `dry_run == false`
# conjunct to the enable_git_tracked_removal skip condition reintroduces the
# original defect -- --dry-run and a real run diverge again on an identical
# population. A parity test that still passes against this mutation proves
# nothing, so this must go red.
# ===========================================================================
echo ""
echo "=== Mutation proof 1: restoring the dry_run==false conjunct breaks parity ==="

_add_wt "mut1-a"

MUTATED_LIB1="${TMPDIR_ROOT}/worktree-registry.mut1.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB1"
python3 - "$MUTATED_LIB1" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()
marker = '          if [[ "$enable_git_tracked_removal" == "false" ]]; then\n'
replacement = '          if [[ "$dry_run" == "false" && "$enable_git_tracked_removal" == "false" ]]; then\n'
assert text.count(marker) == 1, f"mutation-1 anchor not found exactly once (found {text.count(marker)}) -- source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

WTR_OPEN_PR_BRANCHES_OVERRIDE="" MUT1_DRY=$(_run_reap "$MUTATED_LIB1" --dry-run)
WTR_OPEN_PR_BRANCHES_OVERRIDE="" MUT1_REAL=$(_run_reap "$MUTATED_LIB1")

MUT1_SB_DRY=$(_skip_breakdown "$MUT1_DRY")
MUT1_SB_REAL=$(_skip_breakdown "$MUT1_REAL")
if [[ "$MUT1_SB_DRY" != "$MUT1_SB_REAL" ]]; then
  _pass "Mutation proof 1: restoring the dry_run==false conjunct makes the skip-breakdown parity assertion go red, as expected"
else
  _fail "Mutation proof 1: skip-breakdown STILL identical after restoring the dry_run==false conjunct (mutation had no effect)"
fi
if echo "$MUT1_DRY" | grep -qF "would-remove (git-tracked): mut1-a"; then
  _pass "Mutation proof 1: mutated --dry-run reports would-remove again (the original defect), confirming the mutation is faithful"
else
  _fail "Mutation proof 1: mutated --dry-run did not reproduce the original would-remove defect -- mutation anchor may be wrong"
fi

rm -f "$MUTATED_LIB1"
git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/mut1-a" >/dev/null 2>&1 || true

# ===========================================================================
# Mutation proof 2 (acceptance item 2): un-hoisting the cap check (gating it
# on dry_run=="false", i.e. moving it back below the would-remove branch so
# only the real arm enforces it) reintroduces the second defect -- a capped
# --dry-run --enable-git-tracked-removal promises unbounded removals again
# while the real run still stops at the cap.
# ===========================================================================
echo ""
echo "=== Mutation proof 2: un-hoisting the cap check breaks capped-preview parity ==="

_add_wt "mut2-a"
_add_wt "mut2-b"
_add_wt "mut2-c"

MUTATED_LIB2="${TMPDIR_ROOT}/worktree-registry.mut2.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB2"
python3 - "$MUTATED_LIB2" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()
marker = '          if [[ "$git_tracked_removed_this_pass" -ge "$gt_removal_cap" ]]; then\n'
replacement = '          if [[ "$dry_run" == "false" && "$git_tracked_removed_this_pass" -ge "$gt_removal_cap" ]]; then\n'
assert text.count(marker) == 1, f"mutation-2 anchor not found exactly once (found {text.count(marker)}) -- source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

WTR_OPEN_PR_BRANCHES_OVERRIDE="" MUT2_DRY=$(WORKTREE_REAP_MAX_PER_PASS=2 _run_reap "$MUTATED_LIB2" --dry-run --enable-git-tracked-removal)
MUT2_REAPED_DRY=$(_reaped_count "$MUT2_DRY")

if [[ "${MUT2_REAPED_DRY:-0}" -gt 2 ]]; then
  _pass "Mutation proof 2: un-hoisting the cap check makes the capped-preview assertion go red (dry-run promised ${MUT2_REAPED_DRY} > cap of 2), as expected"
else
  _fail "Mutation proof 2: mutated --dry-run still respected the cap (reaped=${MUT2_REAPED_DRY:-0}) -- mutation had no effect"
fi

rm -f "$MUTATED_LIB2"
for id in mut2-a mut2-b mut2-c; do
  git -C "$REPO" worktree remove --force "${WORKTREES_DIR}/${id}" >/dev/null 2>&1 || true
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
