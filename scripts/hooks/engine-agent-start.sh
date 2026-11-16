#!/usr/bin/env bash
# PreToolUse(Agent|Task) hook. With cloud_bridge.operate true it first asks the cloud (decide
# engine_spawn) and exits 2 with a fixed reason on ask, halted or unreachable, so the Agent call does
# not run and no agent_started is emitted for it. Otherwise it emits agent_started, prints nothing and
# exits 0: with operate off or absent it can never block a spawn.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD="$(cat)"
ENGINE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [[ -f "$SCRIPT_DIR/../lib/cloud-spawn-gate.sh" ]]; then
  # shellcheck source=../lib/cloud-spawn-gate.sh
  source "$SCRIPT_DIR/../lib/cloud-spawn-gate.sh"
  _rc=0
  cloud_spawn_gate "$ENGINE_ROOT" hook <<<"$PAYLOAD" >/dev/null || _rc=$?
  [[ "$_rc" -eq 0 ]] || exit 2
elif grep -qE '"operate"[[:space:]]*:[[:space:]]*(true|"|[^f[:space:]])' "$ENGINE_ROOT/.autonomous-team/config.json" 2>/dev/null; then
  echo "cloud unreachable; spawn refused (fail-closed)" >&2
  exit 2
fi
timeout 10 python3 "$SCRIPT_DIR/../lib/engine_agent_events.py" started <<<"$PAYLOAD" >/dev/null 2>&1 || true
exit 0
