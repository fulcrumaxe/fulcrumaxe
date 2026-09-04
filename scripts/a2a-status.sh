#!/usr/bin/env bash
# a2a-status.sh — post a one-way status update to Team Lead's inbox.
#
# Usage: bash scripts/a2a-status.sh "<status text>" [--to agent_id]
#
# Rate-limited by broker: 1 status per 2 minutes per sender.
# Exits 0 always (including broker-down).
#
# Environment:
#   A2A_PORT        broker port (default: 8830)
#   A2A_TOKEN       Bearer token for this agent
#   CLAUDE_AGENT_EVENT_ID  agent_id for this agent

set +e

A2A_BASE="http://127.0.0.1:${A2A_PORT:-8830}"
STATUS_TEXT="${1:-}"
TO_AGENT="team-lead-session-${PPID}"

shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to) TO_AGENT="$2"; shift 2 ;;
    *)    shift ;;
  esac
done

FROM_AGENT="${CLAUDE_AGENT_EVENT_ID:-unknown}"
TOKEN="${A2A_TOKEN:-}"

if [[ -z "$TOKEN" || -z "$STATUS_TEXT" ]]; then
  exit 0
fi

curl -sf -X POST "$A2A_BASE/a2a/message" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"from\":\"$FROM_AGENT\",\"to\":\"$TO_AGENT\",\"kind\":\"status\",\"body\":$(printf '%s' "$STATUS_TEXT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
  -o /dev/null \
  2>/dev/null

# Always exit 0 — status is optional coordination, never a hard dependency
exit 0
