#!/usr/bin/env bash
# test_reaper_enumeration_report.sh — D#2001 PR1 acceptance tests.
#
# Proves the reaper's `--dry-run` output tells the truth about what it saw,
# not only what it did (AC-1 through AC-5 of the frozen Spec on D#2001).
#
# Background: `worktree_registry reap`'s Step 5 back-compat path enumerates
# on-disk worktrees, finds they are all still tracked by
# `git worktree list --porcelain`, and discards every one of them on that
# single condition — silently. "0 reaped" was then indistinguishable from a
# genuinely clean tree. This PR adds a read-only enumeration + skip-reason
# report (Step 6) that runs alongside the unchanged removal logic and a loud
# `registry-empty` warning when the registry cannot explain what is on disk.
#
# No removal behaviour is exercised or asserted here — this file is
# counts-only, matching PR1's scope.
#
# Every fixture is a throwaway git repo under $TMPDIR — never this checkout.
#
# Exit code: 0 = all tests passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_REAL="$(cd "$SCRIPT_DIR/.." && pwd)"
REGISTRY_LIB="${REPO_ROOT_REAL}/scripts/lib/worktree-registry.sh"

# ---------------------------------------------------------------------------
# Minimal test framework (matches tests/test_worktree_self_exclusion.sh)
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

