#!/usr/bin/env bash
# alarm.sh -- staleness.sh plus edge-triggered notification and a grace
# window before the staleness is allowed to stop the loop.
#
# It never writes git state, never applies a patch, and never spawns an
# agent. The only file it writes is its own small state file.
#
# stdout is staleness.sh's JSON line with two fields added:
#
#   consecutive_failures  how many runs in a row have come back stale or
#                         undecidable without a genuine sync in between
#   halt                  true once that count has reached the threshold
#
# The exit code still mirrors staleness.sh's (0 in-sync / 1 stale /
# 2 undecidable), so a caller that only wants the status can keep reading
# it exactly as before.
#
# --- Edge-triggered notification ---
#
# A notification is posted only on the transition FROM a non-failure status
# (in-sync, or no prior recorded status) INTO a failure status (stale or
# undecidable). Two consecutive failure runs post exactly one notification;
# the second is a repeat, not a transition. Recovering to in-sync posts no
# notification of its own but clears the recorded failure status, so the
# next failure is a fresh transition and fires again. Per-run notification
# is the bug this exists to not repeat.
#
# --- The grace window, and why the counter does not reset on a good check ---
#
# Staleness is loud immediately but does not stop the loop immediately. The
# two costs are not symmetric: drift means work built on a stale base, which
# is detectable and recoverable by rebasing, while a halted loop means
# nothing happens at all until a human merges the sync PR -- and the same
# argument that says nobody reads an alarm at 03:00 says nobody merges a PR
# at 03:00 either. Halting on the first observation converts a recoverable
# problem into a total stop, justified by the unavailability of the humans
# it then depends on.
#
# So the halt fires on N consecutive non-sync observations, not on the
# first. A working sync never reaches N because it clears the staleness
# before the next check. Reaching N means the sync is not working, which is
# the condition worth stopping for.
#
# `undecidable` counts toward N as well: "I cannot tell whether we are
# stale" is worse than "we are stale", and it is reachable in ordinary
# operation (an uninitialized marker), so it cannot be treated as an error
# state that never happens.
#
# The counter resets on a genuine sync -- the marker ref moving forward to a
# descendant of where it was -- and on nothing else. It does NOT reset just
# because one check came back in-sync: a counter reset by a successful check
# is a counter that only ever reads zero, and this codebase already ships
# one number that is incremented and printed but never compared against
# anything. The threshold below is this one's consumer.
#
# An in-sync reading with a marker that did not move (the code plane's tip
# having been rewound to meet a stationary marker, say) deliberately does
# NOT clear the count. That state is abnormal and being stopped for it is
# the right outcome.
#
# --- Env overrides ---
#
# MARKER_REF/REMOTE/REMOTE_BRANCH/GIT_DIR pass straight through to
# staleness.sh; see its own header.
#   ENGINE_SYNC_MARKER_REF, ENGINE_SYNC_REMOTE, ENGINE_SYNC_REMOTE_BRANCH,
#   ENGINE_SYNC_GIT_DIR
#   ENGINE_SYNC_STATE_DIR         override for $AUTONOMOUS_TEAM_STATE_DIR
#   ENGINE_SYNC_HALT_THRESHOLD    N, default 6 (~1 hour at the loop's
#                                 10-minute cadence). Raising it is the
#                                 documented way for an operator to stand
#                                 the halt down without touching code.
#   ENGINE_SYNC_NOTIFY_CMD        a command run with the status JSON as its
#                                 only argument. Default: append one line to
#                                 $STATE_DIR/engine-sync-inbound-notifications.log

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${ENGINE_SYNC_STATE_DIR:-${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}}"
STATE_FILE="$STATE_DIR/engine-sync-inbound.json"
HALT_THRESHOLD="${ENGINE_SYNC_HALT_THRESHOLD:-6}"
MARKER_REF="${ENGINE_SYNC_MARKER_REF:-refs/synced/code-plane}"

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

