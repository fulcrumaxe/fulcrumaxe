#!/usr/bin/env bash
# validate-agent-result.sh — Validate an agent output envelope against the schema.
# Usage: validate-agent-result.sh '<json-string>'
# Exit 0: valid. Exit 1: invalid (error message to stderr).

set -euo pipefail

JSON="${1:-}"

if [ -z "$JSON" ]; then
  echo "Usage: $0 '<json-string>'" >&2
  exit 1
fi

# Check it parses as JSON
if ! echo "$JSON" | jq . > /dev/null 2>&1; then
  echo "ERROR: input is not valid JSON" >&2
  exit 1
fi

# Check required fields
AGENT=$(echo "$JSON" | jq -r '.agent // empty')
VERDICT=$(echo "$JSON" | jq -r '.verdict // empty')

if [ -z "$AGENT" ]; then
  echo "ERROR: missing required field 'agent'" >&2
  exit 1
fi

if [ -z "$VERDICT" ]; then
  echo "ERROR: missing required field 'verdict'" >&2
  exit 1
fi

# Check verdict is one of the allowed enum values
VALID_VERDICTS="pass fail needs-fix done skip"
VERDICT_VALID=false
for v in $VALID_VERDICTS; do
  if [ "$VERDICT" = "$v" ]; then
    VERDICT_VALID=true
    break
  fi
done

if [ "$VERDICT_VALID" = "false" ]; then
  echo "ERROR: 'verdict' must be one of: $VALID_VERDICTS (got: '$VERDICT')" >&2
  exit 1
fi

# If issues array is present, validate each item has required fields
ISSUE_COUNT=$(echo "$JSON" | jq '.issues | length // 0' 2>/dev/null || echo 0)
if [ "$ISSUE_COUNT" -gt 0 ]; then
  VALID_SEVERITIES="error warning suggestion"
  for i in $(seq 0 $((ISSUE_COUNT - 1))); do
    ISSUE_FILE=$(echo "$JSON" | jq -r ".issues[$i].file // empty")
    ISSUE_MSG=$(echo "$JSON" | jq -r ".issues[$i].message // empty")
    ISSUE_SEV=$(echo "$JSON" | jq -r ".issues[$i].severity // empty")

    if [ -z "$ISSUE_FILE" ]; then
      echo "ERROR: issues[$i] missing required field 'file'" >&2
      exit 1
    fi
    if [ -z "$ISSUE_MSG" ]; then
      echo "ERROR: issues[$i] missing required field 'message'" >&2
      exit 1
    fi
    if [ -z "$ISSUE_SEV" ]; then
      echo "ERROR: issues[$i] missing required field 'severity'" >&2
      exit 1
    fi

    SEV_VALID=false
    for s in $VALID_SEVERITIES; do
      if [ "$ISSUE_SEV" = "$s" ]; then
        SEV_VALID=true
        break
      fi
    done
    if [ "$SEV_VALID" = "false" ]; then
      echo "ERROR: issues[$i].severity must be one of: $VALID_SEVERITIES (got: '$ISSUE_SEV')" >&2
      exit 1
    fi
  done
fi

echo "OK: agent=$AGENT verdict=$VERDICT"
exit 0
