#!/usr/bin/env bash
# tests/test_pr_tree_provisioning.sh — unit tests for scripts/lib/pr-tree.sh (D#2014, D#1940 PR-a)
#
# Run: bash tests/test_pr_tree_provisioning.sh   (expects exit 0)
#
# Same convention as tests/test_verify_tree.sh: everything runs against a
# small synthetic ORIGIN (bare, stands in for GitHub) + PARENT (a working
# clone, stands in for the real checkout scripts/spawn-agent.sh runs in) pair
# built in a temp dir, so this finishes in seconds and never touches the real
# repo or GitHub. The PR's refs/pull/<N>/head is set directly on ORIGIN,
# exactly the way GitHub creates it for a real open PR.
#
# D#1940 FM-5 additions (PR-a): pr_tree_provision now resolves the code-plane
# git remote via scripts/lib/repo-resolve.sh's _resolve_code_plane_remote
# instead of a hardcoded "origin", and independently cross-checks the
# fetched head against the code plane's authoritative headRefOid. The
# fixture ORIGIN above stands in for the code plane too (its remote name in
# PARENT is "origin", which does not match the real project's code_repo
# slug) — CODE_PLANE_REMOTE_OVERRIDE and PRT_EXPECTED_HEAD_OVERRIDE (both
# test-only escape hatches, same convention as worktree-claims.sh's
# WTC_*_OVERRIDE vars) let the pre-existing tests below keep exercising the
# ORIGIN/PARENT fixture without a real "fulcrumaxe/fulcrumaxe" GitHub remote
# or a live `gh pr view` call. The NEW tests further down (collision /
# unresolvable-remote / headRefOid-mismatch) deliberately do NOT set these
# overrides where the point is to exercise the real resolution logic.

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
MAIN_SHA="$(git -C "$PARENT" rev-parse HEAD)"

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

# Fixture escape hatches for the pre-existing ORIGIN/PARENT tests below: the
# fixture's remote is named "origin" and points at a local bare path, not a
# real "fulcrumaxe/fulcrumaxe" GitHub remote, so it would never match the
# real code-plane slug on its own. These two overrides make the fixture
# behave, for these tests only, as if "origin" were the resolved code-plane
# remote and PR_SHA were the code plane's authoritative headRefOid.
export CODE_PLANE_REMOTE_OVERRIDE="origin"
export PRT_EXPECTED_HEAD_OVERRIDE="$PR_SHA"

echo "=== pr_tree_provision — happy path (checks 1-3) ==="
DEST="$WORK/tree"
OUT="$(pr_tree_provision "$PR_NUMBER" "$PR_SHA" "$DEST" "code" "$PARENT" 2>err.log)"
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
OUT2="$(pr_tree_provision "$PR_NUMBER" "$PR_SHA" "$DEST" "code" "$PARENT" 2>&1)"
RC2=$?
assert_nonzero "refuses when dest already exists" "$RC2"
assert_contains "reason names the existing-path refusal" "refusing to provision over an existing path" "$OUT2"
assert_ok "the original tree at DEST is untouched" test "$(git -C "$DEST" rev-parse HEAD)" != ""

echo "=== unreachable sha fails cleanly and leaves nothing behind (negative case) ==="
BOGUS_SHA="0000000000000000000000000000000000dead"
BOGUS_DEST="$WORK/tree-bogus"
OUT3="$(pr_tree_provision 999 "$BOGUS_SHA" "$BOGUS_DEST" "code" "$PARENT" 2>&1)"
RC3=$?
assert_nonzero "unreachable PR head fails" "$RC3"
assert_not "no half-built tree left behind" test -e "$BOGUS_DEST"

echo "=== usage errors (missing args) ==="
OUT4="$(pr_tree_provision 2>&1)"
RC4=$?
assert_rc "missing args returns usage error" 3 "$RC4"
assert_contains "usage message names the function" "usage: pr_tree_provision" "$OUT4"

unset CODE_PLANE_REMOTE_OVERRIDE
unset PRT_EXPECTED_HEAD_OVERRIDE

