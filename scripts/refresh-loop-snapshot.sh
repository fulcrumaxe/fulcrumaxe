#!/usr/bin/env bash
# scripts/refresh-loop-snapshot.sh — regenerate the loop snapshot at the canonical path.
#
# This is the *only* thing that keeps the snapshot under MAX_AGE=600s. It runs
# every 5 minutes from loop-snapshot-refresh.timer (see scripts/install-snapshot-timer.sh),
# and once more from scripts/start-the-day.sh so the first read of the day is warm.
#
# Three properties this script exists to guarantee:
#
#   1. It writes where the readers read. The producer's output used to go to a
#      PID-suffixed /tmp file that nobody opened; the path now comes from
#      backend/snapshot_path.py, the one definition every reader imports.
#
#   2. It does not consume the event feed. --no-drain makes the producer peek
#      agent-feed.jsonl without advancing loop-snapshot-cursor.json, so a
#      5-minute refresh cannot eat events the next loop iteration needs to see.
#
#   3. The write is atomic. The producer writes a sibling .tmp and this script
#      mv's it into place, so a reader parsing the file concurrently sees either
#      the old snapshot or the new one, never a half-written one.
#
# It spawns no agent and initiates no Discussion-level work — it regenerates a
# read-only status blob and nothing else.
#
# Environment:
#   SNAPSHOT_PATH=...  — override the destination (honoured by snapshot_path.py)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Put the project venv first on PATH so bare `python3` calls below resolve to
# the interpreter with the project's dependencies (PyYAML etc.) installed,
# regardless of what python3 this process inherited on PATH. No-op when
# .venv/ doesn't exist yet.
if [ -d "$REPO_ROOT/.venv/bin" ]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi

DEST="$(python3 "$REPO_ROOT/backend/snapshot_path.py")"
if [ -z "$DEST" ]; then
  echo "refresh-loop-snapshot: could not resolve the snapshot path" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"

# Sibling temp file — same filesystem as DEST, so the mv below is a rename and
# therefore atomic. A temp file in /tmp would make it a copy, which is not.
TMP="${DEST}.tmp.$$"
trap 'rm -f "$TMP"' EXIT

python3 "$REPO_ROOT/scripts/loop-subsystem-snapshot.py" --no-drain --output "$TMP"

mv -f "$TMP" "$DEST"
trap - EXIT

echo "refresh-loop-snapshot: wrote $DEST"
