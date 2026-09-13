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
#   INDETERMINATE  either the probe's own precondition failed (missing
#                  binary, missing target) — it never got to test anything
#                  — or the probe ran, its command failed, and the failure
#                  is one a per-probe classifier recognizes as unable to
#                  tell a real denial from a transient fault (D#2584): the
#                  network probe's curl exiting one of its transport-
#                  failure codes is the only case of this today. A probe
#                  with no classifier keeps the plain
#                  two-way DENIED/NOT-DENIED split on its command's exit
#                  code.
# Any INDETERMINATE probe forces the verdict to INDETERMINATE. It can never
# read as CONTAINED — an unrun probe, or one whose result can't be trusted,
# must never look like a passing one.
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
#                             curl's exit code can't tell a firewall from a
#                             fault (drop=28, reject=7, blocked resolver=6,
#                             SNI/DPI filtering or a reset against an
#                             established connection=35/56, each with an
#                             innocent timeout/blip/TLS-hiccup twin), so
#                             this probe alone carries a transient-exit
#                             classifier covering curl's transport-failure
#                             codes (see _network_transient_classifier for
#                             the full list) — those read INDETERMINATE,
#                             not DENIED. A genuine egress denial therefore
#                             now reads INDETERMINATE too, not CONTAINED —
#                             that is intended, not a regression (D#2584);
#                             a positive network signal is out of scope
#                             here.
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