# ─────────────────────────────────────────────────────────────────────────
# D#1940 FM-5 — the code plane must be resolved, never assumed to be
# "origin". The tests below deliberately do NOT set CODE_PLANE_REMOTE_OVERRIDE
# (except where explicitly noted), so they exercise the real
# _resolve_code_plane_remote matching logic against this checkout's actual
# code_repo slug.
# ─────────────────────────────────────────────────────────────────────────

REAL_CODE_REPO="$(_resolve_code_repo 2>/dev/null || true)"
if [ -z "$REAL_CODE_REPO" ]; then
  echo "FATAL: could not resolve this checkout's real code_repo slug — cannot run the FM-5 tests"
  exit 1
fi

echo "=== (D#1940 item 3) aborts non-zero, naming the plane, when the code-plane remote cannot be resolved ==="
# PARENT's only remote is "origin" -> a local bare path with no relation to
# the real code_repo slug, so with no override set, resolution must fail.
UNRESOLVED_DEST="$WORK/tree-unresolved"
OUT5="$(pr_tree_provision "$PR_NUMBER" "$PR_SHA" "$UNRESOLVED_DEST" "code" "$PARENT" 2>&1)"
RC5=$?
assert_nonzero "provision aborts when the code-plane remote is unresolvable" "$RC5"
assert_contains "message names the code plane slug" "$REAL_CODE_REPO" "$OUT5"
assert_not "no half-built tree left behind" test -e "$UNRESOLVED_DEST"

echo "=== (D#1940 review) _resolve_code_plane_remote fails closed — tested directly, not through pr_tree_provision ==="
# A composed end-to-end nonzero exit from pr_tree_provision does not by
# itself prove the RESOLVER refused: a resolver mutated to wrongly fall back
# to a remote name (e.g. "origin") instead of failing would often still make
# pr_tree_provision abort later for an unrelated reason (the headRefOid
# cross-check calling a live `gh pr view` with no override in this block),
# so an "aborts nonzero" assertion alone can pass for the wrong reason. Call
# the resolver directly and pin its exact documented contract instead: exit
# 1 (not some other nonzero), nothing on stdout (a fallback would print a
# remote name here), and the plane named on stderr.
RESOLVE_ERR_LOG="$WORK/resolve_err.log"
RESOLVE_OUT="$(_resolve_code_plane_remote "$PARENT" 2>"$RESOLVE_ERR_LOG")"
RESOLVE_RC=$?
RESOLVE_ERR="$(cat "$RESOLVE_ERR_LOG" 2>/dev/null)"; rm -f "$RESOLVE_ERR_LOG"
assert_rc "_resolve_code_plane_remote returns exactly 1 on an unmatched remote (its documented contract)" 1 "$RESOLVE_RC"
assert_ok "_resolve_code_plane_remote prints NOTHING on stdout on failure (a fallback-to-a-remote-name mutation would print one here)" \
  test -z "$RESOLVE_OUT"
assert_contains "_resolve_code_plane_remote's stderr names the unresolved plane" "$REAL_CODE_REPO" "$RESOLVE_ERR"

echo "=== (D#1940 item 4) positive control — PR number collision across planes, fetch lands the CODE plane's head ==="
# Build two independent bare "remotes" both claiming PR #67, with distinct
# heads — the exact shape D#1940 measured live: the code plane's PR #67 and
# the Discussion plane's PR #67 are unrelated commits. One remote is named
# "origin" (Discussion plane stand-in); the other's URL is engineered to
# END in the real code_repo slug so _resolve_code_plane_remote's URL-suffix
# match picks it up WITHOUT any override — proving the real matching logic,
# not a mock, resolves to the correct remote.
COLLISION_WORK="$WORK/collision"
mkdir -p "$COLLISION_WORK"

DISCUSSION_BARE="$COLLISION_WORK/discussion-plane.git"
git init --quiet --bare "$DISCUSSION_BARE"
DISCUSSION_CLONE="$COLLISION_WORK/discussion-clone"
git clone --quiet "$DISCUSSION_BARE" "$DISCUSSION_CLONE"
(
  cd "$DISCUSSION_CLONE" || exit 1
  git config user.email "disc@example.invalid"
  git config user.name "discussion plane"
  echo "five-month-old TUI commit" > payload.txt
  git add -A && git commit --quiet -m "add auto-reconnect (Discussion plane PR #67)" --allow-empty
  git push --quiet origin HEAD:refs/heads/some-branch
) || { echo "FATAL: could not build discussion-plane collision fixture"; exit 1; }
DISCUSSION_SHA="$(git -C "$DISCUSSION_CLONE" rev-parse HEAD)"
git -C "$DISCUSSION_BARE" update-ref refs/pull/67/head "$DISCUSSION_SHA"

