#!/usr/bin/env bash
# measure-spawn-context.sh — Report first-turn cache_creation_input_tokens for role spawns.
#
# Usage:
#   bash scripts/measure-spawn-context.sh [--help]
#   bash scripts/measure-spawn-context.sh [--roles executor,code-reviewer,project-manager]
#   bash scripts/measure-spawn-context.sh [--csv /path/to/output.csv]
#
# How it works:
#   Scans the Claude Code transcript JSONL at
#   ~/.claude/projects/-home-agent-fulcrumaxe/*.jsonl
#   for entries with role=assistant and type=usage that contain
#   cache_creation_input_tokens. Groups by the first assistant turn
#   in each session. Reports the median across sessions per role.
#
# Output:
#   CSV to stdout: role,session_id,cache_creation_input_tokens
#   Summary line to stderr: role=<role> median=<N> sessions=<N>

set -euo pipefail

ROLES="executor,code-reviewer,project-manager"
CSV_OUT=""
TRANSCRIPT_DIR="$HOME/.claude/projects/-home-agent-fulcrumaxe"

usage() {
  grep '^#' "$0" | sed 's/^# \?//' | head -20
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --roles) ROLES="$2"; shift 2 ;;
    --csv) CSV_OUT="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ ! -d "$TRANSCRIPT_DIR" ]]; then
  echo "ERROR: Transcript dir not found: $TRANSCRIPT_DIR" >&2
  echo "Set TRANSCRIPT_DIR env var to override." >&2
  exit 1
fi

IFS=',' read -ra ROLE_LIST <<< "$ROLES"

# Print header
echo "role,session_id,cache_creation_input_tokens"

for role in "${ROLE_LIST[@]}"; do
  # Find transcript files that mention this role in the first 5KB
  # (role appears in the spawn prompt sent to the sub-agent)
  role_sessions=()
  while IFS= read -r jsonl_file; do
    # Check if file mentions this role
    if head -c 10000 "$jsonl_file" | grep -q "\"$role\"" 2>/dev/null; then
      role_sessions+=("$jsonl_file")
    fi
  done < <(find "$TRANSCRIPT_DIR" -name "*.jsonl" -newer /dev/null 2>/dev/null | sort -t/ -k1 | tail -50)

  for session_file in "${role_sessions[@]}"; do
    session_id=$(basename "$session_file" .jsonl)
    # Extract first assistant turn's cache_creation_input_tokens
    tokens=$(python3 -c "
import json, sys
found = False
for line in open('$session_file'):
    try:
        entry = json.loads(line)
    except:
        continue
    # Look for usage in assistant messages (first turn)
    if entry.get('role') == 'assistant' and not found:
        usage = entry.get('usage', {})
        if 'cache_creation_input_tokens' in usage:
            print(usage['cache_creation_input_tokens'])
            found = True
            break
" 2>/dev/null || echo "")
    if [[ -n "$tokens" && "$tokens" -gt 0 ]]; then
      echo "$role,$session_id,$tokens"
    fi
  done
done | tee "${CSV_OUT:-/dev/null}"

# Print summary stats to stderr
echo "" >&2
echo "=== Summary ===" >&2
IFS=',' read -ra ROLE_LIST <<< "$ROLES"
for role in "${ROLE_LIST[@]}"; do
  # Re-run just to get summary (reading already-printed output is tricky in bash)
  # Read from transcript files again
  tokens_list=()
  while IFS= read -r jsonl_file; do
    if head -c 10000 "$jsonl_file" | grep -q "\"$role\"" 2>/dev/null; then
      tokens=$(python3 -c "
import json
for line in open('$jsonl_file'):
    try:
        entry = json.loads(line)
    except:
        continue
    if entry.get('role') == 'assistant':
        usage = entry.get('usage', {})
        if 'cache_creation_input_tokens' in usage:
            print(usage['cache_creation_input_tokens'])
            break
" 2>/dev/null || echo "")
      if [[ -n "$tokens" && "$tokens" -gt 0 ]]; then
        tokens_list+=("$tokens")
      fi
    fi
  done < <(find "$TRANSCRIPT_DIR" -name "*.jsonl" -newer /dev/null 2>/dev/null | sort | tail -50)

  if [[ ${#tokens_list[@]} -gt 0 ]]; then
    median=$(printf '%s\n' "${tokens_list[@]}" | sort -n | awk '{a[NR]=$1} END{if(NR%2==1)print a[int(NR/2)+1]; else print (a[NR/2]+a[NR/2+1])/2}')
    echo "role=$role median=$median sessions=${#tokens_list[@]}" >&2
  else
    echo "role=$role no sessions found" >&2
  fi
done
