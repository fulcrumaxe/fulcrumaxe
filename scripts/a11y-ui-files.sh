#!/usr/bin/env bash
# a11y-ui-files.sh — predicate: exits 0 if a PR diff touches UI files, 1 otherwise.
# Usage: bash scripts/a11y-ui-files.sh <pr_number>
# UI files: dashboard/src/**.tsx, tui/**.tsx, *.html

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/repo-resolve.sh"

PR="${1:-}"
if [ -z "$PR" ]; then
  echo "Usage: $0 <pr_number>" >&2
  exit 2
fi

# The code plane — this reads a PR diff, which lives with the code.
#
# An unresolved plane must stop before `gh` runs: `gh pr diff --repo ""` exits 0
# against whatever the checkout's origin remote points at, so it would answer
# this predicate from the wrong repo instead of failing. It exits 0 ("assume
# UI files touched") rather than 1, because every caller collapses non-zero to "no" and
# would otherwise silently skip the accessibility review.
REPO="$(_resolve_code_repo 2>/dev/null || true)"
if [ -z "${REPO}" ]; then
  echo "[a11y-ui-files] ERROR: could not resolve the code repo — reporting \"UI files touched\" so the accessibility review is not silently skipped. Add a \"code_repo\" (or \"repo\") field to .autonomous-team/config.json, or set AUTONOMOUS_TEAM_REPO." >&2
  exit 0
fi

DIFF_FILES=$(gh pr diff "$PR" --repo "$REPO" --name-only 2>/dev/null || true)

if echo "$DIFF_FILES" | grep -qE '(^|/)dashboard/src/.*\.tsx$|(^|/)tui/.*\.tsx$|\.html$'; then
  exit 0
else
  exit 1
fi
