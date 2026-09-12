#!/usr/bin/env bash
# scripts/gate1-verify-containment.sh — reports containment state. Does NOT
# gate anything: exit code is 0 whether the verdict is CONTAINED or
# UNCONTAINED.
#
# Probes four capabilities a Gate-1 pass/fail signal does not need but which
# a same-uid Gate-1 run currently can reach: the gh credential, the state
# dir, the operator checkout (.git included), and the network. Today, on
# this host, before any host-provisioning gesture, all four are expected to
# read NOT-DENIED and the verdict UNCONTAINED — that is not a bug in this
# script, it is what makes the gap this reports honest instead of vacuous.
#
# Every probe is benign and self-cleaning:
#   gh-credential            `gh auth token`, discarded to /dev/null.
#   state-dir                touch+rm a .gate1-probe.$$ file under
#                             $AUTONOMOUS_TEAM_STATE_DIR (default
#                             ~/.autonomous-forever-state).
#   operator-checkout-write  touch+rm a .gate1-probe.$$ file under this
#                             script's own repo root's .git/ directory.
#   network                  `curl -sS -m 5 https://api.github.com/zen`.
#
# Environment scrubbing (rewriting $HOME, unsetting GH_TOKEN/GITHUB_TOKEN,
# clearing DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR) is deliberately NOT
# attempted anywhere in this script: the gh credential lives in the system
# keyring, not in a dotfile under $HOME, so scrubbing the environment at the
# same uid measurably buys nothing against it. A probe that only "passed"
# because it scrubbed the environment would be reporting on the scrub, not
# on the credential — this script reports on the credential.
#
# Usage: bash scripts/gate1-verify-containment.sh
# Exit code: always 0. Read the printed verdict line instead.
set -uo pipefail  # no -e: every probe below must run even if an earlier one fails

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"

# _probe LABEL CMD... — runs CMD, prints "gate1_probe LABEL=NOT-DENIED" and
# returns 0 if it succeeded (the capability was reachable), else prints
# "gate1_probe LABEL=DENIED" and returns 1.
_probe() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "gate1_probe ${label}=NOT-DENIED"
    return 0
  else
    echo "gate1_probe ${label}=DENIED"
    return 1
  fi
}

_gh_credential_probe() {
  gh auth token
}

_state_dir_probe() {
  local f="$STATE_DIR/.gate1-probe.$$"
  touch "$f" && rm -f "$f"
}

_operator_checkout_write_probe() {
  local f="$REPO_ROOT/.git/.gate1-probe.$$"
  touch "$f" && rm -f "$f"
}

_network_probe() {
  curl -sS -m 5 https://api.github.com/zen
}

DENIED_COUNT=0
_probe "gh-credential" _gh_credential_probe || DENIED_COUNT=$((DENIED_COUNT + 1))
_probe "state-dir" _state_dir_probe || DENIED_COUNT=$((DENIED_COUNT + 1))
_probe "operator-checkout-write" _operator_checkout_write_probe || DENIED_COUNT=$((DENIED_COUNT + 1))
_probe "network" _network_probe || DENIED_COUNT=$((DENIED_COUNT + 1))

if [ "$DENIED_COUNT" -eq 4 ]; then
  echo "gate1_containment_verdict=CONTAINED"
else
  echo "gate1_containment_verdict=UNCONTAINED"
fi

exit 0
