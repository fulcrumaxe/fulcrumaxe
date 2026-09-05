#!/usr/bin/env bash
# tests/test_review_role_tree_isolation.sh — concurrency test for D#1684.
#
# Run: bash tests/test_review_role_tree_isolation.sh   (expects exit 0)
#
# D#1684's whole premise is that two review-role agents sharing one physical
# checkout race on HEAD when each runs its own branch checkout. The fix
# (backend/spawn_templates/acceptance-tester.tmpl STEP 0) routes acceptance-
# tester through scripts/lib/verify-tree.sh's verify_tree_build instead, which
# clones into its own destination rather than mutating shared branch state.
# This test is the concurrency check the original filing (and Spec criterion
# 5) asked for: prove two concurrent builds into two destinations land on the
# right, distinct commits, and prove a collision (two builds racing on ONE
# destination) is caught rather than silently reported as two passes.
#
# Everything runs against a small synthetic parent repo and /tmp destinations,
# same convention as tests/test_verify_tree.sh — this must not create, move,
# or delete anything in the real checkout.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/verify-tree.sh"
PASS=0
FAIL=0

ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; shift; [ $# -gt 0 ] && echo "        $*"; FAIL=$((FAIL + 1)); }
assert_rc() {
  if [ "$3" -eq "$2" ]; then ok "$1 (exit $3)"; else bad "$1" "expected exit $2, got $3"; fi
}
assert_ok() { local l="$1"; shift; if "$@"; then ok "$l"; else bad "$l" "expected true: $*"; fi; }

WORK="$(mktemp -d)"
export AUTONOMOUS_TEAM_STATE_DIR="$WORK/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"
cleanup() { chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

# --- synthetic parent repo, two distinct commits reachable in it -----------
PARENT="$WORK/parent"
mkdir -p "$PARENT"
(
  cd "$PARENT" || exit 1
  git init --quiet -b main .
  git config user.email "test@example.invalid"
  git config user.name "tree-isolation test"
  echo "commit A" > CLAUDE.md
  git add -A && git commit --quiet -m "commit A"
) || { echo "FATAL: could not build fixture repo (commit A)"; exit 1; }
SHA_A="$(git -C "$PARENT" rev-parse HEAD)"

(
  cd "$PARENT" || exit 1
  echo "commit B" > CLAUDE.md
  git add -A && git commit --quiet -m "commit B"
) || { echo "FATAL: could not build fixture repo (commit B)"; exit 1; }
SHA_B="$(git -C "$PARENT" rev-parse HEAD)"

[ "$SHA_A" != "$SHA_B" ] || { echo "FATAL: fixture commits are not distinct"; exit 1; }

# shellcheck source=scripts/lib/verify-tree.sh
source "$LIB"

echo "=== two concurrent builds into two destinations land on the right, distinct SHAs ==="
DEST_A="$WORK/dest-a"
DEST_B="$WORK/dest-b"

verify_tree_build "$SHA_A" "$DEST_A" "$PARENT" > "$WORK/build-a.log" 2>&1 &
PID_A=$!
verify_tree_build "$SHA_B" "$DEST_B" "$PARENT" > "$WORK/build-b.log" 2>&1 &
PID_B=$!

wait "$PID_A"; RC_A=$?
wait "$PID_B"; RC_B=$?

assert_rc "build into dest-a exits 0" 0 "$RC_A"
assert_rc "build into dest-b exits 0" 0 "$RC_B"
assert_ok "dest-a HEAD is commit A, not commit B" test "$(git -C "$DEST_A" rev-parse HEAD 2>/dev/null)" = "$SHA_A"
assert_ok "dest-b HEAD is commit B, not commit A" test "$(git -C "$DEST_B" rev-parse HEAD 2>/dev/null)" = "$SHA_B"

echo "=== inverted: two builds racing on the SAME destination — collision must be caught, not silently double-passed ==="
# verify_tree_build refuses to build over an existing path (a fresh TOCTOU
# check right at its top). Racing two builds at one destination therefore
# has exactly one non-racy outcome by design: at most one of them can win the
# clone. Run it several times — timing is not guaranteed on any single try —
# and assert the invariant holds on every iteration: it is never the case
# that BOTH report success. A single "both rc=0" would mean the shared
# destination was silently and inconsistently populated by two different
# commits, which is precisely the class of bug this Discussion is about.
BOTH_SUCCEEDED=0
ITERATIONS=10
for i in $(seq 1 "$ITERATIONS"); do
  DEST_C="$WORK/dest-collision-$i"

  verify_tree_build "$SHA_A" "$DEST_C" "$PARENT" > "$WORK/collide-a-$i.log" 2>&1 &
  P1=$!
  verify_tree_build "$SHA_B" "$DEST_C" "$PARENT" > "$WORK/collide-b-$i.log" 2>&1 &
  P2=$!

  wait "$P1"; R1=$?
  wait "$P2"; R2=$?

  if [ "$R1" -eq 0 ] && [ "$R2" -eq 0 ]; then
    BOTH_SUCCEEDED=$((BOTH_SUCCEEDED + 1))
    bad "iteration $i: both concurrent builds into the same destination reported success" \
        "dest=$DEST_C rc1=$R1 rc2=$R2 — expected at most one to succeed"
  fi

  # Whichever one (if either) won, the surviving tree must be internally
  # consistent — HEAD must match ONE of the two SHAs it was ever asked to
  # build, never something else entirely (e.g. an interleaved half-write).
  if [ -e "$DEST_C" ]; then
    GOT="$(git -C "$DEST_C" rev-parse HEAD 2>/dev/null || echo "unreadable")"
    if [ "$GOT" = "$SHA_A" ] || [ "$GOT" = "$SHA_B" ]; then
      : # consistent — one of the two builds legitimately owns this tree
    else
      bad "iteration $i: collided destination HEAD is neither SHA it was built from" "got=$GOT"
    fi
  fi
done

if [ "$BOTH_SUCCEEDED" -eq 0 ]; then
  ok "collision detected on every iteration ($ITERATIONS/$ITERATIONS) — never a silent double pass"
else
  bad "collision NOT detected on $BOTH_SUCCEEDED/$ITERATIONS iterations"
fi

echo
echo "PASS: $PASS  FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
