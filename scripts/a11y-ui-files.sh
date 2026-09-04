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

REPO="$(_resolve_repo)"

DIFF_FILES=$(gh pr diff "$PR" --repo "$REPO" --name-only 2>/dev/null || true)

if echo "$DIFF_FILES" | grep -qE '(^|/)dashboard/src/.*\.tsx$|(^|/)tui/.*\.tsx$|\.html$'; then
  exit 0
else
  exit 1
fi
