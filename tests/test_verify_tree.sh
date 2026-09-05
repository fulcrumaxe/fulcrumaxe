#!/usr/bin/env bash
# tests/test_verify_tree.sh — unit tests for scripts/lib/verify-tree.sh (D#1964)
#
# Run: bash tests/test_verify_tree.sh   (expects exit 0)
#
# Plain-bash, same convention as tests/test_ci_status_check.sh — the sibling
# libs' own style, which is what actually gets run here day to day.
#
# Everything runs against a small synthetic parent repo in a temp dir, so this
# finishes in seconds and never touches the real checkout. AUTONOMOUS_TEAM_STATE_DIR
# points at a temp dir so manifests land there, not in the live state directory.
# The whole-repo items (3572 tracked files, a full pytest run, the assertion
# wall-clock budget) are hand-run against the real repo and reported in the PR.

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
assert_nonzero() {
  if [ "$2" -ne 0 ]; then ok "$1 (exit $2)"; else bad "$1" "expected non-zero exit, got 0"; fi
}
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain: $2 — got: $3"; fi
}
# assert_ok <label> <cmd...> / assert_not <label> <cmd...>
assert_ok() { local l="$1"; shift; if "$@"; then ok "$l"; else bad "$l" "expected true: $*"; fi; }
assert_not() { local l="$1"; shift; if "$@"; then bad "$l" "expected false: $*"; else ok "$l"; fi; }

