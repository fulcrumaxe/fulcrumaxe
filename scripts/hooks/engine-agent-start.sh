#!/usr/bin/env bash
# PreToolUse(Agent|Task) hook: emit agent_started for the spawn. Observe-only: prints
# nothing and always exits 0, so it can never block a spawn.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
timeout 10 python3 "$SCRIPT_DIR/../lib/engine_agent_events.py" started >/dev/null 2>&1 || true
exit 0
