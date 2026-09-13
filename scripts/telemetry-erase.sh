#!/usr/bin/env bash
# scripts/telemetry-erase.sh — erase every row this install ever sent (D#2565).
#
# Reads the install id itself and issues the DELETE — the id never reaches
# scrollback or a clipboard on a machine whose code plane is public.
#
# Ordering matters: run this BEFORE deleting the state file (or the whole
# state dir). Deleting the state file first permanently forfeits erasure of
# rows already sent — the id is gone locally, but the rows persist
# server-side under an id nobody can reproduce anymore. See CLAUDE.md.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
  esac
done

INSTALL_ID="$(PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/backend/telemetry_install_id.py" --read 2>/dev/null)"

if [ -z "$INSTALL_ID" ]; then
  echo "No telemetry install id found for this machine — nothing to erase."
  exit 0
fi

# Never print the literal id — a short, redacted stand-in only. Kept to 2+2
# characters (16 bits) rather than 4+4: enough for an operator to eyeball
# "yes, that's the run I meant", but 4+4 (32 bits) was flagged as usable for
# cross-log correlation even though it can't reproduce the real id.
REDACTED="${INSTALL_ID:0:2}...${INSTALL_ID: -2}"

if [ "$DRY_RUN" = true ]; then
  echo "Would issue: DELETE /api/telemetry  host=fulcrumaxe.dev  install=${REDACTED}"
  exit 0
fi

curl -sS --connect-timeout 1 --max-time 1.5 --retry 0 \
  -X DELETE "https://fulcrumaxe.dev/api/telemetry" \
  -H "Content-Type: application/json" \
  -d "{\"install\": \"${INSTALL_ID}\"}" \
  -o /dev/null
RC=$?

if [ "$RC" -eq 0 ]; then
  echo "Erase request sent (install ${REDACTED})."
else
  echo "Erase request failed (curl exit ${RC}) — try again, or check your network."
fi
exit "$RC"