# The bare repo's own path deliberately ends in the real code_repo slug
# (e.g. .../fulcrumaxe/fulcrumaxe) so the URL-suffix match in
# _resolve_code_plane_remote finds it with no override.
CODE_BARE="$COLLISION_WORK/remotes/$REAL_CODE_REPO"
mkdir -p "$(dirname "$CODE_BARE")"
git init --quiet --bare "$CODE_BARE"
CODE_CLONE="$COLLISION_WORK/code-clone"
git clone --quiet "$CODE_BARE" "$CODE_CLONE"
(
  cd "$CODE_CLONE" || exit 1
  git config user.email "code@example.invalid"
  git config user.name "code plane"
  echo "code-plane-pr helper" > payload.txt
  git add -A && git commit --quiet -m "add code-plane-pr.sh (code plane PR #67)" --allow-empty
  git push --quiet origin HEAD:refs/heads/code-plane-pr-helper
) || { echo "FATAL: could not build code-plane collision fixture"; exit 1; }
CODE_SHA="$(git -C "$CODE_CLONE" rev-parse HEAD)"
git -C "$CODE_BARE" update-ref refs/pull/67/head "$CODE_SHA"

[ "$DISCUSSION_SHA" != "$CODE_SHA" ] || { echo "FATAL: collision fixture SHAs are not distinct"; exit 1; }

COLLISION_PARENT="$COLLISION_WORK/parent"
git init --quiet -b main "$COLLISION_PARENT"
git -C "$COLLISION_PARENT" remote add origin "$DISCUSSION_BARE"
git -C "$COLLISION_PARENT" remote add code-plane-fixture "$CODE_BARE"

COLLISION_DEST="$COLLISION_WORK/tree"
# No CODE_PLANE_REMOTE_OVERRIDE here — real slug-matching resolution.
# PRT_EXPECTED_HEAD_OVERRIDE stands in for the live `gh pr view` call (item
# 5's cross-check) so this test makes no network call.
OUT6="$(PRT_EXPECTED_HEAD_OVERRIDE="$CODE_SHA" pr_tree_provision 67 "$CODE_SHA" "$COLLISION_DEST" "code" "$COLLISION_PARENT" 2>&1)"
RC6=$?
assert_rc "provision of colliding PR #67 exits 0" 0 "$RC6"
assert_ok "resolved HEAD is the CODE plane's sha" \
  test "$(git -C "$COLLISION_DEST" rev-parse HEAD 2>/dev/null)" = "$CODE_SHA"
assert_not "resolved HEAD is NOT the Discussion plane's sha" \
  test "$(git -C "$COLLISION_DEST" rev-parse HEAD 2>/dev/null)" = "$DISCUSSION_SHA"

echo "=== (D#1940 item 5) headRefOid mismatch is a hard abort ==="
# PR_SHA is genuinely reachable in PARENT (proven below), so a failure here
# can only come from the headRefOid cross-check catching the mismatch, not
# from unreachability.
MISMATCH_DEST="$WORK/tree-mismatch"
OUT7="$(CODE_PLANE_REMOTE_OVERRIDE="origin" PRT_EXPECTED_HEAD_OVERRIDE="$MAIN_SHA" \
  pr_tree_provision "$PR_NUMBER" "$PR_SHA" "$MISMATCH_DEST" "code" "$PARENT" 2>&1)"
RC7=$?
assert_nonzero "provision aborts on headRefOid mismatch" "$RC7"
assert_contains "message shows the mismatched shas" "$MAIN_SHA" "$OUT7"
assert_ok "the head_sha argument WAS reachable in parent (mismatch, not unreachability, caused the abort)" \
  git -C "$PARENT" rev-parse --verify --quiet "${PR_SHA}^{commit}"
assert_not "no half-built tree left behind on a headRefOid mismatch" test -e "$MISMATCH_DEST"