_assert_exit0()        { [[ "$1" -eq 0 ]] && _pass "$2" || _fail "$2 (exit=$1)"; }
_assert_contains()     { echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (missing: '$2' in output)"; }
_assert_not_contains() { ! echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (unexpected: '$2' in output)"; }

# ---------------------------------------------------------------------------
# Case 1 setup — one throwaway repo, one real bare "origin", five worktrees
# built so each lands in exactly one required skip bucket:
#   wt-self      -- the caller's own worktree
#   wt-young     -- created moments ago, younger than TTL
#   wt-dirty     -- uncommitted tracked change
#   wt-unpushed  -- a local commit not on origin
#   wt-clean     -- old, clean, fully pushed -- D#2001 PR2 made this a
#                   removal candidate under --enable-git-tracked-removal
#                   (was "still counted under skipped-git-tracked"
#                   pre-PR2; see AC-7). D#2149: a bare --dry-run (no
#                   opt-in, what this fixture exercises) now classifies it
#                   the same as a real run -- skipped-git-tracked, with an
#                   informational candidate-git-tracked line, not a
#                   would-remove or a `reaped` count. See
#                   test_reaper_git_tracked_removal.sh and
#                   test_reaper_dryrun_parity.sh for the opt-in preview
#                   behaviour.
# WTR_TEST_MODE=1 + WTR_OPEN_PR_BRANCHES_OVERRIDE="" below tells the open-PR
# guard "the list was obtained and it is empty" so this fixture never makes
# a real `gh` call and never depends on network/auth state. Both are
# required -- the override alone is deliberately not enough (D#2001 PR2
# fix-cycle 1): an operator accidentally exporting it empty in a real shell
# must not silently disable the guard.
# ---------------------------------------------------------------------------
TMPDIR_ROOT=$(mktemp -d /tmp/test-wtr-enum-report-XXXXXX)
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

mkdir -p "$REPO/.autonomous-team" "$REPO/archive/orphan-diffs"
echo '[]' > "$REPO/.autonomous-team/worktrees.json"

git -C "$REPO" worktree add -q --detach "$REPO/.claude/worktrees/wt-self" >/dev/null
git -C "$REPO" worktree add -q --detach "$REPO/.claude/worktrees/wt-young" >/dev/null
git -C "$REPO" worktree add -q --detach "$REPO/.claude/worktrees/wt-dirty" >/dev/null
git -C "$REPO" worktree add -q --detach "$REPO/.claude/worktrees/wt-unpushed" >/dev/null
git -C "$REPO" worktree add -q --detach "$REPO/.claude/worktrees/wt-clean" >/dev/null

WT_SELF="$REPO/.claude/worktrees/wt-self"
WT_DIRTY="$REPO/.claude/worktrees/wt-dirty"
WT_UNPUSHED="$REPO/.claude/worktrees/wt-unpushed"
WT_CLEAN="$REPO/.claude/worktrees/wt-clean"
# wt-young is deliberately left at its just-created mtime.

# Age everything except wt-young to well beyond a 1-minute TTL.
OLD_TS="202001010000"
touch -t "$OLD_TS" "$WT_SELF" "$WT_DIRTY" "$WT_UNPUSHED" "$WT_CLEAN"

# wt-dirty: uncommitted tracked modification (existing file -- doesn't touch
# the parent directory's mtime).
echo "changed" >> "$WT_DIRTY/README.md"

# wt-unpushed: local commit not on origin. Creating a new file bumps the
# parent directory's mtime back to "now" -- re-age after the commit so this
# lands in skipped-unpushed, not skipped-young.
echo "extra" > "$WT_UNPUSHED/extra.txt"
git -C "$WT_UNPUSHED" add extra.txt
git -C "$WT_UNPUSHED" commit --quiet -m "local only"
touch -t "$OLD_TS" "$WT_UNPUSHED"

# wt-clean: nothing further -- already matches origin/main, aged above.

export _WTR_REPO_ROOT="$REPO"
export WTR_TEST_MODE=1                   # required for the override below to take effect
export WTR_OPEN_PR_BRANCHES_OVERRIDE=""  # available, empty -- never calls real gh
# shellcheck source=scripts/lib/worktree-registry.sh
source "$REGISTRY_LIB"

cd "$WT_SELF"
OUT1=$(worktree_registry reap --ttl-min 1 --dry-run 2>&1); RC1=$?
cd "$REPO_ROOT_REAL" >/dev/null

echo "=== Case 1: enumeration + skip-breakdown against a 5-worktree fixture ==="

# AC-4: exit 0.
_assert_exit0 "$RC1" "reap --dry-run exits 0"

# AC-1: enumerated matches git worktree list --porcelain minus the main
# checkout, measured independently in this test (not relayed from the tool).
GIT_COUNT=$(git -C "$REPO" worktree list --porcelain | grep -c '^worktree ')
EXPECTED_ENUMERATED=$(( GIT_COUNT - 1 ))
_assert_contains "$OUT1" "enumerated=${EXPECTED_ENUMERATED}" "enumerated (${EXPECTED_ENUMERATED}) matches git worktree list minus main checkout"
_assert_not_contains "$OUT1" "enumerated=0" "enumerated is not 0 while worktree directories exist"

# AC-2: per-reason skip breakdown covers all five required buckets and each
# one landed the fixture's matching worktree in it.
_assert_contains "$OUT1" "skipped-self=1" "self worktree counted under skipped-self"
_assert_contains "$OUT1" "skipped-young=1" "freshly-created worktree counted under skipped-young"
_assert_contains "$OUT1" "skipped-dirty=1" "worktree with a tracked change counted under skipped-dirty"
_assert_contains "$OUT1" "skipped-unpushed=1" "worktree with a local-only commit counted under skipped-unpushed"
# D#2149: a bare --dry-run (no --enable-git-tracked-removal) classifies
# the old+clean+pushed worktree the same way a real run would --
# skipped-git-tracked, not a would-remove candidate. This is the parity
# fix's core behaviour change; see test_reaper_dryrun_parity.sh.
_assert_contains "$OUT1" "skipped-git-tracked=1" "old+clean+pushed worktree counted under skipped-git-tracked without the opt-in (D#2149)"
_assert_contains "$OUT1" "candidate-git-tracked (requires --enable-git-tracked-removal): wt-clean" "old+clean+pushed worktree reported on an informational candidate-git-tracked line"
_assert_not_contains "$OUT1" "would-remove (git-tracked): wt-clean" "old+clean+pushed worktree is NOT reported as a would-remove candidate without the opt-in"

# AC-2's "sum to enumerated minus removals": without the opt-in, nothing is
# a removal candidate, so the skip-breakdown sums to the full enumerated
# count and `reaped` stays 0.
_assert_contains "$OUT1" "sum=${EXPECTED_ENUMERATED}" "skip-breakdown sum equals enumerated -- no removal candidates without the opt-in"
_assert_contains "$OUT1" "0 reaped" "nothing is counted in the reaped total without the opt-in"

# AC-3: registry is an empty array while 5 directories exist -- must warn.
_assert_contains "$OUT1" "registry-empty" "registry-empty warning fires for an empty registry with dirs on disk"

echo ""

# ---------------------------------------------------------------------------
# Case 2 -- registry file missing entirely (not just an empty array).
# ---------------------------------------------------------------------------
echo "=== Case 2: registry file absent entirely ==="

TMPDIR_ROOT2=$(mktemp -d /tmp/test-wtr-enum-report-missing-XXXXXX)
REPO2="$TMPDIR_ROOT2/repo"
git init --quiet -b main "$REPO2"
git -C "$REPO2" config user.email "test@test.com"
git -C "$REPO2" config user.name "Test"
echo hello > "$REPO2/README.md"
git -C "$REPO2" add README.md
git -C "$REPO2" commit --quiet -m init
mkdir -p "$REPO2/archive/orphan-diffs"
# Deliberately no .autonomous-team/worktrees.json at all.
git -C "$REPO2" worktree add -q --detach "$REPO2/.claude/worktrees/wt-only" >/dev/null
touch -t "$OLD_TS" "$REPO2/.claude/worktrees/wt-only"

(
  export _WTR_REPO_ROOT="$REPO2"
  unset _WTR_SELF_ROOT
  # shellcheck source=scripts/lib/worktree-registry.sh
  source "$REGISTRY_LIB"
  cd "$REPO2"
  worktree_registry reap --ttl-min 1 --dry-run 2>&1
  echo "RC=$?"
) > "$TMPDIR_ROOT2/out.txt" 2>&1
OUT2=$(cat "$TMPDIR_ROOT2/out.txt")
RC2=$(echo "$OUT2" | grep -oE 'RC=[0-9]+' | tail -1 | cut -d= -f2)
rm -rf "$TMPDIR_ROOT2"

_assert_exit0 "${RC2:-1}" "reap --dry-run exits 0 with no registry file at all"
_assert_contains "$OUT2" "registry-empty" "registry-empty warning fires when the registry file is missing"
_assert_contains "$OUT2" "missing" "warning names the registry as missing, not just empty"

echo ""

# ---------------------------------------------------------------------------
# Case 3 -- negative check: a populated, valid registry must NOT trip the
# registry-empty warning (proves the warning is conditional, not unconditional).
# ---------------------------------------------------------------------------
echo "=== Case 3: populated registry -- no false-positive warning ==="

TMPDIR_ROOT3=$(mktemp -d /tmp/test-wtr-enum-report-populated-XXXXXX)
REPO3="$TMPDIR_ROOT3/repo"
git init --quiet -b main "$REPO3"
git -C "$REPO3" config user.email "test@test.com"
git -C "$REPO3" config user.name "Test"
echo hello > "$REPO3/README.md"
git -C "$REPO3" add README.md
git -C "$REPO3" commit --quiet -m init
mkdir -p "$REPO3/.autonomous-team" "$REPO3/archive/orphan-diffs"
git -C "$REPO3" worktree add -q --detach "$REPO3/.claude/worktrees/wt-reg" >/dev/null
touch -t "$OLD_TS" "$REPO3/.claude/worktrees/wt-reg"

python3 - "$REPO3/.autonomous-team/worktrees.json" <<'PYEOF'
import json, sys
path = sys.argv[1]
entries = [{
    "worktree_id": "wt-reg", "path": ".claude/worktrees/wt-reg", "agent_id": "a1",
    "role": "executor", "discussion": None, "pr": None, "base_branch": "main",
    "branch": None, "parent_pid": None, "created_at": "2020-01-01T00:00:00Z",
    "last_heartbeat": "2020-01-01T00:00:00Z", "status": "active",
}]
with open(path, "w") as f:
    json.dump(entries, f, indent=2)
PYEOF

(
  export _WTR_REPO_ROOT="$REPO3"
  unset _WTR_SELF_ROOT
  # shellcheck source=scripts/lib/worktree-registry.sh
  source "$REGISTRY_LIB"
  cd "$REPO3"
  worktree_registry reap --ttl-min 1 --dry-run 2>&1
  echo "RC=$?"
) > "$TMPDIR_ROOT3/out.txt" 2>&1
OUT3=$(cat "$TMPDIR_ROOT3/out.txt")
RC3=$(echo "$OUT3" | grep -oE 'RC=[0-9]+' | tail -1 | cut -d= -f2)
rm -rf "$TMPDIR_ROOT3"

_assert_exit0 "${RC3:-1}" "reap --dry-run exits 0 with a populated registry"
_assert_not_contains "$OUT3" "registry-empty" "no registry-empty warning when the registry is populated and valid"

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
