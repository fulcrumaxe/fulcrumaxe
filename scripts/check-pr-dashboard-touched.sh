#!/usr/bin/env bash
# check-pr-dashboard-touched.sh <PR_NUMBER> [REPO]
#
# Exit 0 if the PR touches any file under dashboard/; exit 1 otherwise.
# Used by Team Lead and workflow_runner to gate browser-tester spawns.
#
# [REPO] is optional (D#2563): a PR number is not always on the code plane, so
# a caller that has already resolved which plane #PR lives on (see
# scripts/lib/pr-plane.sh) passes that slug explicitly. Omitted — the
# pre-D#2563 shape, still what loop-phased-step5.sh's _dashboard_touched
# wrapper calls — falls back to _resolve_code_repo exactly as before, so
# that caller (explicitly out of scope for D#2563) is unaffected.
#
# Usage:
#   bash scripts/check-pr-dashboard-touched.sh 123          && echo "dashboard touched"
#   bash scripts/check-pr-dashboard-touched.sh 123 owner/repo && echo "dashboard touched"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"

PR="${1:?Usage: check-pr-dashboard-touched.sh <PR_NUMBER> [REPO]}"
_REPO="${2:-}"

# An unresolved plane must stop before `gh` runs: `gh pr diff --repo ""` exits 0
# against whatever the checkout's origin remote points at, so it would answer
# this predicate from the wrong repo instead of failing. It exits 0 ("assume
# dashboard touched") rather than 1, because every caller collapses non-zero to "no" and
# would otherwise silently skip the browser test.
if [ -z "$_REPO" ]; then
  _REPO="$(_resolve_code_repo 2>/dev/null || true)"
fi
if [ -z "${_REPO}" ]; then
  echo "[check-pr-dashboard-touched] ERROR: could not resolve a repo for PR #${PR} — reporting \"dashboard touched\" so the browser test is not silently skipped. Add a \"code_repo\" (or \"repo\") field to .autonomous-team/config.json, or set AUTONOMOUS_TEAM_REPO, or pass the resolved plane's repo explicitly." >&2
  exit 0
fi

gh pr diff --name-only "$PR" --repo "$_REPO" \
  | grep -q '^dashboard/' && exit 0 || exit 1
