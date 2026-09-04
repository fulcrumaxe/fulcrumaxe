#!/usr/bin/env bash
# scripts/agent-feed-rotate.sh — daily rotation wrapper for agent-feed.jsonl
#
# Called from /loop step 7.5 (single scheduler invariant — do not call from elsewhere).
#
# Behavior:
#   1. Splits events older than today (UTC) into per-date .jsonl.gz files.
#   2. Archives .jsonl.gz files older than 30 days to archive/agent-feed/.
#   3. Logs rotation result to stderr (non-fatal on failure).
#
# Exit codes:
#   0 — rotation ran (even if nothing to rotate)
#   1 — rotation failed due to Python import error

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[agent-feed-rotate] Starting rotation at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

result=$(python3 -c "
import sys, json, os
sys.path.insert(0, '$REPO_ROOT')
os.chdir('$REPO_ROOT')
from backend.agent_feed import rotate
result = rotate()
print(json.dumps(result, indent=2))
" 2>&1)
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
  echo "[agent-feed-rotate] Error: rotation failed:" >&2
  echo "$result" >&2
  exit 1
fi

echo "[agent-feed-rotate] Result: $result"

# Log summary to agent-feed itself (meta-event)
ROTATED=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d.get('rotated_dates',[])) or 'none')" 2>/dev/null || echo "unknown")
ARCHIVED=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(len(d.get('archived_files',[]))))" 2>/dev/null || echo "0")

bash "$SCRIPT_DIR/agent-feed-append.sh" \
  --role "agent-feed-rotate" \
  --event-type "log" \
  --message "Rotation complete: dates=$ROTATED archived=${ARCHIVED}files" \
  2>/dev/null || true

echo "[agent-feed-rotate] Done."
