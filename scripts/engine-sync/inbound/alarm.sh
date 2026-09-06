#!/usr/bin/env bash
# alarm.sh -- Slice A of D#2439 (engine-sync inbound).
#
# Wraps staleness.sh with edge-triggered notification. It never writes git
# state, never fetches, never applies a patch, never spawns an agent. The
# only file it writes is its own small state file, which remembers the last
# observed status so a repeat run can be told apart from a transition.
#
# stdout is exactly staleness.sh's JSON line, unchanged, and the exit code
# mirrors staleness.sh's (0 in-sync / 1 stale / 2 undecidable) -- a caller
# that only wants the status (loop-preflight.sh) can call this in place of
# staleness.sh and get identical output, plus the notification side effect.
#
# Edge-triggering: a notification is posted only on the transition FROM a
# non-failure status (in-sync, or no prior recorded status) INTO a failure
# status (stale or undecidable). Two consecutive failure runs post exactly
# one notification -- the second is a repeat, not a transition. Recovering
# to in-sync posts no notification of its own, but it does clear the
# recorded failure status, so the *next* failure (even the same kind as
# before) is a fresh transition and fires again. Per-run notification is
# the bug this alarm exists to not repeat.
#
# The notify sink is injectable for tests:
#   ENGINE_SYNC_NOTIFY_CMD  a command run with the status JSON as its only
#                           argument. Default: append one line to
#                           $STATE_DIR/engine-sync-inbound-notifications.log
#
# Env overrides (mainly for tests; MARKER_REF/REMOTE/REMOTE_BRANCH/GIT_DIR
# are passed straight through to staleness.sh, see its own header):
#   ENGINE_SYNC_MARKER_REF, ENGINE_SYNC_REMOTE, ENGINE_SYNC_REMOTE_BRANCH,
#   ENGINE_SYNC_GIT_DIR
#   ENGINE_SYNC_STATE_DIR   override for $AUTONOMOUS_TEAM_STATE_DIR (tests)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${ENGINE_SYNC_STATE_DIR:-${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}}"
STATE_FILE="$STATE_DIR/engine-sync-inbound.json"

mkdir -p "$STATE_DIR" 2>/dev/null || true

STATUS_JSON="$("$SCRIPT_DIR/staleness.sh")"
STATUS_RC=$?

CURRENT_STATUS="$(printf '%s' "$STATUS_JSON" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("status", "undecidable"))
except Exception:
    print("undecidable")
' 2>/dev/null)"
[ -z "$CURRENT_STATUS" ] && CURRENT_STATUS="undecidable"

PREV_STATUS=""
if [ -f "$STATE_FILE" ]; then
  PREV_STATUS="$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("last_status", ""))
except Exception:
    print("")
' "$STATE_FILE" 2>/dev/null)"
fi

is_failure_status() {
  [ "$1" = "stale" ] || [ "$1" = "undecidable" ]
}

SHOULD_NOTIFY=0
if is_failure_status "$CURRENT_STATUS" && ! is_failure_status "$PREV_STATUS"; then
  SHOULD_NOTIFY=1
fi

# Persist the new status BEFORE notifying: a notify-sink failure must never
# leave the state stuck re-firing on every subsequent run.
python3 -c '
import json, sys
with open(sys.argv[1], "w") as f:
    json.dump({"last_status": sys.argv[2]}, f)
' "$STATE_FILE" "$CURRENT_STATUS" 2>/dev/null || true

if [ "$SHOULD_NOTIFY" -eq 1 ]; then
  if [ -n "${ENGINE_SYNC_NOTIFY_CMD:-}" ]; then
    $ENGINE_SYNC_NOTIFY_CMD "$STATUS_JSON" || true
  else
    printf '%s %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$STATUS_JSON" \
      >> "$STATE_DIR/engine-sync-inbound-notifications.log" 2>/dev/null || true
  fi
fi

printf '%s\n' "$STATUS_JSON"
exit "$STATUS_RC"
