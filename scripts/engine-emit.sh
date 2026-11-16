#!/usr/bin/env bash
# scripts/engine-emit.sh: append one event to engine-events.jsonl.
#
#   bash scripts/engine-emit.sh <kind> --field value ...
#   bash scripts/engine-emit.sh checkin --name ROW --origin cron   # alias of checkin_tick
#
# Fields are closed ids, enums and counts, checked against
# schemas/engine-events.v1.json. Free text is refused: exit 2, nothing written.
# Exit 1 means the line could not be written. Instrumented callers always run
# this as `... || true`: an emitter failure never changes their own exit status.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/state-dir.sh
source "$SCRIPT_DIR/lib/state-dir.sh" 2>/dev/null || true
exec python3 "$SCRIPT_DIR/lib/engine_emit.py" "$@"
