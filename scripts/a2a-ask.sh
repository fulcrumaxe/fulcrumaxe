#!/usr/bin/env bash
# a2a-ask.sh — post a question via A2A broker and poll inbox for an answer.
#
# Usage: bash scripts/a2a-ask.sh "<question>" [--timeout 60] [--to agent_id]
#
# Reads $CLAUDE_AGENT_EVENT_ID (sender agent_id) and $A2A_TOKEN (Bearer token).
# Exits 0 on answer received, 1 on timeout — always exits 0 when broker is down.
#
# Environment:
#   A2A_PORT        broker port (default: 8830)
#   A2A_TOKEN       Bearer token for this agent
#   CLAUDE_AGENT_EVENT_ID  agent_id for this agent

set +e

A2A_BASE="http://127.0.0.1:${A2A_PORT:-8830}"
QUESTION="${1:-}"
TIMEOUT=60
TO_AGENT="team-lead-session-${PPID}"

shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --to)      TO_AGENT="$2"; shift 2 ;;
    *)         shift ;;
  esac
done

FROM_AGENT="${CLAUDE_AGENT_EVENT_ID:-unknown}"
TOKEN="${A2A_TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  # Broker-down / unconfigured — silent no-op
  exit 0
fi

if [[ -z "$QUESTION" ]]; then
  exit 0
fi

# Post the question
MSG_ID=$(curl -sf -X POST "$A2A_BASE/a2a/message" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"from\":\"$FROM_AGENT\",\"to\":\"$TO_AGENT\",\"kind\":\"question\",\"body\":$(printf '%s' "$QUESTION" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
  2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id",""))' 2>/dev/null)

if [[ -z "$MSG_ID" ]]; then
  # Broker down or error — silent degrade
  exit 0
fi

# Poll for an answer
DEADLINE=$(( $(date +%s) + TIMEOUT ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
  RESP=$(curl -sf \
    -H "Authorization: Bearer $TOKEN" \
    "$A2A_BASE/a2a/inbox/$FROM_AGENT" 2>/dev/null)
  if [[ -n "$RESP" ]]; then
    ANSWER=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
msgs = data.get('messages', [])
for m in msgs:
    if m.get('kind') == 'answer' and m.get('in_reply_to') == '$MSG_ID':
        print(m.get('body', ''))
        break
" <<< "$RESP" 2>/dev/null)
    if [[ -n "$ANSWER" ]]; then
      echo "$ANSWER"
      exit 0
    fi
  fi
  sleep 5
done

# Timeout — not an error for callers
exit 0
