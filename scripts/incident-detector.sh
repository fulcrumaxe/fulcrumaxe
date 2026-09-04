#!/usr/bin/env bash
# incident-detector.sh — check for system-level incident conditions.
#
# Exits 0 with a JSON envelope if any trigger fires; exits 1 if the system
# is healthy. Called by /loop step 5.0.5 before potentially spawning the
# incident-commander.
#
# Trigger conditions:
#   1. circuit_breaker.tripped_roles >= 2 within the last hour
#   2. health_monitor.py check reports any subsystem stalled >= 2h
#   3. An open GitHub Issue has the label `incident` (manual escalation)
#
# Output (exit 0):
#   {"trigger":"circuit_breaker|health_stall|manual","evidence":{...}}
#
# Usage:
#   bash scripts/incident-detector.sh
#   bash scripts/incident-detector.sh --dry-run   # print state without exiting 0 on trigger

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"
source "${REPO_ROOT}/scripts/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

emit() {
  local trigger="$1"
  local evidence="$2"
  printf '{"trigger":"%s","evidence":%s}\n' "$trigger" "$evidence"
  if [ "$DRY_RUN" = "true" ]; then
    echo "[dry-run] trigger=$trigger (would exit 0)" >&2
    exit 0
  fi
  exit 0
}

# ---------------------------------------------------------------------------
# Trigger 1: circuit_breaker — 2+ roles tripped
# ---------------------------------------------------------------------------

CB_STATUS=""
CB_TRIPPED_ROLES="[]"
CB_TRIPPED_COUNT=0

if python3 backend/circuit_breaker.py status --json > /tmp/cb-status.json 2>/dev/null; then
  CB_STATUS=$(cat /tmp/cb-status.json)
  # Extract tripped roles — expects {"tripped_roles": ["executor", ...], ...}
  CB_TRIPPED_ROLES=$(python3 -c "
import json, sys
try:
    d = json.load(open('/tmp/cb-status.json'))
    roles = d.get('tripped_roles', [])
    print(json.dumps(roles))
except Exception as e:
    print('[]')
" 2>/dev/null || echo "[]")
  CB_TRIPPED_COUNT=$(python3 -c "import json; print(len(json.loads('$CB_TRIPPED_ROLES')))" 2>/dev/null || echo 0)
fi

if [ "$CB_TRIPPED_COUNT" -ge 2 ] 2>/dev/null; then
  EVIDENCE=$(python3 -c "
import json
evidence = {
    'tripped_roles': json.loads('$CB_TRIPPED_ROLES'),
    'tripped_count': $CB_TRIPPED_COUNT,
}
print(json.dumps(evidence))
" 2>/dev/null || printf '{"tripped_roles":%s,"tripped_count":%d}' "$CB_TRIPPED_ROLES" "$CB_TRIPPED_COUNT")
  emit "circuit_breaker" "$EVIDENCE"
fi

# ---------------------------------------------------------------------------
# Trigger 2: health_monitor — any subsystem stalled >= 2h
# ---------------------------------------------------------------------------

STALLED_SUBSYSTEMS="[]"
STALLED_COUNT=0

if python3 backend/health_monitor.py check --json > /tmp/health-status.json 2>/dev/null; then
  # Extract stalled subsystems — expects {"subsystems": {"name": {"status": "stalled", "stalled_since": ...}}}
  STALLED_JSON=$(python3 -c "
import json, sys
from datetime import datetime, timezone, timedelta

TWO_HOURS = timedelta(hours=2)
now = datetime.now(timezone.utc)

try:
    d = json.load(open('/tmp/health-status.json'))
    subsystems = d.get('subsystems', {})
    stalled = []
    for name, info in subsystems.items():
        if isinstance(info, dict) and info.get('status') == 'stalled':
            since_str = info.get('stalled_since', '')
            if since_str:
                try:
                    since = datetime.fromisoformat(since_str.replace('Z', '+00:00'))
                    if (now - since) >= TWO_HOURS:
                        stalled.append({'name': name, 'stalled_since': since_str})
                except Exception:
                    stalled.append({'name': name, 'stalled_since': since_str})
            else:
                stalled.append({'name': name})
    print(json.dumps(stalled))
except Exception as e:
    print('[]')
" 2>/dev/null || echo "[]")
  STALLED_COUNT=$(python3 -c "import json; print(len(json.loads('$STALLED_JSON')))" 2>/dev/null || echo 0)
  STALLED_SUBSYSTEMS="$STALLED_JSON"
fi

if [ "$STALLED_COUNT" -ge 1 ] 2>/dev/null; then
  EVIDENCE=$(python3 -c "
import json
evidence = {
    'stalled_subsystems': json.loads('$STALLED_SUBSYSTEMS'),
    'stalled_count': $STALLED_COUNT,
}
print(json.dumps(evidence))
" 2>/dev/null || printf '{"stalled_subsystems":%s,"stalled_count":%d}' "$STALLED_SUBSYSTEMS" "$STALLED_COUNT")
  emit "health_stall" "$EVIDENCE"
fi

# ---------------------------------------------------------------------------
# Trigger 3: manual escalation — open Issue with label `incident`
# ---------------------------------------------------------------------------

MANUAL_COUNT=0

if MANUAL_COUNT=$(gh issue list \
    --repo "$REPO" \
    --label incident \
    --state open \
    --json number,title,createdAt \
    --jq 'length' 2>/dev/null); then
  MANUAL_COUNT="${MANUAL_COUNT:-0}"
fi

if [ "${MANUAL_COUNT:-0}" -ge 1 ] 2>/dev/null; then
  ISSUES_JSON=$(gh issue list \
    --repo "$REPO" \
    --label incident \
    --state open \
    --json number,title,createdAt 2>/dev/null || echo "[]")
  EVIDENCE=$(python3 -c "
import json
issues = json.loads('$ISSUES_JSON') if '$ISSUES_JSON'.startswith('[') else []
evidence = {
    'open_incident_issues': issues,
    'count': int('$MANUAL_COUNT'),
}
print(json.dumps(evidence))
" 2>/dev/null || printf '{"open_incident_issues":[],"count":%s}' "$MANUAL_COUNT")
  emit "manual" "$EVIDENCE"
fi

# ---------------------------------------------------------------------------
# No trigger — healthy
# ---------------------------------------------------------------------------

if [ "$DRY_RUN" = "true" ]; then
  echo '{"status":"healthy","tripped_roles":[],"stalled_subsystems":[],"manual_incidents":0}' >&2
fi

exit 1
