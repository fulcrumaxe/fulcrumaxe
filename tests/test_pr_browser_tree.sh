#!/usr/bin/env bash
# tests/test_pr_browser_tree.sh — unit tests for scripts/lib/pr-browser-tree.sh (D#2549)
#
# Run: bash tests/test_pr_browser_tree.sh   (expects exit 0)
#
# Same convention as tests/test_pr_tree_provisioning.sh: everything runs
# against a small synthetic ORIGIN (bare, stands in for the code-plane
# GitHub remote) + PARENT (a working clone, stands in for the real checkout
# scripts/pr-browser-preview.sh runs from) pair built in a temp dir, so this
# finishes in seconds and never touches the real repo, the real code plane,
# or the network.
#
# This is the test that proves the whole point of D#2549: a PR's dashboard
# tree, materialized by this library, must contain the PR head's content and
# must NOT silently fall back to main's content — which is exactly the
# defect that made every prior browser-test screenshot a screenshot of main.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/pr-browser-tree.sh"
PASS=0
FAIL=0

ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; shift; [ $# -gt 0 ] && echo "        $*"; FAIL=$((FAIL + 1)); }
assert_rc() {
  if [ "$3" -eq "$2" ]; then ok "$1 (exit $3)"; else bad "$1" "expected exit $2, got $3"; fi
}
assert_nonzero() {
  if [ "$2" -ne 0 ]; then ok "$1 (exit $2)"; else bad "$1" "expected non-zero exit, got 0"; fi
}
assert_ok() { local l="$1"; shift; if "$@"; then ok "$l"; else bad "$l" "expected true: $*"; fi; }
assert_not() { local l="$1"; shift; if "$@"; then bad "$l" "expected false: $*"; else ok "$l"; fi; }
assert_eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi
}

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# state-dir isolation (repo convention): verify_tree_build reads state_paths.py
# for its manifest dir — point it at scratch so this never touches the real
# state dir. See CLAUDE.md "AUTONOMOUS_TEAM_STATE_DIR in tests".
export AUTONOMOUS_TEAM_STATE_DIR="$WORK/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"

ORIGIN="$WORK/origin.git"
PARENT="$WORK/parent"

git init --quiet --bare "$ORIGIN"

git clone --quiet "$ORIGIN" "$PARENT"
(
  cd "$PARENT" || exit 1
  git config user.email "test@example.invalid"
  git config user.name "pr-browser-tree test"
  mkdir -p dashboard/src
  echo "MAIN_ONLY_MARKER — this string lives on main, never on a PR head" > dashboard/src/marker.txt
  git add -A && git commit --quiet -m "main state"
  git push --quiet origin HEAD:refs/heads/main
) || { echo "FATAL: could not build fixture main history"; exit 1; }

# Simulate an open PR on the code plane: a second clone builds the PR
# commit, and refs/pull/<N>/head is pointed at it directly on ORIGIN —
# exactly what GitHub does automatically for any open PR.
PR_WORK="$WORK/pr-author-clone"
git clone --quiet "$ORIGIN" "$PR_WORK"
(
  cd "$PR_WORK" || exit 1
  git config user.email "pr-author@example.invalid"
  git config user.name "pr author"
  # The bare ORIGIN's own HEAD symref may still point at a "master" that was
  # never pushed (git-init's traditional default), so a plain clone can land
  # on an empty working tree even though refs/heads/main is real — check out
  # main explicitly rather than relying on clone's default-branch guess.
  git checkout --quiet main
  echo "PR_HEAD_ONLY_MARKER — this string exists only on the PR head" > dashboard/src/marker.txt
  git add -A && git commit --quiet -m "pr change"
  git push --quiet origin HEAD:refs/heads/pr-branch
) || { echo "FATAL: could not build fixture PR commit"; exit 1; }

PR_SHA="$(git -C "$PR_WORK" rev-parse HEAD)"
PR_NUMBER=2549
git -C "$ORIGIN" update-ref "refs/pull/${PR_NUMBER}/head" "$PR_SHA"

