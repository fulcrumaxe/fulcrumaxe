#!/usr/bin/env bash
# scripts/gate1-verify-containment.sh — reports containment state. Does NOT
# gate anything: exit code is 0 for every verdict.
#
# Probes four capabilities a Gate-1 pass/fail signal does not need but which
# a same-uid Gate-1 run currently can reach: the gh credential, the state
# dir, the operator checkout (.git included), and the network. Today, on
# this host, before any host-provisioning gesture, all four are expected to
# read NOT-DENIED and the verdict UNCONTAINED — that is not a bug in this
# script, it is what makes the gap this reports honest instead of vacuous.
#
# THREE OUTCOMES PER PROBE, NOT TWO. A probe whose tool is missing, or whose
# target doesn't exist, is neither denied nor not-denied — it simply never
# ran. Collapsing that into DENIED is dangerous precisely because DENIED is
# the "good" reading once PR-B is live: four DENIEDs print CONTAINED, so "I
# couldn't test this" would read identically to "this really is denied" —
# and the miscount lands on the side that tells an operator the host is
# safe when it might not be. So:
#   NOT-DENIED     the probe ran and the capability was reachable.
#   DENIED         the probe ran and the capability was NOT reachable.
#   INDETERMINATE  the probe's own precondition failed (missing binary,
#                  missing target) — it never got to test anything.
# Any INDETERMINATE probe forces the verdict to INDETERMINATE. It can never
# read as CONTAINED — an unrun probe must never look like a passing one.
#
# ABSOLUTE TARGETS, NOT PROBING-ENVIRONMENT-RELATIVE ONES. The state-dir and
# operator-checkout probes used to resolve their targets against whoever is
# currently running the script ($HOME, the script's own on-disk location) —
# which is exactly backwards for a script whose entire purpose is to be rerun
# under a *different* identity once PR-B provisions one. Under a different
# uid, $HOME moves and the script's own location may sit inside a different
# checkout (e.g. a worktree, where .git is a pointer file, not a directory)
# entirely independently of whether the real operator checkout became any
# less writable. Both targets are now resolved independently of the
# probing environment:
#   GATE1_VERIFY_STATE_DIR      absolute state-dir path. Defaults to
#                               $AUTONOMOUS_TEAM_STATE_DIR or
#                               $HOME/.autonomous-forever-state (today's
#                               same-uid behaviour) ONLY when unset — set it
#                               explicitly to the operator's real absolute
#                               path when probing as a different user.
#   GATE1_VERIFY_CHECKOUT_DIR   absolute operator-checkout path. Defaults to
#                               the checkout this script's own copy is
#                               physically part of, resolved via
#                               `git rev-parse --git-common-dir` — that
#                               resolves to the SAME shared .git regardless
#                               of which worktree's copy of this script is
#                               running, so it survives being invoked from
#                               inside a worktree. Set it explicitly if this
#                               script is ever deployed outside a git
#                               checkout entirely.
#
# Every probe is benign and self-cleaning:
#   gh-credential            `gh auth token`, discarded to /dev/null.
#   state-dir                READS the state dir (not a write) — the
#                             runbook's own stated risk ("a second uid can
#                             already read audit.jsonl, state.db,
#                             discussion_cache.db") is a read concern, and a
#                             write-only probe would flip to DENIED under a
#                             hardening that removes write but leaves the
#                             directory traversable, silently misreporting
#                             the thing the runbook actually cares about.
#   operator-checkout-write  touch+rm a .gate1-probe.$$ file under
#                             $GATE1_VERIFY_CHECKOUT_DIR's .git/ directory.
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

# _resolve_checkout_root — prints the shared operator checkout root on
# stdout and returns 0, or prints nothing and returns 1 if it can't be
# determined. Uses git-common-dir rather than "$SCRIPT_DIR/.." so the same
# answer comes back whether this copy of the script lives in the operator's
# own checkout or inside one of its worktrees (both share one .git).
_resolve_checkout_root() {
  local common_dir
  common_dir="$(cd "$SCRIPT_DIR" && git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common_dir" ] || return 1
  (cd "$SCRIPT_DIR" && cd "$(dirname "$common_dir")" && pwd) 2>/dev/null
}

