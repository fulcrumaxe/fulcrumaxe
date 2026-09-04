#!/usr/bin/env bash
# dial-state-summary.sh — daily dial-state snapshot posted to the team log.
#
# Reads all registered dial classes via dial_registry.list_directives() and
# posts a one-line summary to the team log.  Summary names any class where
# level != ceiling OR with active directives; reports "all classes at default"
# when nothing stands out.
#
# Scheduled: 07:00 UTC daily (see scripts/schedule/jobs.yaml).
# Gate: gates.dial_state_summary (default false).  Set to true to enable.
# token_ceiling: 0 — pure Python, no agent spawn.
#
# Exit codes: 0 = success or gate-off, 1 = failure

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
export REPO_ROOT

# ── 1. Control plane gate ─────────────────────────────────────────────────────
GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.dial_state_summary 2>/dev/null || echo "false")
if [[ "$GATE" != "true" ]]; then
  echo "[dial-state-summary] gate=off — skipping (set gates.dial_state_summary=true to enable)"
  exit 0
fi

echo "[dial-state-summary] gate=on — reading dial registry"

# ── 2. Read dial state ────────────────────────────────────────────────────────
DIAL_OUTPUT=$(python3 - <<'PYEOF' 2>&1
import sys, os
sys.path.insert(0, os.environ.get('REPO_ROOT', '.'))
from backend.dial_registry import list_directives

directives = list_directives()
non_default = []

for d in directives:
    cls = d["class"]
    lvl = d["level"]
    ceil = d["ceiling"]
    active_dirs = d.get("directives", [])
    if lvl != ceil or len(active_dirs) > 0:
        parts = [f"{cls}(level={lvl}/{ceil}"]
        if len(active_dirs) > 0:
            parts.append(f"+{len(active_dirs)} directive{'s' if len(active_dirs) != 1 else ''}")
        non_default.append("".join(parts) + ")")

if non_default:
    print("non-default: " + ", ".join(non_default))
else:
    print("all classes at default")
PYEOF
)
DIAL_RC=$?

if [[ $DIAL_RC -ne 0 ]]; then
  echo "[dial-state-summary] ERROR: failed to read dial registry (exit $DIAL_RC)" >&2
  echo "$DIAL_OUTPUT" >&2
  exit 1
fi

echo "[dial-state-summary] $DIAL_OUTPUT"

# ── 3. Post to team log ───────────────────────────────────────────────────────
bash "$REPO_ROOT/scripts/rotate-team-log.sh" comment \
  "[$(date -u +%H:%M)] dial-state-summary: $DIAL_OUTPUT" 2>/dev/null \
  || echo "[dial-state-summary] WARN: failed to post to team log (non-fatal)" >&2

echo "[dial-state-summary] done"
exit 0
