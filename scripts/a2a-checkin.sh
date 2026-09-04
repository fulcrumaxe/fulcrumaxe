#!/usr/bin/env bash
# a2a-checkin.sh — query this agent's inbox and print unread messages.
#
# Usage: bash scripts/a2a-checkin.sh [--peek]
#
# --peek: read messages without marking them as read.
# Exits 0 always (including broker-down, empty inbox).
#
# Environment:
#   A2A_PORT        broker port (default: 8830)
#   A2A_TOKEN       Bearer token for this agent
#   CLAUDE_AGENT_EVENT_ID  agent_id for this agent

set +e

A2A_BASE="http://127.0.0.1:${A2A_PORT:-8830}"
PEEK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --peek) PEEK=1; shift ;;
    *)      shift ;;
  esac
done

AGENT_ID="${CLAUDE_AGENT_EVENT_ID:-unknown}"
TOKEN="${A2A_TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  exit 0
fi

URL="$A2A_BASE/a2a/inbox/$AGENT_ID"
if [[ "$PEEK" -eq 1 ]]; then
  URL="${URL}?peek=1"
fi

HTTP_CODE=$(curl -sf \
  -H "Authorization: Bearer $TOKEN" \
  -o /tmp/a2a-inbox-$$.json \
  -w "%{http_code}" \
  "$URL" 2>/dev/null)

if [[ "$HTTP_CODE" == "204" || "$HTTP_CODE" == "" ]]; then
  # Empty inbox or broker down
  rm -f /tmp/a2a-inbox-$$.json
  exit 0
fi

if [[ "$HTTP_CODE" == "200" ]]; then
  python3 -c "
import json, sys
data = json.load(open('/tmp/a2a-inbox-$$.json'))
msgs = data.get('messages', [])
for m in msgs:
    print(f\"[{m['ts']}] {m['from']} ({m['kind']}): {m['body']}\")
" 2>/dev/null
fi

rm -f /tmp/a2a-inbox-$$.json
exit 0
