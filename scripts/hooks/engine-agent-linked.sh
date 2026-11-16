#!/usr/bin/env bash
# PostToolUse(Agent|Task) hook: emit agent_linked (spawn id -> agent id). Observe-only: prints
# nothing and always exits 0, so it can never block anything.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
timeout 10 python3 "$SCRIPT_DIR/../lib/engine_agent_events.py" linked >/dev/null 2>&1 || true
exit 0
