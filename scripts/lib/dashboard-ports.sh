#!/usr/bin/env bash
# scripts/lib/dashboard-ports.sh — resolve the four dashboard service ports.
#
# Shared by scripts/start-dashboard.sh (which starts the services) and
# scripts/start-the-day.sh (which only checks whether they're already bound,
# as part of its self-heal step) so the two agree by construction instead of
# each hardcoding its own copy of the same four port numbers (D#2598).
#
# Resolution order (highest to lowest priority):
#   1. AF_API_PORT / AF_RPC_PORT / AF_SSE_PORT / AF_VITE_PORT env vars
#   2. "ports" block in .autonomous-team/project.json
#   3. Derived from dashboard_port in project.json (vite=base, api=+100, rpc=+200, sse=+300)
#   4. Hardcoded autonomous-forever defaults (18099/8765/8420/5173)
#
# Usage:
#   source scripts/lib/dashboard-ports.sh
#   resolve_dashboard_ports "$REPO_ROOT"
#   echo "$API_PORT $RPC_PORT $SSE_PORT $VITE_PORT"

_dashboard_ports_read_from_project() {
  local project_json="$1"
  if [[ ! -f "$project_json" ]]; then return; fi
  python3 - "$project_json" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    ports = d.get('ports', {})
    dp = d.get('dashboard_port')

    # If explicit ports block present, use it
    if ports.get('vite') and ports.get('api') and ports.get('rpc') and ports.get('sse'):
        print(f"vite={ports['vite']}")
        print(f"api={ports['api']}")
        print(f"rpc={ports['rpc']}")
        print(f"sse={ports['sse']}")
    elif isinstance(dp, int):
        # Derive from dashboard_port
        print(f"vite={dp}")
        print(f"api={dp + 100}")
        print(f"rpc={dp + 200}")
        print(f"sse={dp + 300}")
except Exception:
    pass
PYEOF
}

# resolve_dashboard_ports <repo_root> — sets API_PORT, RPC_PORT, SSE_PORT,
# VITE_PORT in the caller's shell (not local — callers read them back).
resolve_dashboard_ports() {
  local repo_root="${1:?resolve_dashboard_ports: repo_root required}"
  local project_json="$repo_root/.autonomous-team/project.json"

  # Default ports (autonomous-forever hardcoded values — preserved for backward compatibility)
  API_PORT=18099
  RPC_PORT=8765
  SSE_PORT=8420
  VITE_PORT=5173

  # Apply project.json ports (overrides defaults)
  while IFS='=' read -r key val; do
    [[ -z "$key" ]] && continue
    case "$key" in
      api)  API_PORT="$val" ;;
      rpc)  RPC_PORT="$val" ;;
      sse)  SSE_PORT="$val" ;;
      vite) VITE_PORT="$val" ;;
    esac
  done < <(_dashboard_ports_read_from_project "$project_json")

  # Apply explicit env overrides (highest priority)
  API_PORT="${AF_API_PORT:-$API_PORT}"
  RPC_PORT="${AF_RPC_PORT:-$RPC_PORT}"
  SSE_PORT="${AF_SSE_PORT:-$SSE_PORT}"
  VITE_PORT="${AF_VITE_PORT:-$VITE_PORT}"
}