# _probe LABEL PRECONDITION_FN COMMAND_FN [TRANSIENT_CLASSIFIER_FN] — checks
# PRECONDITION_FN first, entirely separately from COMMAND_FN's own exit
# code. This split matters: several real commands (GNU `ls`, for one)
# already use exit code 2 for their own unrelated failure modes (e.g.
# "Permission denied" on `ls -A` against an unreadable directory
# legitimately exits 2), so a design that tried to smuggle "precondition
# failed" through the same exit-code channel as "the command ran and
# failed" would misread that real denial as INDETERMINATE. Precondition and
# outcome are checked in two separate steps so neither can be confused with
# the other:
#   PRECONDITION_FN fails -> INDETERMINATE, COMMAND_FN never runs.
#   PRECONDITION_FN passes, COMMAND_FN exits 0       -> NOT-DENIED.
#   PRECONDITION_FN passes, COMMAND_FN exits nonzero, no classifier,
#     or classifier says "not transient"            -> DENIED.
#   PRECONDITION_FN passes, COMMAND_FN exits nonzero, classifier says
#     "transient" (prints an annotation, exit 0)     -> INDETERMINATE.
#
# TRANSIENT_CLASSIFIER_FN is optional and per-probe by design — it is NOT a
# blanket exit-code rule inside this function. Given the command's exit
# code as $1, it either prints a short annotation ("curl exit 28") and
# returns 0 (this failure is transient — a fault, not a denial), or prints
# nothing and returns nonzero (this failure is a real DENIED). A probe
# passed no classifier keeps today's exact two-way behaviour. This is
# deliberate: `ls -A` on a `chmod 000` directory exits 2 for a genuine
# denial, so a shared "exit N -> INDETERMINATE" rule here would misread
# that as INDETERMINATE for every probe, not just the one it was meant
# for (D#2584).
#
# CLASSIFIER_FN's own exit status is the transient/not-transient signal,
# which means an unresolvable function name (a typo, a rename that missed
# this call site) would fail the command substitution below with bash's
# "command not found", exit 127 — indistinguishable from "not transient"
# without the explicit `declare -F` check just below, and silently
# restoring the exact pre-fix DENIED behaviour with no diagnostic. So a
# classifier that cannot be invoked reads INDETERMINATE, the same as any
# other "the answer is unknown" case, never DENIED.
#
# Prints one line and returns 0 (NOT-DENIED), 1 (DENIED), or 2
# (INDETERMINATE) so the caller can aggregate it.
_probe() {
  local label="$1" precondition_fn="$2" command_fn="$3" classifier_fn="${4:-}"
  local rc annotation
  if ! "$precondition_fn" >/dev/null 2>&1; then
    echo "gate1_probe ${label}=INDETERMINATE"
    return 2
  fi
  "$command_fn" >/dev/null 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "gate1_probe ${label}=NOT-DENIED"
    return 0
  fi
  if [ -n "$classifier_fn" ]; then
    if ! declare -F "$classifier_fn" >/dev/null 2>&1; then
      echo "gate1_probe ${label}=INDETERMINATE (classifier ${classifier_fn} not defined)"
      return 2
    fi
    if annotation="$("$classifier_fn" "$rc")"; then
      echo "gate1_probe ${label}=INDETERMINATE (${annotation})"
      return 2
    fi
  fi
  echo "gate1_probe ${label}=DENIED"
  return 1
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
# _network_transient_classifier — curl's exit code cannot distinguish a
# firewall from a fault for this probe's fixed, well-formed `curl -sS -m 5
# https://...` invocation (no --fail, so a live HTTP response of any status
# still exits 0 — every nonzero exit here comes from the transport layer,
# not from the HTTP exchange). An nftables `drop` produces exit 28 (the
# same as a WAN timeout), `reject` produces exit 7 (the same as any
# refused connection), a blocked resolver produces exit 6 (the same as a
# DNS blip) — and SNI/DPI-based egress filtering, or a `reject with tcp
# reset` against an already-established connection, surfaces as exit 35 or
# 56, not 7 or 28. Each has an innocent twin, so none of curl's transport-
# failure codes can be trusted as DENIED — they read INDETERMINATE instead
# (D#2584):
#   5   couldn't resolve proxy (the direct analogue of 6, for a proxy)
#   6   couldn't resolve host (DNS)
#   7   couldn't connect (refused)
#   16  HTTP/2 framing layer problem
#   18  partial file / transfer closed with outstanding read data
#   28  operation timed out
#   35  SSL/TLS connect error
#   52  server returned nothing (empty reply)
#   55  failed sending network data
#   56  failure receiving network data
#   60  SSL certificate problem (peer cert failed verification)
#   77  problem reading the SSL CA cert (local file/path)
#   92  HTTP/2 stream error
# 60 and 77 are included even though they don't fire under the specific
# egress rule this Discussion measured against (an nftables drop/reject) —
# a TLS-terminating/inspecting proxy enforcing egress policy surfaces as
# exactly 60 (it presents its own cert, which fails the pinned/expected
# verification), and both still have innocent twins (a stale local CA
# bundle, an unreadable CA path on a freshly-provisioned uid) that this
# probe cannot rule out from the exit code alone — the same ambiguity as
# every other code in this list, just against a different possible egress
# mechanism than the one on hand today.
# Exit 1 (unsupported protocol) is deliberately NOT in this list: it is
# curl's own client-side misuse code, not a network outcome, which is
# exactly why the test suite uses it as the synthetic "tool is broken, not
# firewalled" stand-in (test 5) — it must keep reading DENIED. Any other,
# unenumerated nonzero exit also stays DENIED by default; a future curl
# adding new transport-failure codes should extend this list, but the
# default direction for an exit this classifier has no explicit opinion
# about is unchanged from before this fix. This also means a genuine
# egress denial now reads INDETERMINATE, not CONTAINED: designing a
# positive network signal that can tell the difference is out of scope
# here (see D#2560).
_network_transient_classifier() {
  local rc="$1"
  case "$rc" in
    5|6|7|16|18|28|35|52|55|56|60|77|92) echo "curl exit ${rc}"; return 0 ;;
    *) return 1 ;;
  esac
}

DENIED_COUNT=0
INDETERMINATE_COUNT=0

_run_probe() {
  local label="$1" precondition_fn="$2" command_fn="$3" classifier_fn="${4:-}" rc
  _probe "$label" "$precondition_fn" "$command_fn" "$classifier_fn"
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
_run_probe "network" _network_precondition _network_probe _network_transient_classifier

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