STATE_DIR="${GATE1_VERIFY_STATE_DIR:-${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}}"
if [ -n "${GATE1_VERIFY_CHECKOUT_DIR:-}" ]; then
  CHECKOUT_DIR="$GATE1_VERIFY_CHECKOUT_DIR"
else
  CHECKOUT_DIR="$(_resolve_checkout_root)" || CHECKOUT_DIR=""
fi

# _probe LABEL PRECONDITION_FN COMMAND_FN — checks PRECONDITION_FN first,
# entirely separately from COMMAND_FN's own exit code. This split matters:
# several real commands (GNU `ls`, for one) already use exit code 2 for
# their own unrelated failure modes (e.g. "Permission denied" on `ls -A`
# against an unreadable directory legitimately exits 2), so a design that
# tried to smuggle "precondition failed" through the same exit-code channel
# as "the command ran and failed" would misread that real denial as
# INDETERMINATE. Precondition and outcome are checked in two separate steps
# so neither can be confused with the other:
#   PRECONDITION_FN fails -> INDETERMINATE, COMMAND_FN never runs.
#   PRECONDITION_FN passes, COMMAND_FN exits 0       -> NOT-DENIED.
#   PRECONDITION_FN passes, COMMAND_FN exits nonzero -> DENIED.
# Prints one line and returns 0 (NOT-DENIED), 1 (DENIED), or 2
# (INDETERMINATE) so the caller can aggregate it.
_probe() {
  local label="$1" precondition_fn="$2" command_fn="$3"
  if ! "$precondition_fn" >/dev/null 2>&1; then
    echo "gate1_probe ${label}=INDETERMINATE"
    return 2
  fi
  if "$command_fn" >/dev/null 2>&1; then
    echo "gate1_probe ${label}=NOT-DENIED"
    return 0
  else
    echo "gate1_probe ${label}=DENIED"
    return 1
  fi
}

_gh_credential_precondition() {
  command -v gh >/dev/null 2>&1
}
_gh_credential_probe() {
  gh auth token
}

_state_dir_precondition() {
  [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ] && command -v ls >/dev/null 2>&1
}
_state_dir_probe() {
  ls -A "$STATE_DIR"
}

_operator_checkout_write_precondition() {
  [ -n "$CHECKOUT_DIR" ] && [ -d "$CHECKOUT_DIR/.git" ]
}
_operator_checkout_write_probe() {
  local f="$CHECKOUT_DIR/.git/.gate1-probe.$$"
  touch "$f" && rm -f "$f"
}

_network_precondition() {
  command -v curl >/dev/null 2>&1
}
_network_probe() {
  curl -sS -m 5 https://api.github.com/zen
}

DENIED_COUNT=0
INDETERMINATE_COUNT=0

_run_probe() {
  local label="$1" precondition_fn="$2" command_fn="$3" rc
  _probe "$label" "$precondition_fn" "$command_fn"
  rc=$?
  if [ "$rc" -eq 2 ]; then
    INDETERMINATE_COUNT=$((INDETERMINATE_COUNT + 1))
  elif [ "$rc" -ne 0 ]; then
    DENIED_COUNT=$((DENIED_COUNT + 1))
  fi
}

_run_probe "gh-credential" _gh_credential_precondition _gh_credential_probe
_run_probe "state-dir" _state_dir_precondition _state_dir_probe
_run_probe "operator-checkout-write" _operator_checkout_write_precondition _operator_checkout_write_probe
_run_probe "network" _network_precondition _network_probe

# An INDETERMINATE probe means the answer is unknown, not that the
# capability is unreachable — it must never be counted toward, or allowed
# to produce, CONTAINED. That is the whole point of the three-outcome fix.
if [ "$INDETERMINATE_COUNT" -gt 0 ]; then
  echo "gate1_containment_verdict=INDETERMINATE"
elif [ "$DENIED_COUNT" -eq 4 ]; then
  echo "gate1_containment_verdict=CONTAINED"
else
  echo "gate1_containment_verdict=UNCONTAINED"
fi

exit 0
