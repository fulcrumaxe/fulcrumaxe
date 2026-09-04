#!/usr/bin/env bash
# scripts/replay-hook-event.sh — manual recovery tool for partially-completed hook events.
#
# Usage:
#   scripts/replay-hook-event.sh <event_id>
#
# Reads the marker at .autonomous-team/hook-events/{event_id}.json,
# identifies incomplete steps, and re-invokes the originating hook with
# --event-id <event_id> --resume.
#
# Exit codes:
#   0 — replay succeeded (all steps now completed)
#   1 — marker missing or corrupt
#   2 — hook script not found
#   3 — re-invocation failed

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <event_id>" >&2
  exit 1
fi

EVENT_ID="$1"
# Respect an externally-set HOOK_EVENT_DIR the same way
# scripts/lib/hook-event.sh's hook_event_init does ("respect externally-set
# HOOK_EVENT_DIR (e.g. in tests)") — this script reads and resumes the exact
# markers that library writes, so a caller that redirected one must be able
# to redirect the other, or a test pointed at an isolated fixture would
# still replay against the live tree (D#2267).
HOOK_EVENTS_DIR="${HOOK_EVENT_DIR:-$REPO_ROOT/.autonomous-team/hook-events}"
MARKER="$HOOK_EVENTS_DIR/${EVENT_ID}.json"
DONE_MARKER="$HOOK_EVENTS_DIR/done/${EVENT_ID}.json"

# ── 1. Locate marker ──────────────────────────────────────────────────────────

if [[ -f "$DONE_MARKER" ]]; then
  echo "[replay] Event $EVENT_ID is already complete (marker in done/)."
  exit 0
fi

if [[ ! -f "$MARKER" ]]; then
  echo "[replay] ERROR: marker not found for event_id=$EVENT_ID" >&2
  echo "  Checked: $MARKER" >&2
  echo "  and:     $DONE_MARKER" >&2
  exit 1
fi

# ── 2. Parse marker ───────────────────────────────────────────────────────────

MARKER_JSON=$(cat "$MARKER")

HOOK_NAME=$(echo "$MARKER_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('hook',''))
" 2>/dev/null || echo "")

if [[ -z "$HOOK_NAME" ]]; then
  echo "[replay] ERROR: marker is corrupt — missing 'hook' field" >&2
  echo "  Marker path: $MARKER" >&2
  exit 1
fi

STEPS_TOTAL=$(echo "$MARKER_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(','.join(d.get('steps_total',[])))
" 2>/dev/null || echo "")

STEPS_DONE=$(echo "$MARKER_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(','.join(d.get('steps_completed',[])))
" 2>/dev/null || echo "")

INPUTS=$(echo "$MARKER_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
inp=d.get('inputs',{})
# Print as shell args: --key value pairs
for k,v in inp.items():
  print(f'--{k} {v}')
" 2>/dev/null || echo "")

echo "[replay] Event: $EVENT_ID"
echo "[replay] Hook:  $HOOK_NAME"
echo "[replay] Steps total:     $STEPS_TOTAL"
echo "[replay] Steps completed: $STEPS_DONE"

# ── 3. Identify incomplete steps ──────────────────────────────────────────────

INCOMPLETE=$(python3 -c "
import sys
total=[s.strip() for s in sys.argv[1].split(',') if s.strip()]
done=set(s.strip() for s in sys.argv[2].split(',') if s.strip())
remaining=[s for s in total if s not in done]
print(' '.join(remaining))
" "$STEPS_TOTAL" "$STEPS_DONE")

if [[ -z "$INCOMPLETE" ]]; then
  echo "[replay] No incomplete steps found — marking as done."
  mkdir -p "$HOOK_EVENTS_DIR/done"
  mv "$MARKER" "$DONE_MARKER" 2>/dev/null || true
  exit 0
fi

echo "[replay] Incomplete steps: $INCOMPLETE"

# ── 4. Find hook script ───────────────────────────────────────────────────────

HOOK_SCRIPT=""
case "$HOOK_NAME" in
  post-agent-hook)   HOOK_SCRIPT="$SCRIPT_DIR/post-agent-hook.sh" ;;
  post-merge-hook)   HOOK_SCRIPT="$SCRIPT_DIR/post-merge-hook.sh" ;;
  pre-spawn-check)   HOOK_SCRIPT="$SCRIPT_DIR/pre-spawn-check.sh" ;;
  *)
    echo "[replay] ERROR: unknown hook '$HOOK_NAME' — cannot locate script" >&2
    exit 2
    ;;
esac

if [[ ! -f "$HOOK_SCRIPT" ]]; then
  echo "[replay] ERROR: hook script not found: $HOOK_SCRIPT" >&2
  exit 2
fi

# ── 5. Build replay command ───────────────────────────────────────────────────

# Convert inputs JSON to CLI args
EXTRA_ARGS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && EXTRA_ARGS+=($line)
done <<< "$INPUTS"

echo "[replay] Re-invoking: bash $HOOK_SCRIPT --event-id $EVENT_ID --resume ${EXTRA_ARGS[*]:-}"

# ── 6. Re-invoke ──────────────────────────────────────────────────────────────

if bash "$HOOK_SCRIPT" --event-id "$EVENT_ID" --resume "${EXTRA_ARGS[@]:-}"; then
  echo "[replay] Replay succeeded for event $EVENT_ID."
  exit 0
else
  echo "[replay] ERROR: hook re-invocation failed for event $EVENT_ID." >&2
  exit 3
fi
