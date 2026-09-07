#!/usr/bin/env bash
# scripts/ci/ts-backend-test-integrity.sh — fail loud if `bun run test`
# mutates the tracked tree it is measuring (D#2342).
#
# The ts-backend suite used to run a step (branch-contamination recovery)
# unconditionally during tests, and that step performed a real fetch and
# hard-reset against the tree the process was running in. A full
# `bun run test` on a contaminated tree quietly reverted three tracked
# files to a different version mid-run and still printed a clean pass
# count -- the aggregate described a tree that no longer existed by the
# time it was printed.
#
# That specific writer is now gated off (D#2342 PR-a, see
# ts-backend/src/spawn/post-agent-hook.ts). This script is the recurrence
# guard: it hashes every tracked file before and after the suite runs, and
# treats ANY change to the tracked tree as a failure -- regardless of what
# the suite itself reported -- so a future instance of this bug shape (a
# different step, a different file) fails the build instead of passing
# quietly.
#
# Usage: scripts/ci/ts-backend-test-integrity.sh [REPO_ROOT]
#   REPO_ROOT defaults to this repo (resolved from the script's own path).
#   An explicit REPO_ROOT lets the same script be pointed at a scratch
#   checkout for verification without touching the real tree.
set -uo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT" || { echo "ts-backend-test-integrity: cannot cd to '$REPO_ROOT'" >&2; exit 1; }

if [ ! -d "$REPO_ROOT/ts-backend" ]; then
  echo "ts-backend-test-integrity: no ts-backend/ under '$REPO_ROOT'" >&2
  exit 1
fi

BEFORE="$(mktemp)"
AFTER="$(mktemp)"
trap 'rm -f "$BEFORE" "$AFTER"' EXIT

# git ls-files is the subject set (D#2342 item 8) -- the whole tracked
# tree, not the four files the original finding happened to name. The
# original bug touched files outside ts-backend/ entirely.
hash_tree() {
  git -C "$REPO_ROOT" ls-files -z | sort -z | xargs -0 sha256sum
}

hash_tree > "$BEFORE"

set +e
( cd "$REPO_ROOT/ts-backend" && bun run test )
TEST_RC=$?
set -e

hash_tree > "$AFTER"

STATUS=0

if ! diff -q "$BEFORE" "$AFTER" > /dev/null 2>&1; then
  echo "" >&2
  echo "ts-backend-test-integrity: FAIL -- the test run modified the tracked tree (D#2342)." >&2
  echo "ts-backend-test-integrity: changed files:" >&2
  diff "$BEFORE" "$AFTER" | grep -E '^[<>]' | awk '{print $NF}' | sort -u | while read -r f; do
    echo "  $f" >&2
  done
  STATUS=1
fi

if [ "$TEST_RC" -ne 0 ]; then
  echo "ts-backend-test-integrity: bun run test exited $TEST_RC" >&2
  STATUS=1
fi

if [ "$STATUS" -eq 0 ]; then
  echo "ts-backend-test-integrity: PASS -- tracked tree unchanged, bun run test exited 0"
fi

exit "$STATUS"
