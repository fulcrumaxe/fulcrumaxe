#!/usr/bin/env bash
# check-pr-dashboard-touched.sh <PR_NUMBER>
#
# Exit 0 if the PR touches any file under dashboard/; exit 1 otherwise.
# Used by Team Lead and workflow_runner to gate browser-tester spawns.
#
# Usage:
#   bash scripts/check-pr-dashboard-touched.sh 123  && echo "dashboard touched"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# The code plane — this reads a PR diff, which lives with the code.
#
# An unresolved plane must stop before `gh` runs: `gh pr diff --repo ""` exits 0
# against whatever the checkout's origin remote points at, so it would answer
# this predicate from the wrong repo instead of failing. It exits 0 ("assume
# dashboard touched") rather than 1, because every caller collapses non-zero to "no" and
# would otherwise silently skip the browser test.
_REPO="$(_resolve_code_repo 2>/dev/null || true)"
if [ -z "${_REPO}" ]; then
  echo "[check-pr-dashboard-touched] ERROR: could not resolve the code repo — reporting \"dashboard touched\" so the browser test is not silently skipped. Add a \"code_repo\" (or \"repo\") field to .autonomous-team/config.json, or set AUTONOMOUS_TEAM_REPO." >&2
  exit 0
fi

PR="${1:?Usage: check-pr-dashboard-touched.sh <PR_NUMBER>}"

gh pr diff --name-only "$PR" --repo "$_REPO" \
  | grep -q '^dashboard/' && exit 0 || exit 1
