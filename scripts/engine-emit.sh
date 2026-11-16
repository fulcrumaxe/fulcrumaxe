#!/usr/bin/env bash
# engine-emit.sh <kind> --field value ...  (checkin = checkin_tick): append one event.
# Fields are checked against schemas/engine-events.v1.json; free text is refused.
# Exit 2 refused, 1 could not write. Callers run it as `timeout 10 ... || true`.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/state-dir.sh
source "$SCRIPT_DIR/lib/state-dir.sh" 2>/dev/null || true
exec python3 "$SCRIPT_DIR/lib/engine_emit.py" "$@"