# Where the marker points right now. Resolved in the same directory
# staleness.sh just used, so the two agree about which repo they mean.
GIT_C_DIR="${ENGINE_SYNC_GIT_DIR:-.}"
CURRENT_MARKER="$(git -C "$GIT_C_DIR" rev-parse --verify -q "${MARKER_REF}^{commit}" 2>/dev/null)"

PREV_STATUS=""
PREV_FAILURES=0
PREV_MARKER=""
if [ -f "$STATE_FILE" ]; then
  PREV_STATE="$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
print(d.get("last_status", ""))
print(int(d.get("consecutive_failures", 0) or 0))
print(d.get("last_marker_sha", ""))
' "$STATE_FILE" 2>/dev/null)"
  PREV_STATUS="$(printf '%s\n' "$PREV_STATE" | sed -n '1p')"
  PREV_FAILURES="$(printf '%s\n' "$PREV_STATE" | sed -n '2p')"
  PREV_MARKER="$(printf '%s\n' "$PREV_STATE" | sed -n '3p')"
fi
[ -z "$PREV_FAILURES" ] && PREV_FAILURES=0

is_failure_status() {
  [ "$1" = "stale" ] || [ "$1" = "undecidable" ]
}

# A genuine sync: the marker moved, and it moved FORWARD (the old position
# is an ancestor of the new one). A marker reset backwards, or repointed
# sideways, is not a sync and must not clear the count. If the old position
# cannot be resolved at all -- a first run, or objects since pruned -- the
# count starts from zero anyway because there is nothing to carry forward.
MARKER_ADVANCED=0
if [ -n "$CURRENT_MARKER" ] && [ -n "$PREV_MARKER" ] && [ "$CURRENT_MARKER" != "$PREV_MARKER" ]; then
  if git -C "$GIT_C_DIR" merge-base --is-ancestor "$PREV_MARKER" "$CURRENT_MARKER" 2>/dev/null; then
    MARKER_ADVANCED=1
  fi
fi

FAILURES="$PREV_FAILURES"
if [ "$MARKER_ADVANCED" -eq 1 ]; then
  FAILURES=0
fi
if is_failure_status "$CURRENT_STATUS"; then
  FAILURES=$((FAILURES + 1))
fi

HALT=false
if [ "$FAILURES" -ge "$HALT_THRESHOLD" ]; then
  HALT=true
fi

SHOULD_NOTIFY=0
if is_failure_status "$CURRENT_STATUS" && ! is_failure_status "$PREV_STATUS"; then
  SHOULD_NOTIFY=1
fi

# Persist the new state BEFORE notifying: a notify-sink failure must never
# leave the state stuck re-firing on every subsequent run.
python3 -c '
import json, sys
with open(sys.argv[1], "w") as f:
    json.dump(
        {
            "last_status": sys.argv[2],
            "consecutive_failures": int(sys.argv[3]),
            "last_marker_sha": sys.argv[4],
        },
        f,
    )
' "$STATE_FILE" "$CURRENT_STATUS" "$FAILURES" "$CURRENT_MARKER" 2>/dev/null || true

if [ "$SHOULD_NOTIFY" -eq 1 ]; then
  if [ -n "${ENGINE_SYNC_NOTIFY_CMD:-}" ]; then
    $ENGINE_SYNC_NOTIFY_CMD "$STATUS_JSON" || true
  else
    printf '%s %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$STATUS_JSON" \
      >> "$STATE_DIR/engine-sync-inbound-notifications.log" 2>/dev/null || true
  fi
fi

# Splice the two new fields into staleness.sh's own JSON rather than
# reformatting it, so every field it emitted survives verbatim.
OUT_JSON="$(printf '%s' "$STATUS_JSON" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {"status": "undecidable", "behind": None}
d["consecutive_failures"] = int(sys.argv[1])
d["halt"] = sys.argv[2] == "true"
d["halt_threshold"] = int(sys.argv[3])
print(json.dumps(d))
' "$FAILURES" "$HALT" "$HALT_THRESHOLD" 2>/dev/null)"
[ -z "$OUT_JSON" ] && OUT_JSON="$STATUS_JSON"

printf '%s\n' "$OUT_JSON"
exit "$STATUS_RC"
