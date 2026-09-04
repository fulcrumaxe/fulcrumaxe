#!/usr/bin/env bash
# record-agent-result.sh — record a lesson and budget spend after agent completion.
#
# Usage:
#   scripts/record-agent-result.sh \
#     --discussion 14 \
#     --role executor \
#     --verdict done \
#     --input-tokens 12000 \
#     --output-tokens 1800 \
#     [--model "claude-sonnet-4-20250514"]
#     [--content "TypeScript type error in src/foo.ts line 42"] \
#     [--files "src/foo.ts,src/bar.ts"] \
#     [--tags "type-error,import-error"]
#
# Records:
#   1. Agent memory lesson (backend/agent_memory.py record)
#   2. Budget spend entry  (backend/budget.py spend --model)
#
# All parameters except --discussion, --role, and --verdict are optional.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Defaults
DISCUSSION=""
ROLE=""
VERDICT=""
INPUT_TOKENS=0
OUTPUT_TOKENS=0
CACHE_READ_TOKENS=0
CACHE_WRITE_TOKENS=0
MODEL="claude-sonnet-4-20250514"
CONTENT=""
FILES=""
TAGS=""
PR=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --discussion)   DISCUSSION="$2"; shift 2 ;;
    --role)         ROLE="$2";       shift 2 ;;
    --verdict)      VERDICT="$2";    shift 2 ;;
    --input-tokens) INPUT_TOKENS="$2"; shift 2 ;;
    --output-tokens)      OUTPUT_TOKENS="$2";      shift 2 ;;
    --cache-read-tokens)  CACHE_READ_TOKENS="$2";  shift 2 ;;
    --cache-write-tokens) CACHE_WRITE_TOKENS="$2"; shift 2 ;;
    --model)              MODEL="$2";              shift 2 ;;
    --content)            CONTENT="$2";            shift 2 ;;
    --files)              FILES="$2";              shift 2 ;;
    --tags)               TAGS="$2";               shift 2 ;;
    --pr)                 PR="$2";                 shift 2 ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$DISCUSSION" || -z "$ROLE" || -z "$VERDICT" ]]; then
  echo "Error: --discussion, --role, and --verdict are required" >&2
  echo "Usage: $0 --discussion N --role ROLE --verdict VERDICT [options]" >&2
  exit 1
fi

# Map verdict to lesson_type
case "$VERDICT" in
  done|pass)   LESSON_TYPE="success" ;;
  fail|needs-fix) LESSON_TYPE="failure" ;;
  skip)        LESSON_TYPE="pattern" ;;
  *)           LESSON_TYPE="pattern" ;;
esac

# Default content if not provided
if [[ -z "$CONTENT" ]]; then
  CONTENT="${ROLE} ${VERDICT} on discussion #${DISCUSSION}"
fi

# Default tags: include role and verdict
if [[ -z "$TAGS" ]]; then
  TAGS="${ROLE},${VERDICT}"
fi

# 1. Record memory lesson
echo "[record-agent-result] Recording memory lesson: role=${ROLE} type=${LESSON_TYPE} discussion=${DISCUSSION}"
python3 "$REPO_ROOT/backend/agent_memory.py" record \
  --discussion "$DISCUSSION" \
  --role "$ROLE" \
  --type "$LESSON_TYPE" \
  --content "$CONTENT" \
  --files "$FILES" \
  --tags "$TAGS"

# 2. Record budget spend
AGENT_ID="${ROLE}-${DISCUSSION}-$(date +%s)"
echo "[record-agent-result] Recording budget spend: ${INPUT_TOKENS} in / ${OUTPUT_TOKENS} out (model: ${MODEL})"
SPEND_ARGS=("$AGENT_ID" "$ROLE" "$INPUT_TOKENS" "$OUTPUT_TOKENS" --discussion "$DISCUSSION" --model "$MODEL")
[[ -n "$PR" ]]                                          && SPEND_ARGS+=(--pr "$PR")
[[ "${CACHE_READ_TOKENS:-0}" -gt 0 ]] 2>/dev/null       && SPEND_ARGS+=(--cache-read-tokens "$CACHE_READ_TOKENS")
[[ "${CACHE_WRITE_TOKENS:-0}" -gt 0 ]] 2>/dev/null      && SPEND_ARGS+=(--cache-write-tokens "$CACHE_WRITE_TOKENS")
python3 "$REPO_ROOT/backend/budget.py" spend "${SPEND_ARGS[@]}" \
  || echo "[record-agent-result] Warning: budget spend failed (non-fatal)" >&2

echo "[record-agent-result] Done."
