#!/usr/bin/env bash
# scripts/gh-api-dial.sh
#
# Thin wrapper around `gh api` for Team Lead callers.
#
# Before forwarding a mutating call (POST, PATCH, PUT, DELETE), it checks
# the dial registry to confirm that external.system dial is at level >= 2
# (the minimum required to make outbound GitHub mutations).
#
# Read-only calls (no -X / --method flag, or explicit GET/HEAD) are forwarded
# unconditionally — the dial check is only applied to mutations.
#
# Sub-agents are refused outright by the sandbox hook (hooks/sandbox.py) and
# should never reach this script. This wrapper is for Team Lead use only.
#
# Usage: scripts/gh-api-dial.sh [gh api arguments...]
#
# Examples:
#   # Read — no dial check needed:
#   bash scripts/gh-api-dial.sh repos/autonomous-agent-7/autonomous-forever/pulls --jq '.[].number'
#
#   # Mutation — dial registry consulted first:
#   bash scripts/gh-api-dial.sh repos/autonomous-agent-7/autonomous-forever/issues -X POST -f title="My Issue"
#
# Phase 2 (not in this PR):
#   - Emit a per-call audit row to audit.jsonl
#   - Migrate all Team Lead callers to use this wrapper

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# Detect whether the call is a mutation
# ---------------------------------------------------------------------------

is_mutation() {
  local args=("$@")
  for i in "${!args[@]}"; do
    local tok="${args[$i]}"
    if [[ "$tok" == "-X" || "$tok" == "--method" ]]; then
      local next_i=$((i + 1))
      if [[ $next_i -lt ${#args[@]} ]]; then
        local method="${args[$next_i]}"
        case "${method^^}" in
          POST|PATCH|PUT|DELETE) return 0 ;;
        esac
      fi
    fi
  done
  return 1
}

# ---------------------------------------------------------------------------
# Dial check for mutations
# ---------------------------------------------------------------------------

if is_mutation "$@"; then
  # Consult external.system dial — minimum level 2 required for GitHub mutations.
  DIAL_RESULT=$(python3 "$REPO_ROOT/backend/dial_registry.py" check external.system 2 2>/dev/null || true)
  # dial_registry.py check prints "allowed" or "denied: <reason>"
  if [[ "$DIAL_RESULT" != "allowed" ]]; then
    echo "gh-api-dial: blocked — external.system dial check failed: $DIAL_RESULT" >&2
    echo "Use: python3 backend/dial_registry.py set external.system 2 --source '{\"kind\":\"operator\"}'" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Forward to gh api
# ---------------------------------------------------------------------------

exec gh api "$@"
