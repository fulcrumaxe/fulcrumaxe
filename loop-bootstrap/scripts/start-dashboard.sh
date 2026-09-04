#!/usr/bin/env bash
# scripts/start-dashboard.sh — project-agnostic dashboard launcher (installed copy).
#
# This file is installed by loop-bootstrap/bootstrap.sh into an adopter's own
# project at <target>/scripts/start-dashboard.sh — it does not ship the
# dashboard itself (backend/, dashboard/ live in a fulcrumaxe checkout, not
# here). It reads .autonomous-team/project.json from the current directory to
# get:
#   - state_dir      → exported as AUTONOMOUS_TEAM_STATE_DIR
#   - dashboard_port → the port for the Vite dev server
#
# ...then delegates to a real fulcrumaxe checkout's scripts/start-dashboard.sh,
# which does the actual work of launching all four dashboard services.
#
# Usage:
#   cd /path/to/your-project
#   AF_ROOT=/path/to/fulcrumaxe bash scripts/start-dashboard.sh
#
# Prerequisites:
#   - AF_ROOT must point at a fulcrumaxe checkout (the repo that contains the
#     actual dashboard code). There is no reliable way to auto-detect this:
#     the target project this script is installed into has no filesystem
#     relationship to wherever you happened to clone fulcrumaxe, so guessing
#     silently would risk delegating to the wrong tree. Set AF_ROOT once in
#     your shell profile, or pass it inline each time.
#   - .autonomous-team/project.json must exist with dashboard_port and
#     state_dir set (created as part of project setup).
#
# Note: for the fulcrumaxe project itself, use its own scripts/start-dashboard.sh
# directly — it has hardcoded defaults and doesn't need AF_ROOT or project.json.

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve AF_ROOT (the fulcrumaxe checkout to delegate to)
# ---------------------------------------------------------------------------
# AF_ROOT must be set explicitly — see "Prerequisites" above for why no
# fallback is safe here. Fail loudly and name exactly what to set rather
# than silently delegating to the wrong directory (or this script's own
# parent, which is the adopter's project, not fulcrumaxe).
if [[ -z "${AF_ROOT:-}" ]]; then
    echo "ERROR: AF_ROOT is not set." >&2
    echo "       This launcher delegates to a fulcrumaxe checkout for the actual" >&2
    echo "       dashboard services (backend/, dashboard/) — it doesn't ship them." >&2
    echo "       Set AF_ROOT to the path of your fulcrumaxe checkout, e.g.:" >&2
    echo "         AF_ROOT=/path/to/fulcrumaxe bash scripts/start-dashboard.sh" >&2
    exit 1
fi
if [[ ! -d "$AF_ROOT" ]]; then
    echo "ERROR: AF_ROOT=\"$AF_ROOT\" is not a directory." >&2
    echo "       Set AF_ROOT to the path of your fulcrumaxe checkout." >&2
    exit 1
fi
if [[ ! -f "$AF_ROOT/scripts/start-dashboard.sh" ]]; then
    echo "ERROR: AF_ROOT=\"$AF_ROOT\" does not look like a fulcrumaxe checkout" >&2
    echo "       (missing scripts/start-dashboard.sh)." >&2
    echo "       Set AF_ROOT to the path of your fulcrumaxe checkout." >&2
    exit 1
fi

# The target project root is the working directory, or can be passed as $1
TARGET_ROOT="${1:-$(pwd)}"
PROJECT_JSON="$TARGET_ROOT/.autonomous-team/project.json"

if [[ ! -f "$PROJECT_JSON" ]]; then
    echo "ERROR: project.json not found at $PROJECT_JSON" >&2
    echo "       coldstart-project.sh lives in the fulcrumaxe checkout, not this project —" >&2
    echo "       run it from AF_ROOT, pointed at this repo:" >&2
    echo "         bash \"\$AF_ROOT/scripts/coldstart-project.sh\" \"$TARGET_ROOT\" <project-name>" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Parse project.json
# ---------------------------------------------------------------------------
read_json_field() {
    local file="$1"
    local field="$2"
    python3 -c "
import json, sys
try:
    d = json.load(open('$file'))
    v = d.get('$field')
    if v is None:
        sys.exit(1)
    print(v)
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
"
}

STATE_DIR="$(read_json_field "$PROJECT_JSON" "state_dir")" || {
    echo "ERROR: could not read 'state_dir' from $PROJECT_JSON" >&2
    echo "       Check that the file is valid JSON and contains 'state_dir'." >&2
    exit 1
}

DASHBOARD_PORT="$(read_json_field "$PROJECT_JSON" "dashboard_port")" || {
    echo "WARN: 'dashboard_port' not set in $PROJECT_JSON — defaulting to 5173" >&2
    DASHBOARD_PORT=5173
}

PROJECT_NAME="$(read_json_field "$PROJECT_JSON" "project_name" 2>/dev/null)" || {
    PROJECT_NAME="$(basename "$TARGET_ROOT")"
}

# ---------------------------------------------------------------------------
# Export state dir for child processes
# ---------------------------------------------------------------------------
export AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR"

# ---------------------------------------------------------------------------
# Delegate to the fulcrumaxe checkout's start-dashboard.sh with port override
# ---------------------------------------------------------------------------
# AF_ROOT and $AF_ROOT/scripts/start-dashboard.sh were already validated above.
START_SCRIPT="$AF_ROOT/scripts/start-dashboard.sh"

echo "[start-dashboard] Project: $PROJECT_NAME"
echo "[start-dashboard] State dir: $STATE_DIR"
echo "[start-dashboard] Dashboard port: $DASHBOARD_PORT"
echo "[start-dashboard] AUTONOMOUS_TEAM_STATE_DIR=$STATE_DIR"
echo ""

# Read explicit ports block from project.json if present; otherwise derive from
# dashboard_port so all 4 services get non-colliding ports.
# start-dashboard.sh reads these env vars and uses them over its own defaults.
_PORTS_JSON="$(python3 -c "
import json, sys
try:
    d = json.load(open('$PROJECT_JSON'))
    ports = d.get('ports', {})
    dp = d.get('dashboard_port', $DASHBOARD_PORT)
    vite = ports.get('vite', dp)
    api  = ports.get('api',  dp + 100)
    rpc  = ports.get('rpc',  dp + 200)
    sse  = ports.get('sse',  dp + 300)
    print(f'{vite} {api} {rpc} {sse}')
except Exception:
    print(f'$DASHBOARD_PORT 0 0 0')
" 2>/dev/null || echo "$DASHBOARD_PORT 0 0 0")"
read -r _VPT _APT _RPT _SPT <<< "$_PORTS_JSON"

export AF_VITE_PORT="$_VPT"
[[ "$_APT" -gt 0 ]] 2>/dev/null && export AF_API_PORT="$_APT" || true
[[ "$_RPT" -gt 0 ]] 2>/dev/null && export AF_RPC_PORT="$_RPT" || true
[[ "$_SPT" -gt 0 ]] 2>/dev/null && export AF_SSE_PORT="$_SPT" || true

exec bash "$START_SCRIPT" "$@"
