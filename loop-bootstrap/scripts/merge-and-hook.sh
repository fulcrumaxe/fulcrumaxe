#!/usr/bin/env bash
# loop-bootstrap/scripts/merge-and-hook.sh — Team Lead merge wrapper.
#
# Project-agnostic version. Reads repo from .autonomous-team/project.json.
# Installs to <target>/scripts/merge-and-hook.sh by bootstrap.sh.
#
# Usage:
#   bash scripts/merge-and-hook.sh --pr <PR_NUMBER> [--discussion <DISC_NUMBER>]
#
# 1. Merges the PR via squash (deletes branch).
# 2. Runs post-merge-hook.sh with the same PR/discussion args.
# 3. Tees hook output to .autonomous-team/dashboard-logs/manual-merge-<PR>.log.
#
# NEVER call gh pr merge directly for manual merges — this wrapper ensures
# post-merge bookkeeping (audit, team-log, Discussion close) always runs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Argument parsing ──────────────────────────────────────────────────────────
PR=""
DISC=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)         PR="$2";   shift 2 ;;
    --discussion) DISC="$2"; shift 2 ;;
    *)
      echo "[merge-and-hook] unknown argument: $1" >&2
      echo "Usage: $0 --pr <PR_NUMBER> [--discussion <DISC_NUMBER>]" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$PR" ]]; then
  echo "[merge-and-hook] --pr is required" >&2
  exit 1
fi

# ── Load repo slug from project.json ─────────────────────────────────────────
PROJECT_JSON="$REPO_ROOT/.autonomous-team/project.json"
REPO_SLUG=""
if [[ -f "$PROJECT_JSON" ]]; then
  REPO_SLUG=$(python3 -c "import json; d=json.load(open('$PROJECT_JSON')); print(d.get('repo',''))" 2>/dev/null || echo "")
fi
if [[ -z "$REPO_SLUG" ]]; then
  echo "[merge-and-hook] ERROR: repo not set in .autonomous-team/project.json" >&2
  exit 1
fi

LOG_DIR="$REPO_ROOT/.autonomous-team/dashboard-logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/manual-merge-${PR}.log"

# ── Step 1: Merge ─────────────────────────────────────────────────────────────
echo "[merge-and-hook] merging PR #$PR (squash, delete-branch) in $REPO_SLUG..."
gh pr merge --squash --delete-branch "$PR" --repo "$REPO_SLUG"
echo "[merge-and-hook] PR #$PR merged."

# ── Step 2: Post-merge hook ───────────────────────────────────────────────────
HOOK_ARGS=(--pr "$PR")
if [[ -n "$DISC" ]]; then
  HOOK_ARGS+=(--discussion "$DISC")
fi

echo "[merge-and-hook] running post-merge-hook.sh for PR #$PR (log: $LOG_FILE)"
set +e
bash "$SCRIPT_DIR/post-merge-hook.sh" "${HOOK_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
HOOK_EXIT="${PIPESTATUS[0]}"
set -e

if [[ $HOOK_EXIT -ne 0 ]]; then
  echo "[merge-and-hook] WARNING: post-merge-hook.sh exited $HOOK_EXIT — see $LOG_FILE" >&2
fi

exit "$HOOK_EXIT"