WORK="$(mktemp -d)"
export AUTONOMOUS_TEAM_STATE_DIR="$WORK/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"
# Protected files are read-only; restore write before removing.
cleanup() { chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

PARENT="$WORK/parent"
mkdir -p "$PARENT/.autonomous-team/hook-events" "$PARENT/hooks"
(
  cd "$PARENT" || exit 1
  git init --quiet -b main .
  git config user.email "test@example.invalid"
  git config user.name "verify-tree test"
  echo "hub content" > CLAUDE.md
  echo "sentinel" > hooks/sandbox.py
  echo '{"dial": 1}' > .autonomous-team/config.json
  echo '{"roles": []}' > .autonomous-team/agent-profiles.json
  echo "seed" > .autonomous-team/hook-events/seed.jsonl
  git add -A && git commit --quiet -m "first"
  echo "second commit" >> CLAUDE.md
  git add -A && git commit --quiet -m "second"
) || { echo "FATAL: could not build fixture repo"; exit 1; }

SHA="$(git -C "$PARENT" rev-parse HEAD)"
DEST="$WORK/tree"

# shellcheck source=scripts/lib/verify-tree.sh
source "$LIB"

echo "=== verify_tree_build (items 2, 4) ==="
OUT="$(verify_tree_build "$SHA" "$DEST" "$PARENT" 2>&1)"
assert_rc "build exits 0" 0 $?
assert_ok "HEAD is exactly the requested sha" test "$(git -C "$DEST" rev-parse HEAD)" = "$SHA"
assert_ok "clone carries real history" test "$(git -C "$DEST" rev-list --count HEAD)" -gt 1
MANIFEST="$(printf '%s' "$OUT" | sed -n 's/^verify-tree: manifest: //p')"
assert_ok "manifest exists under STATE_DIR/tree-manifests" test -f "$MANIFEST"
assert_not "manifest is outside the tree" grep -q "^$DEST/" <<< "$MANIFEST"
assert_not "no attestation dropping inside the tree" compgen -G "$DEST/.tree-attest*"
assert_not "carve-outs are absent from the manifest" grep -q 'autonomous-team/config.json' "$MANIFEST"
assert_ok "ordinary tracked files are in the manifest" grep -q 'hooks/sandbox.py' "$MANIFEST"

echo "=== write protection covers files, not directories (item 7) ==="
assert_not "tracked file is not writable" test -w "$DEST/CLAUDE.md"
touch "$DEST/.autonomous-team/hook-events/probe.jsonl" 2>/dev/null
assert_rc "directory stays writable so runs can drop untracked files" 0 $?

echo "=== a clean tree stays silent (items 5, 8) ==="
OUT="$(verify_tree_assert "$DEST" "$SHA" 2>&1)"
assert_rc "clean tree asserts 0" 0 $?
assert_contains "clean assert reports the file count" "tracked files unchanged" "$OUT"

# Assert the appends SUCCEED before asserting the tree stays clean. Carve-outs
# are not hashed, so if build ever chmod'd one read-only the write would fail
# silently and the assert would still return 0 — the test would pass for the
# wrong reason and could never fail for the reason it exists.
assert_ok "carved-out file stays writable" test -w "$DEST/.autonomous-team/config.json"
printf 'x' >> "$DEST/.autonomous-team/config.json"
assert_rc "appending to a carved-out file succeeds" 0 $?
printf 'x' >> "$DEST/.autonomous-team/agent-profiles.json"
assert_rc "appending to the second carved-out file succeeds" 0 $?
verify_tree_assert "$DEST" "$SHA" > /dev/null 2>&1
assert_rc "runtime-mutated carve-outs do not trip the assert" 0 $?

echo '{"e":1}' > "$DEST/.autonomous-team/hook-events/run-1.jsonl"
mkdir -p "$DEST/backend/__pycache__" && touch "$DEST/backend/__pycache__/x.pyc"
verify_tree_assert "$DEST" "$SHA" > /dev/null 2>&1
assert_rc "untracked runtime droppings do not trip the assert" 0 $?

echo "=== a changed tree is loud and names the path (item 6) ==="
chmod u+w "$DEST/hooks/sandbox.py"
git -C "$PARENT" show "$SHA:CLAUDE.md" > "$DEST/hooks/sandbox.py"
OUT="$(verify_tree_assert "$DEST" "$SHA" 2>&1)"
assert_nonzero "clobbered tree asserts non-zero" $?
assert_contains "clobber output names the changed path" "CHANGED  hooks/sandbox.py" "$OUT"

rm -f "$DEST/hooks/sandbox.py"
OUT="$(verify_tree_assert "$DEST" "$SHA" 2>&1)"
assert_nonzero "deleted tracked file asserts non-zero" $?
assert_contains "deletion output names the missing path" "MISSING  hooks/sandbox.py" "$OUT"

git -C "$PARENT" show "$SHA:hooks/sandbox.py" > "$DEST/hooks/sandbox.py"
chmod a-w "$DEST/hooks/sandbox.py"
verify_tree_assert "$DEST" "$SHA" > /dev/null 2>&1
assert_rc "restoring exact content clears the failure" 0 $?

echo "=== assert from INSIDE the tree must stay silent on a clean tree ==="
# The scan's own forks inherit the CALLER's cwd. Piping matters independently:
# a pipeline sibling of the caller is another process rooted in the tree.
( cd "$DEST" && verify_tree_assert "$DEST" "$SHA" > /dev/null 2>&1 )
assert_rc "clean assert from inside the tree, unpiped" 0 $?
( cd "$DEST" && { verify_tree_assert "$DEST" "$SHA" 2>&1 | tee /dev/null > /dev/null; exit "${PIPESTATUS[0]}"; } )
assert_rc "clean assert from inside the tree, piped" 0 $?

echo "=== live FOREIGN process rooted in the tree (item 10) ==="
# setsid --fork, not a plain background job: a child of this shell is by design
# not flagged, and a reparented process is what a real leftover looks like.
PIDF="$WORK/sleeper.pid"
setsid --fork bash -c "cd '$DEST' && echo \$\$ > '$PIDF' && exec sleep 25" &
n=0; while [ ! -s "$PIDF" ] && [ "$n" -lt 100 ]; do sleep 0.05; n=$((n + 1)); done
SLEEPER="$(cat "$PIDF" 2>/dev/null)"
OUT="$(verify_tree_assert "$DEST" "$SHA" 2>&1)"
assert_nonzero "foreign process rooted in the tree asserts non-zero" $?
assert_contains "live-process output names the pid" "$SLEEPER" "$OUT"
kill "$SLEEPER" 2>/dev/null
n=0; while [ -e "/proc/$SLEEPER" ] && [ "$n" -lt 100 ]; do sleep 0.05; n=$((n + 1)); done
verify_tree_assert "$DEST" "$SHA" > /dev/null 2>&1
assert_rc "assert clears once the process is gone" 0 $?

verify_tree_assert "$WORK/no-such-tree" "$SHA" > /dev/null 2>&1
assert_rc "unknown tree exits 3" 3 $?

verify_tree_unprotect "$DEST" "$SHA"
assert_rc "unprotect exits 0" 0 $?
assert_ok "unprotect restores write permission" test -w "$DEST/CLAUDE.md"

echo "=== verify_tree_assert_log (item 9) ==="
L="$WORK/logs"; mkdir -p "$L"
printf '....\n=========== 1 failed, 2 passed in 0.12s ============\n' > "$L/one-decorated.log"
verify_tree_assert_log "$L/one-decorated.log" > /dev/null 2>&1
assert_rc "one decorated summary line passes" 0 $?

printf '....\n37 failed, 3666 passed, 12 skipped in 412.34s\n' > "$L/one-quiet.log"
verify_tree_assert_log "$L/one-quiet.log" > /dev/null 2>&1
assert_rc "one -q summary line passes" 0 $?

printf '=== no tests ran in 0.01s ===\n' > "$L/notests.log"
verify_tree_assert_log "$L/notests.log" > /dev/null 2>&1
assert_rc "'no tests ran' counts as one summary line" 0 $?

printf '3 passed in 0.10s\n....\n4 failed, 1 passed in 0.20s\n' > "$L/two.log"
OUT="$(verify_tree_assert_log "$L/two.log" 2>&1)"
assert_nonzero "two summary lines fail" $?
assert_contains "two-summary output says the runs overlapped" "2 pytest summary lines" "$OUT"

printf 'collecting ...\ninterrupted\n' > "$L/none.log"
verify_tree_assert_log "$L/none.log" > /dev/null 2>&1
assert_nonzero "no summary line (truncated run) fails" $?

# Only coverage of the usage / missing-file branch.
verify_tree_assert_log "$L/does-not-exist.log" > /dev/null 2>&1
assert_rc "absent log file exits 3" 3 $?
verify_tree_assert_log > /dev/null 2>&1
assert_rc "no argument exits 3" 3 $?

assert_not "helper contains no filesystem-sweep word" grep -qE '\bfind\b' "$LIB"
assert_ok "manifest derives from git ls-tree" grep -q 'ls-tree -r -z' "$LIB"

echo
echo "PASS: $PASS  FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
