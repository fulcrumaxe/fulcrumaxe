#!/usr/bin/env bash
# tests/test_tree_capability.sh — unit tests for scripts/lib/tree-capability.sh
# (D#1940 PR-b)
#
# Run: bash tests/test_tree_capability.sh   (expects exit 0)
#
# Same convention as tests/test_verify_tree.sh and tests/test_pr_tree_provisioning.sh:
# most of this runs against small synthetic repos built in a temp dir, so it
# finishes in seconds and never mutates the real checkout. The one exception
# is deliberate (item 14 below): the regression this file exists to guard
# against is specifically about THIS repo's own tests/test_fleet_register_hook.py
# and backend/repo_root.py, so that one check runs pytest against a real clone
# and a real archive extraction of the checkout this test lives in.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/tree-capability.sh"
VT_LIB="$REPO_ROOT/scripts/lib/verify-tree.sh"
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
export AUTONOMOUS_TEAM_STATE_DIR="$WORK/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"
cleanup() { chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

# shellcheck source=scripts/lib/tree-capability.sh
source "$LIB"
# shellcheck source=scripts/lib/verify-tree.sh
source "$VT_LIB"

echo "=== usage error ==="
OUT_USAGE="$(tree_capability_assert 2>&1)"
assert_rc "missing <dir> returns usage error" 5 $?
assert_contains "usage message names the function" "usage: tree_capability_assert" "$OUT_USAGE"

# ── a small synthetic parent repo, real history, two commits ────────────────
PARENT="$WORK/parent"
mkdir -p "$PARENT"
(
  cd "$PARENT" || exit 1
  git init --quiet -b main .
  git config user.email "test@example.invalid"
  git config user.name "tree-capability test"
  echo "first" > a.txt
  git add -A && git commit --quiet -m "first"
  echo "second" >> a.txt
  git add -A && git commit --quiet -m "second"
) || { echo "FATAL: could not build fixture parent repo"; exit 1; }
PARENT_SHA="$(git -C "$PARENT" rev-parse HEAD)"

echo "=== FM-1 (item 12) — mandatory positive control: a REAL archive extraction ==="
ARCHIVE_DIR="$WORK/fm1-archive"
mkdir -p "$ARCHIVE_DIR"
git -C "$PARENT" archive --format=tar "$PARENT_SHA" | tar -x -C "$ARCHIVE_DIR"
assert_rc "archive | tar -x succeeded" 0 $?
assert_ok "the extraction directory has real files" test -f "$ARCHIVE_DIR/a.txt"
assert_not ".git is absent in the extraction (the whole premise of FM-1)" test -e "$ARCHIVE_DIR/.git"

OUT1="$(tree_capability_assert "$ARCHIVE_DIR" 2>&1)"
RC1=$?
assert_rc "archive extraction is rejected with the FM-1 code" 1 "$RC1"
assert_contains "message names the shape" "no git metadata" "$OUT1"
assert_contains "message names FM-1" "FM-1" "$OUT1"

echo "=== FM-2 (synthetic history) — named test ==="
SYN="$WORK/fm2-synthetic"
mkdir -p "$SYN"
(
  cd "$SYN" || exit 1
  git init --quiet -b main .
  git config user.email "test@example.invalid"
  git config user.name "tree-capability test"
  echo "only commit" > x.txt
  git add -A && git commit --quiet -m "root"
) || { echo "FATAL: could not build FM-2 fixture"; exit 1; }
SYN_SHA="$(git -C "$SYN" rev-parse HEAD)"

OUT2="$(tree_capability_assert "$SYN" 2>&1)"
RC2=$?
assert_rc "parentless root commit, no expected_sha, is rejected with the FM-2 code" 2 "$RC2"
assert_contains "message names the shape" "synthetic history" "$OUT2"

OUT2b="$(tree_capability_assert "$SYN" "0000000000000000000000000000000000dead" 2>&1)"
RC2b=$?
assert_rc "parentless root commit, WRONG expected_sha, is still rejected with FM-2" 2 "$RC2b"

OUT2c="$(tree_capability_assert "$SYN" "$SYN_SHA" 2>&1)"
RC2c=$?
assert_rc "parentless root commit that IS the expected_sha is not synthetic-history — passes" 0 "$RC2c"

echo "=== FM-3 (commit not present) — named test ==="
BOGUS_SHA="0000000000000000000000000000000000dead"
OUT3="$(tree_capability_assert "$PARENT" "$BOGUS_SHA" 2>&1)"
RC3=$?
assert_rc "unreachable expected_sha is rejected with the FM-3 code" 3 "$RC3"
assert_contains "message names the shape" "commit not present" "$OUT3"
assert_contains "message names the missing sha" "$BOGUS_SHA" "$OUT3"

OUT3b="$(tree_capability_assert "$PARENT" "$PARENT_SHA" 2>&1)"
RC3b=$?
assert_rc "reachable expected_sha (HEAD itself) passes FM-3" 0 "$RC3b"

echo "=== FM-4 (unresolvable comparison base) — named test ==="
# A tree with SOME remote-tracking data (so FM-4 is not skipped as
# not-applicable) but no resolvable main on the code plane's remote.
FM4_BARE="$WORK/fm4-bare.git"
git init --quiet --bare "$FM4_BARE"
FM4_SRC="$WORK/fm4-src"
git clone --quiet "$FM4_BARE" "$FM4_SRC"
(
  cd "$FM4_SRC" || exit 1
  git config user.email "test@example.invalid"
  git config user.name "tree-capability test"
  echo "root" > f.txt
  git add -A && git commit --quiet -m "root"
  echo "feature only, no main pushed" >> f.txt
  git add -A && git commit --quiet -m "feature"
  git push --quiet origin HEAD:refs/heads/some-other-branch
) || { echo "FATAL: could not build FM-4 fixture"; exit 1; }

FM4_DIR="$WORK/fm4-dir"
git clone --quiet "$FM4_BARE" "$FM4_DIR"
(
  cd "$FM4_DIR" || exit 1
  git config user.email "test@example.invalid"
  git config user.name "tree-capability test"
  git fetch --quiet origin some-other-branch
  git checkout --quiet -b local-work origin/some-other-branch
) || { echo "FATAL: could not set up FM-4 working dir"; exit 1; }
assert_ok "FM-4 fixture has SOME remote-tracking refs (origin/some-other-branch)" \
  test -n "$(git -C "$FM4_DIR" for-each-ref refs/remotes/origin --format='x')"
assert_not "FM-4 fixture has NO origin/main" \
  git -C "$FM4_DIR" rev-parse --verify --quiet origin/main

OUT4="$(CODE_PLANE_REMOTE_OVERRIDE="origin" tree_capability_assert "$FM4_DIR" 2>&1)"
RC4=$?
assert_rc "remote-tracked tree with no origin/main is rejected with the FM-4 code" 4 "$RC4"
assert_contains "message names the shape" "unresolvable comparison base" "$OUT4"

echo "=== FM-4 negative: origin/main present and an ancestor — passes ==="
(
  cd "$FM4_SRC" || exit 1
  git push --quiet origin HEAD:refs/heads/main
) || { echo "FATAL: could not push main for FM-4 negative fixture"; exit 1; }
git -C "$FM4_DIR" fetch --quiet origin main
assert_ok "origin/main now resolves in the FM-4 dir" \
  git -C "$FM4_DIR" rev-parse --verify --quiet origin/main
OUT4b="$(CODE_PLANE_REMOTE_OVERRIDE="origin" tree_capability_assert "$FM4_DIR" 2>&1)"
RC4b=$?
assert_rc "origin/main present and an ancestor of HEAD passes" 0 "$RC4b"

echo "=== negative control (item 13) — a tree built by verify_tree_build passes ==="
VT_DEST="$WORK/vt-dest"
verify_tree_build "$PARENT_SHA" "$VT_DEST" "$PARENT" > /dev/null 2>&1
assert_rc "verify_tree_build succeeds" 0 $?
assert_not "verify_tree_build tree has NO remote-tracking refs (by design — see header note)" \
  test -n "$(git -C "$VT_DEST" for-each-ref refs/remotes --format='x')"

OUT5="$(tree_capability_assert "$VT_DEST" "$PARENT_SHA" 2>&1)"
RC5=$?
assert_rc "a verify_tree_build tree is CAPABLE — the check is not merely refusing everything" 0 "$RC5"

echo "=== item 11 — the evidence record ==="
assert_contains "evidence line is present" "tree-capability: OK" "$OUT5"
assert_contains "evidence line names HEAD" "HEAD: $PARENT_SHA" "$OUT5"
assert_contains "evidence line states whether HEAD has a parent" "has parent:" "$OUT5"
assert_contains "evidence line names the comparison base" "comparison base:" "$OUT5"
assert_contains "evidence line names the resolved git dir" "git dir:" "$OUT5"

echo "=== item 14 — the regression this check exists for ==="
if ! command -v pytest > /dev/null 2>&1 && ! python3 -m pytest --version > /dev/null 2>&1; then
  echo "  SKIP: pytest is not available in this environment — item 14's regression leg did not run"
else
  REAL_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  REAL_DEST="$WORK/real-vt-dest"
  verify_tree_build "$REAL_SHA" "$REAL_DEST" "$REPO_ROOT" > /dev/null 2>&1
  VT_RC=$?
  if [ "$VT_RC" -ne 0 ] || [ ! -f "$REAL_DEST/tests/test_fleet_register_hook.py" ]; then
    bad "could not build a real verify_tree_build tree to regression-test against" "verify_tree_build rc=$VT_RC, or tests/test_fleet_register_hook.py missing — is this checkout the real project?"
  else
    OUT_REAL_CAP="$(tree_capability_assert "$REAL_DEST" "$REAL_SHA" 2>&1)"
    assert_rc "the real verify_tree_build tree is capable" 0 $?

    # verify_tree_build's clone checks out $REAL_SHA's OWN tree, not
    # $REPO_ROOT's working tree. On the code plane, .autonomous-team/
    # config.json and project.json are deliberately untracked (tracking them
    # would publish the private Discussion-plane slug on a public repo — see
    # scripts/lib/repo-resolve.sh's own header), so a bare clone of a
    # code-plane commit never has them — a bare `git clone` (verify_tree_build
    # included) only ever checks out TRACKED content. Without either file,
    # backend/spawn_templates.py raises at import (it needs SOME resolvable
    # repo slug), so pytest fails to even COLLECT — a different failure than
    # anything tree_capability_assert checks for: the tree itself has real
    # git metadata, real history, and the right commit. This is a
    # per-checkout bootstrap dependency (every real checkout in this
    # environment has one, set up once by coldstart), not a tree-capability
    # shape, so this leg supplies the minimum a bootstrap would: a
    # `project_name` (already a public literal — tests/test_fleet_register_hook.py
    # hardcodes the identical "fulcrumaxe" itself) and a `repo` key using
    # ONLY this repo's own public slug, never the private one, so nothing
    # here can leak what .autonomous-team/config.json's exclusion protects.
    if [ ! -f "$REAL_DEST/.autonomous-team/config.json" ]; then
      mkdir -p "$REAL_DEST/.autonomous-team"
      printf '{"project_name": "fulcrumaxe"}\n' > "$REAL_DEST/.autonomous-team/config.json"
    fi
    if [ ! -f "$REAL_DEST/.autonomous-team/project.json" ]; then
      mkdir -p "$REAL_DEST/.autonomous-team"
      printf '{"repo": "fulcrumaxe/fulcrumaxe"}\n' > "$REAL_DEST/.autonomous-team/project.json"
    fi

    PYTEST_STATE="$(mktemp -d)"
    PYOUT="$(cd "$REAL_DEST" && AUTONOMOUS_TEAM_STATE_DIR="$PYTEST_STATE" python3 -m pytest tests/test_fleet_register_hook.py -q 2>&1)"
    assert_contains "a real verify_tree_build tree, bootstrapped like any other checkout, reports 22 passed (the FM-1 regression's baseline)" "22 passed" "$PYOUT"
    rm -rf "$PYTEST_STATE"

    # The archive extraction is rejected BEFORE any test is run in it — no
    # pytest invocation happens on this path at all if the check works.
    REAL_ARCHIVE="$WORK/real-fm1-archive"
    mkdir -p "$REAL_ARCHIVE"
    git -C "$REPO_ROOT" archive --format=tar "$REAL_SHA" | tar -x -C "$REAL_ARCHIVE"
    assert_not "the real archive extraction has no .git" test -e "$REAL_ARCHIVE/.git"
    PYTEST_INVOKED=false
    OUT_REAL_REJECT="$(tree_capability_assert "$REAL_ARCHIVE" 2>&1)"
    RC_REAL_REJECT=$?
    assert_rc "the real archive extraction is rejected before any test runs" 1 "$RC_REAL_REJECT"
    assert_ok "pytest was never invoked against the rejected tree" test "$PYTEST_INVOKED" = "false"
  fi
fi

echo
echo "PASS: $PASS  FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
