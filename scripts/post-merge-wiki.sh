#!/usr/bin/env bash
# post-merge-wiki.sh — regenerate and push wiki pages after a PR merge.
# Called from CLAUDE.md auto-merge step 6. Never blocks the merge flow.
set -uo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd .. && pwd)

# Check wiki_sync gate
GATE=$(python3 "$REPO_DIR/backend/control_plane.py" get gates.wiki_sync 2>/dev/null || echo "true")
if [ "$GATE" = "false" ]; then
  echo "[wiki-sync] gate disabled — skipping" >&2
  exit 0
fi

echo "[wiki-sync] refreshing registry..." >&2
python3 "$REPO_DIR/backend/registry.py" sync 2>&1 || true

# Status page and changelog generation moved into sync-wiki.sh (D#1908) —
# they now write into the temp wiki clone that script creates, not into
# this checkout's wiki/ dir. Generating here first would dirty the tracked
# tree the auto_pull step checks right after this hook runs.
echo "[wiki-sync] pushing to wiki repo..." >&2
bash "$REPO_DIR/scripts/sync-wiki.sh" 2>&1 || true

echo "[wiki-sync] done" >&2
