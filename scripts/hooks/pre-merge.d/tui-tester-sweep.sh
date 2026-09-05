#!/usr/bin/env bash
# scripts/hooks/pre-merge.d/tui-tester-sweep.sh
#
# Pre-merge hook step: run the tui anti-pattern sweep when a PR touches
# dashboard_tui/** and block on any error-severity findings.
#
# Unlike the post-merge counterpart (which is always non-fatal), this script
# exits non-zero on errors — the caller (run-pr-tests.sh / preflight) should
# treat exit 1 as a merge block.
#
# Called with:
#   bash scripts/hooks/pre-merge.d/tui-tester-sweep.sh --pr <N>
#
# Exit codes:
#   0  — skipped (PR does not touch dashboard_tui) OR sweep passed
#   1  — error-severity findings found — block merge
#   2  — sweep failed to run

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$REPO_ROOT/scripts/lib/repo-resolve.sh"
# The PR diff is a code-plane read. Exit 2 ("sweep failed to run") rather
# than falling through: `gh pr diff --repo ""` exits 0 against whatever the
# checkout's origin remote points at, so an empty slug would return a diff
# for the wrong PR and be scanned as if it were this one.
CODE_REPO="$(_resolve_code_repo 2>/dev/null || true)"
if [ -z "$CODE_REPO" ]; then
  echo "[tui-tester-pre-merge] ERROR: could not resolve the code repo — cannot read the PR diff. Add a \"code_repo\" (or \"repo\") field to .autonomous-team/config.json, or set AUTONOMOUS_TEAM_REPO." >&2
  exit 2
fi
PR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr) PR="$2"; shift 2 ;;
    *) shift ;;
  esac
done

if [[ -z "$PR" ]]; then
  echo "[tui-tester-pre-merge] --pr is required — skipping" >&2
  exit 0
fi

# ── 1. Check if this PR touches dashboard_tui/** ──────────────────────────────
CHANGED_TUI=$(gh pr diff --name-only "$PR" --repo "$CODE_REPO" \
  2>/dev/null | grep '^dashboard_tui/' || true)

if [[ -z "$CHANGED_TUI" ]]; then
  echo "[tui-tester-pre-merge] PR #$PR does not touch dashboard_tui — skipping"
  exit 0
fi

echo "[tui-tester-pre-merge] PR #$PR touches dashboard_tui — running pre-merge sweep"

# ── 2. Delegate to the standalone check script ────────────────────────────────
bash "$REPO_ROOT/scripts/check-tui-anti-patterns.sh"
