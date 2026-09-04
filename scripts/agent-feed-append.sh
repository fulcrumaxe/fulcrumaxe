#!/usr/bin/env bash
# scripts/agent-feed-append.sh — shell wrapper to append one event to agent-feed.jsonl
#
# Usage (flags mode):
#   scripts/agent-feed-append.sh \
#     --role <role> --event-type <type> --message <msg> \
#     [--discussion N] [--pr N] [--verdict V] \
#     [--input-tokens N] [--output-tokens N] \
#     [--files "file1,file2"] [--model <model>] \
#     [--details '{"key":"val"}'] [--reason <reason>]
#
# Usage (stdin mode):
#   echo '{"event_type":"log","role":"executor","message":"hi"}' | scripts/agent-feed-append.sh
#
# Exit codes:
#   0 — appended successfully (or non-fatal write failure)
#   1 — disk full or file unwritable (real error — caller should treat as fatal)
#
# Write safety: uses flock on the JSONL file.
# Non-fatal errors (e.g. Python missing, validation error) are logged to stderr.
# Fatal errors (disk full, unwritable) exit 1.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FEED_PATH="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"

# ── Parse flags ───────────────────────────────────────────────────────────────
ROLE=""
EVENT_TYPE=""
MESSAGE=""
DISCUSSION=""
PR_NUM=""
VERDICT=""
INPUT_TOKENS=""
OUTPUT_TOKENS=""
FILES=""
MODEL=""
DETAILS=""
REASON=""
USE_STDIN=false

# If stdin has data and no args, treat as JSON passthrough
if [[ $# -eq 0 ]] && ! [ -t 0 ]; then
  USE_STDIN=true
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)          ROLE="$2";          shift 2 ;;
    --event-type)    EVENT_TYPE="$2";    shift 2 ;;
    --message)       MESSAGE="$2";       shift 2 ;;
    --discussion)    DISCUSSION="$2";    shift 2 ;;
    --pr)            PR_NUM="$2";        shift 2 ;;
    --verdict)       VERDICT="$2";       shift 2 ;;
    --input-tokens)  INPUT_TOKENS="$2";  shift 2 ;;
    --output-tokens) OUTPUT_TOKENS="$2"; shift 2 ;;
    --files)         FILES="$2";         shift 2 ;;
    --model)         MODEL="$2";         shift 2 ;;
    --details)       DETAILS="$2";       shift 2 ;;
    --reason)        REASON="$2";        shift 2 ;;
    *)
      echo "[agent-feed-append] Unknown argument: $1" >&2
      exit 0  # Non-fatal — don't crash callers
      ;;
  esac
done

# Ensure .autonomous-team dir exists
mkdir -p "$(dirname "$FEED_PATH")"

# ── Build event JSON ──────────────────────────────────────────────────────────
if [[ "$USE_STDIN" == "true" ]]; then
  # Stdin mode: pass JSON directly to Python for validation + append
  EVENT_JSON=$(cat)
else
  # Flags mode: build JSON from args using positional sys.argv (avoids env var quoting issues)
  # Note: sys.argv[0]='-c', sys.argv[1]='--', so args start at index 2
  EVENT_JSON=$(python3 -c "
import json, sys

role         = sys.argv[2]
event_type   = sys.argv[3] or 'log'
message      = sys.argv[4]
discussion   = sys.argv[5]
pr_num       = sys.argv[6]
verdict      = sys.argv[7]
input_tokens = sys.argv[8]
output_tokens= sys.argv[9]
files_raw    = sys.argv[10]
model        = sys.argv[11]
details_raw  = sys.argv[12] if len(sys.argv) > 12 else ''
reason       = sys.argv[13] if len(sys.argv) > 13 else ''

event = {
    'event_type': event_type or 'log',
    'role': role,
    'message': message[:280] if message else '',
}
if discussion:
    try: event['discussion'] = int(discussion)
    except: pass
if pr_num:
    try: event['pr'] = int(pr_num)
    except: pass
if verdict:
    event['verdict'] = verdict
if input_tokens or output_tokens:
    try:
        event['tokens'] = {
            'input':  int(input_tokens  or 0),
            'output': int(output_tokens or 0),
        }
    except: pass
if files_raw:
    event['files'] = [f.strip() for f in files_raw.split(',') if f.strip()]
if model:
    event['model'] = model
if details_raw:
    try:
        event['details'] = json.loads(details_raw)
    except Exception:
        pass  # non-fatal: omit details if not valid JSON
if reason:
    event['reason'] = reason

print(json.dumps(event))
" -- "$ROLE" "$EVENT_TYPE" "$MESSAGE" \
     "$DISCUSSION" "$PR_NUM" "$VERDICT" \
     "$INPUT_TOKENS" "$OUTPUT_TOKENS" \
     "$FILES" "$MODEL" "$DETAILS" "$REASON" 2>/dev/null)

  if [[ -z "$EVENT_JSON" ]]; then
    echo "[agent-feed-append] Warning: failed to build event JSON (non-fatal)" >&2
    exit 0
  fi
fi

# ── Append via Python module (handles validation + flock) ────────────────────
# Stderr capture uses a fresh mktemp file, not a fixed /tmp name — a fixed
# name here is a shared-writer collision waiting to happen the moment two
# invocations (two concurrent callers, or two copies of a test suite that
# exercises this script) land in the same error branch at once (D#2254).
APPEND_EXIT=0
_APPEND_ERR_FILE="$(mktemp /tmp/agent-feed-append-err.XXXXXX)"
python3 -c "
import sys, json, os
sys.path.insert(0, os.path.join(os.path.dirname('$SCRIPT_DIR'), ''))
# Resolve REPO_ROOT for path references in agent_feed
os.chdir('$REPO_ROOT')
sys.path.insert(0, '$REPO_ROOT')
from backend.agent_feed import append
data = json.loads(sys.stdin.read())
append(data)
" <<< "$EVENT_JSON" 2>"$_APPEND_ERR_FILE"
APPEND_EXIT=$?

if [[ $APPEND_EXIT -ne 0 ]]; then
  ERR=$(cat "$_APPEND_ERR_FILE" 2>/dev/null | head -3 || echo "unknown error")
  rm -f "$_APPEND_ERR_FILE"
  # Distinguish disk-full / unwritable (exit 1) from other errors (exit 0)
  if echo "$ERR" | grep -qiE "no space left|read-only file system|permission denied|OSError"; then
    echo "[agent-feed-append] FATAL: disk/permission error writing feed: $ERR" >&2
    exit 1
  fi
  # Non-fatal: validation error, Python import error, etc.
  echo "[agent-feed-append] Warning: append failed (non-fatal): $ERR" >&2
  exit 0
fi

rm -f "$_APPEND_ERR_FILE"
exit 0
