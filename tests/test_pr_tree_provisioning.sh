#!/usr/bin/env bash
# tests/test_pr_tree_provisioning.sh — unit tests for scripts/lib/pr-tree.sh (D#2014)
#
# Run: bash tests/test_pr_tree_provisioning.sh   (expects exit 0)
#
# Same convention as tests/test_verify_tree.sh: everything runs against a
# small synthetic ORIGIN (bare, stands in for GitHub) + PARENT (a working
# clone, stands in for the real checkout scripts/spawn-agent.sh runs in) pair
# built in a temp dir, so this finishes in seconds and never touches the real
# repo or GitHub. The PR's refs/pull/<N>/head is set directly on ORIGIN,
# exactly the way GitHub creates it for a real open PR.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/pr-tree.sh"
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
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain: $2 — got: $3"; fi
}
assert_ok() { local l="$1"; shift; if "$@"; then ok "$l"; else bad "$l" "expected true: $*"; fi; }
assert_not() { local l="$1"; shift; if "$@"; then bad "$l" "expected false: $*"; else ok "$l"; fi; }

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

ORIGIN="$WORK/origin.git"
PARENT="$WORK/parent"

git init --quiet --bare "$ORIGIN"

git clone --quiet "$ORIGIN" "$PARENT"
(
  cd "$PARENT" || exit 1
  git config user.email "test@example.invalid"
  git config user.name "pr-tree test"
  echo "hub content" > CLAUDE.md
  git add -A && git commit --quiet -m "first" --allow-empty
  git push --quiet origin HEAD:refs/heads/main
) || { echo "FATAL: could not build fixture main history"; exit 1; }

# Simulate an open PR: a second clone builds the PR commit, pushes it to a
# real branch (as a contributor's fork/branch would be), and we additionally
# point refs/pull/<N>/head at it directly on ORIGIN — exactly what GitHub
# does automatically for any open PR, real branch or not.
PR_WORK="$WORK/pr-author-clone"
git clone --quiet "$ORIGIN" "$PR_WORK"
(
  cd "$PR_WORK" || exit 1
  git config user.email "pr-author@example.invalid"
  git config user.name "pr author"
  echo "pr change" >> CLAUDE.md
  git add -A && git commit --quiet -m "pr change"
  git push --quiet origin HEAD:refs/heads/pr-branch
) || { echo "FATAL: could not build fixture PR branch"; exit 1; }

PR_SHA="$(git -C "$PR_WORK" rev-parse HEAD)"
PR_NUMBER=42
git -C "$ORIGIN" update-ref "refs/pull/${PR_NUMBER}/head" "$PR_SHA"

# shellcheck source=scripts/lib/pr-tree.sh
source "$LIB"

echo "=== pr_tree_provision — happy path (checks 1-3) ==="
DEST="$WORK/tree"
OUT="$(pr_tree_provision "$PR_NUMBER" "$PR_SHA" "$DEST" "$PARENT" 2>err.log)"
RC=$?
ERR="$(cat err.log 2>/dev/null)"; rm -f err.log
assert_rc "provision exits 0" 0 "$RC"
assert_ok "stdout prints the dest path" test "$OUT" = "$DEST"
assert_ok "HEAD is exactly the PR head sha" test "$(git -C "$DEST" rev-parse HEAD)" = "$PR_SHA"
assert_contains "stderr reports the provisioned sha" "$PR_SHA" "$ERR"

echo "=== origin remote points at GitHub (ORIGIN), not the local checkout (check 3) ==="
GOT_ORIGIN="$(git -C "$DEST" remote get-url origin)"
assert_ok "origin is the shared remote, not a bare clone of PARENT" test "$GOT_ORIGIN" = "$ORIGIN"
assert_not "origin is NOT the parent checkout path" test "$GOT_ORIGIN" = "$PARENT"

echo "=== the provisioned tree is writable and pushable (no remote surgery needed) ==="
echo "amended" >> "$DEST/CLAUDE.md"
assert_rc "tree is writable" 0 $?
(
  cd "$DEST" || exit 1
  git config user.email "executor@example.invalid"
  git config user.name "executor test"
  git add -A && git commit --quiet -m "amend"
)
assert_ok "push origin HEAD:<pr-branch> works unchanged" \
  git -C "$DEST" push --quiet origin "HEAD:refs/heads/pr-branch"
assert_ok "the pushed commit landed on ORIGIN's pr-branch" \
  test "$(git -C "$ORIGIN" rev-parse refs/heads/pr-branch)" = "$(git -C "$DEST" rev-parse HEAD)"

echo "=== refusing to provision over an existing path ==="
OUT2="$(pr_tree_provision "$PR_NUMBER" "$PR_SHA" "$DEST" "$PARENT" 2>&1)"
RC2=$?
assert_nonzero "refuses when dest already exists" "$RC2"
assert_contains "reason names the existing-path refusal" "refusing to provision over an existing path" "$OUT2"
assert_ok "the original tree at DEST is untouched" test "$(git -C "$DEST" rev-parse HEAD)" != ""

echo "=== unreachable sha fails cleanly and leaves nothing behind (negative case) ==="
BOGUS_SHA="0000000000000000000000000000000000dead"
BOGUS_DEST="$WORK/tree-bogus"
OUT3="$(pr_tree_provision 999 "$BOGUS_SHA" "$BOGUS_DEST" "$PARENT" 2>&1)"
RC3=$?
assert_nonzero "unreachable PR head fails" "$RC3"
assert_not "no half-built tree left behind" test -e "$BOGUS_DEST"

echo "=== usage errors (missing args) ==="
OUT4="$(pr_tree_provision 2>&1)"
RC4=$?
assert_rc "missing args returns usage error" 3 "$RC4"
assert_contains "usage message names the function" "usage: pr_tree_provision" "$OUT4"

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