# ORIGIN plays the role of the code-plane remote; add it under a
# deliberately non-"origin" name so a test that accidentally hardcoded
# "origin" (the Discussion plane, in the real checkout) would fail loudly.
git -C "$PARENT" remote add code-plane-test "$ORIGIN"

# shellcheck source=scripts/lib/pr-browser-tree.sh
source "$LIB"

echo "=== pbt_fetch_head — resolves the PR head sha from the named remote (never 'origin') ==="
GOT_SHA="$(pbt_fetch_head "$PR_NUMBER" "code-plane-test" "$PARENT" 2>err.log)"
RC=$?
ERR="$(cat err.log 2>/dev/null)"; rm -f err.log
assert_rc "pbt_fetch_head exits 0" 0 "$RC"
assert_eq "pbt_fetch_head prints the PR head sha, not main's" "$PR_SHA" "$GOT_SHA"
assert_ok "the fetched ref is reachable in PARENT's object store" \
  git -C "$PARENT" rev-parse --verify --quiet "refs/pr-browser-preview/${PR_NUMBER}^{commit}"

echo "=== pbt_fetch_head — unknown remote fails cleanly ==="
OUT_BADREMOTE="$(pbt_fetch_head "$PR_NUMBER" "no-such-remote" "$PARENT" 2>&1)"
RC_BADREMOTE=$?
assert_nonzero "unknown remote fails" "$RC_BADREMOTE"

echo "=== pbt_materialize — the tree it builds has the PR head's content, not main's (D#2549 criterion 1) ==="
DEST="$WORK/preview-tree"
pbt_materialize "$GOT_SHA" "$DEST" "$PARENT" >build.log 2>&1
RC2=$?
cat build.log
rm -f build.log
assert_rc "pbt_materialize exits 0" 0 "$RC2"
assert_eq "materialized tree HEAD is exactly the PR head sha" "$PR_SHA" "$(git -C "$DEST" rev-parse HEAD)"

MARKER_CONTENT="$(cat "$DEST/dashboard/src/marker.txt" 2>/dev/null || echo "<missing>")"
assert_ok "materialized tree contains the PR-head-only marker string" \
  bash -c "printf '%s' '$MARKER_CONTENT' | grep -qF 'PR_HEAD_ONLY_MARKER'"
assert_not "materialized tree does NOT contain the main-only marker string — this is the defect D#2549 exists to eliminate" \
  bash -c "printf '%s' '$MARKER_CONTENT' | grep -qF 'MAIN_ONLY_MARKER'"

echo "=== pbt_materialize — refuses to build over an existing path (inherited from verify_tree_build) ==="
OUT3="$(pbt_materialize "$GOT_SHA" "$DEST" "$PARENT" 2>&1)"
RC3=$?
assert_nonzero "refuses to rebuild over an existing dest" "$RC3"

echo "=== pbt_pick_free_port — returns a bindable port, never 5173 ==="
PORT1="$(pbt_pick_free_port)"
RC4=$?
assert_rc "pbt_pick_free_port exits 0" 0 "$RC4"
assert_ok "port is numeric" bash -c "[[ '$PORT1' =~ ^[0-9]+$ ]]"
assert_not "picked port is never 5173 (the shared dashboard's port)" test "$PORT1" = "5173"

PORT2="$(pbt_pick_free_port)"
assert_ok "picking again returns a usable port too" bash -c "[[ '$PORT2' =~ ^[0-9]+$ ]]"

echo "=== usage errors ==="
OUT5="$(pbt_fetch_head 2>&1)"
RC5=$?
assert_nonzero "pbt_fetch_head with no args returns a usage error" "$RC5"

OUT6="$(pbt_materialize 2>&1)"
RC6=$?
assert_nonzero "pbt_materialize with no args returns a usage error" "$RC6"

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
