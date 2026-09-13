#!/usr/bin/env bash
# scripts/lib/telemetry.sh — opt-in daily counter report to fulcrumaxe.dev (D#2565).
#
# The gate check is the FIRST statement. Nothing below it resolves a
# hostname, opens a socket, or shells out to curl before that check passes
# — a DNS lookup ahead of the gate is itself a leak (it tells a resolver
# this host runs the tool, before a byte of payload exists).
#
# PR-a (this file, this PR) ships the two offline-verifiable guards:
#   - gate off                    -> return immediately (AC-2/AC-3)
#   - gate on, no install id yet  -> skip silently, never mint one here
#                                    (AC-7 — minting happens only on the
#                                    opt-in transition, in
#                                    backend/telemetry_install_id.py)
# The once-a-day stamp check, the payload build, and the actual curl POST
# are PR-b's job (AC-11 to AC-13, AC-17).
#
# Called from the end of scripts/start-the-day.sh, after the sweeps — kept
# behind this one-line call rather than inlined, so the 700+ line ritual
# does not grow a network dependency inline.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

GATE="$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.telemetry_report 2>/dev/null)"
if [ "$GATE" != "true" ]; then
  exit 0
fi

# Gate is on. Check the install id exists — never regenerate here. A
# missing id at send time means the day is skipped, not that a new install
# is silently minted; that keeps a clobbered state dir from silently
# splitting one install into two forever.
INSTALL_ID="$(PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/backend/telemetry_install_id.py" --read 2>/dev/null)"
if [ -z "$INSTALL_ID" ]; then
  exit 0
fi

# PR-b adds here: the once-a-day stamp check, the payload build
# (backend/telemetry_report.py), disclosure-at-first-send, and the curl
# POST with --connect-timeout 1 --max-time 1.5 --retry 0.
exit 0
