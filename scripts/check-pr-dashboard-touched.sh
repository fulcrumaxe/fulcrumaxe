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
_REPO="$(_resolve_repo)"

PR="${1:?Usage: check-pr-dashboard-touched.sh <PR_NUMBER>}"

gh pr diff --name-only "$PR" --repo "$_REPO" \
  | grep -q '^dashboard/' && exit 0 || exit 1