echo "=== (D#2563, differential) plane-qualified-ref check catches a coincidence even when the live headRefOid cross-check is defeated ==="
# The headRefOid cross-check (D#1940 PR-a, above) is a second, independent
# line of defense — but it depends on a live `gh pr view` call, which this
# test overrides to MATCH the wrong sha, isolating what the plane-qualified
# ref check (D#2563) catches on its own construction, not by luck of an
# object store that happens to already hold the right-looking commit.
#
# A second, wholly independent ORIGIN/PARENT pair, so this case cannot
# inherit any object-store coincidence from the fixtures built above.
ORIGIN2="$WORK/origin2.git"
PARENT2="$WORK/parent2"
git init --quiet --bare "$ORIGIN2"
git clone --quiet "$ORIGIN2" "$PARENT2"
(
  cd "$PARENT2" || exit 1
  git config user.email "test@example.invalid"
  git config user.name "pr-tree test"
  echo "hub2 content" > CLAUDE.md
  git add -A && git commit --quiet -m "first" --allow-empty
  git push --quiet origin HEAD:refs/heads/main
) || { echo "FATAL: could not build fixture2 main history"; exit 1; }

# Commit a "coincidence" object directly into PARENT2's own object store, on
# a throwaway local branch that is deleted (but never gc'd) so the commit
# stays present-but-unreferenced — exactly the situation that would let a
# reachability-only check pass without ever looking at what the fetch landed.
(
  cd "$PARENT2" || exit 1
  git checkout --quiet -b coincidence
  echo "looks like the real PR head, isn't" > decoy.txt
  git add -A && git commit --quiet -m "coincidence"
  git rev-parse HEAD > "$WORK/coincidence-sha.txt"
  git checkout --quiet main
  git branch -D coincidence --quiet
) || { echo "FATAL: could not build coincidence commit"; exit 1; }
COINCIDENCE_SHA="$(cat "$WORK/coincidence-sha.txt")"

# The REAL PR head on ORIGIN2 is a different, unrelated commit.
PR2_WORK="$WORK/pr2-author-clone"
git clone --quiet "$ORIGIN2" "$PR2_WORK"
(
  cd "$PR2_WORK" || exit 1
  git config user.email "pr-author@example.invalid"
  git config user.name "pr author"
  echo "the real pr change" >> CLAUDE.md
  git add -A && git commit --quiet -m "real pr change"
  git push --quiet origin HEAD:refs/heads/pr2-branch
) || { echo "FATAL: could not build fixture2 PR branch"; exit 1; }
PR2_REAL_SHA="$(git -C "$PR2_WORK" rev-parse HEAD)"
PR2_NUMBER=777
git -C "$ORIGIN2" update-ref "refs/pull/${PR2_NUMBER}/head" "$PR2_REAL_SHA"

# CODE_PLANE_REMOTE_OVERRIDE="origin" so the fixture's remote resolves the
# same way the happy-path tests above do. PRT_EXPECTED_HEAD_OVERRIDE is
# deliberately set to COINCIDENCE_SHA — the SAME wrong value as the
# head_sha argument below — so the live cross-check matches and cannot be
# what catches this. Only the plane-qualified-ref check (which reads what
# the fetch actually landed at refs/pr-tree/code/777, the real PR2_REAL_SHA)
# can still refuse.
AC7_DEST="$WORK/tree-ac7"
OUT8="$(CODE_PLANE_REMOTE_OVERRIDE="origin" PRT_EXPECTED_HEAD_OVERRIDE="$COINCIDENCE_SHA" \
  pr_tree_provision "$PR2_NUMBER" "$COINCIDENCE_SHA" "$AC7_DEST" "code" "$PARENT2" 2>&1)"
RC8=$?
assert_rc "D#2563: mismatched fetch-vs-claimed-head returns exactly 3, even with a matching live cross-check" 3 "$RC8"
assert_contains "D#2563: reason names the mismatch, not a bare 'not reachable'" "does not match what the fetch landed" "$OUT8"
assert_not "D#2563: no half-built tree left behind" test -e "$AC7_DEST"
# The coincidence commit really was reachable in the object store all along
# — proving this is not a false negative from a broken fixture.
assert_ok "D#2563: the coincidence commit really is in PARENT2's object store" \
  git -C "$PARENT2" rev-parse --verify --quiet "${COINCIDENCE_SHA}^{commit}"

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
