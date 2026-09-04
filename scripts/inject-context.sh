#!/usr/bin/env bash
# inject-context.sh — emit project context block for agent prompts.
#
# Calls `python backend/context_manager.py prompt`, caches the result for 5
# minutes in /tmp/af-project-context.txt, and prints the block to stdout.
# Exits 0 even when context_manager.py fails — context injection is best-effort.
#
# Usage: CONTEXT=$(bash scripts/inject-context.sh)

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/platform-compat.sh
source "$REPO_DIR/scripts/lib/platform-compat.sh" || exit 1

CACHE=/tmp/af-project-context.txt
TTL=300  # 5 minutes

# Serve from cache if fresh. If the cache's mtime can't be read at all,
# don't guess — just skip straight to recomputing below (D#2263: a silent
# fallback to epoch 0 here used to make the cache look infinitely old,
# which happened to be harmless for this specific script, but only by
# accident — it's still an invisible failure worth not repeating).
if [ -f "$CACHE" ]; then
  if CACHE_MTIME=$(pc_stat_mtime "$CACHE" 2>/dev/null); then
    age=$(( $(date +%s) - CACHE_MTIME ))
    if [ "$age" -lt "$TTL" ]; then
      cat "$CACHE"
      exit 0
    fi
  fi
fi

context=$(python3 "$REPO_DIR/backend/context_manager.py" prompt 2>/dev/null || true)

if [ -n "$context" ]; then
  printf '%s' "$context" > "$CACHE"
  printf '%s' "$context"
fi
# If context is empty, print nothing and exit 0 (best-effort)
exit 0
