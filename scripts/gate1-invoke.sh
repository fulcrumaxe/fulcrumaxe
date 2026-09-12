#!/usr/bin/env bash
# scripts/gate1-invoke.sh — caller-side wrapper around scripts/run-pr-tests.sh.
#
# This script provides NO SECURITY GAIN by itself when GATE1_RUNNER_UID is
# unset. It runs at the same uid as its caller in that case. Actual
# containment (a real uid boundary) is a host-provisioning gesture, tracked
# in wiki/Gate-1-Containment-Runbook.md, and is not part of this change.
#
# What this DOES do: separate which COPY of scripts/run-pr-tests.sh executes
# from which TREE it runs the test suites against, resolve the sha and
# changed-file list the runner needs WITHOUT the runner itself holding a
# `gh` credential, and write a receipt describing all of that — at the
# caller's uid, outside every tree — so `Gate 1: PASS` can be checked
# against an artifact instead of trusted as a self-report (D#2566).
#
# run-pr-tests.sh resolves its own working tree from $SCRIPT_DIR/.. by
# default — that is correct for every existing caller, but it means "run
# the suites inside a PR-head worktree" and "run the head's own copy of the
# runner" used to be the same action. This wrapper resolves the runner from
# ITS OWN path (${BASH_SOURCE[0]}, independent of --tree) and points it at
# an arbitrary tree via RUN_PR_TESTS_TREE_ROOT, so a caller that invokes
# this script by an absolute path into the operator's checkout always runs
# the operator's copy, no matter what --tree contains — even a --tree whose
# own scripts/ directory has been edited.
#
# The separation only holds if the CALLER invokes this file by an absolute
# path into the operator's checkout. A relative "scripts/gate1-invoke.sh"
# run from inside a head worktree resolves to the head's own copy of this
# file, welding runner-copy and tree back together exactly as before. This
# script cannot enforce that on itself — see scripts/gate1-verify-
# containment.sh, whose four probe verdicts get folded into every receipt
# this script writes, and which reports (does not gate) how little is
# actually denied today.
#
# Usage:
#   bash /abs/path/to/operator/checkout/scripts/gate1-invoke.sh \
#     --pr PR_NUMBER --tree /abs/path/to/pr-head/tree \
#     [--manifest-out PATH] [--pr-head-sha SHA] [--changed-files-from FILE] \
#     [--receipt-path-out PATH]
#
#   --manifest-out PATH        Where the runner's own manifest (`routing`,
#                               `tests_run`, `partial`, `measured_tree`)
#                               gets written, independent of stdout — a
#                               head-authored suite can still print
#                               arbitrary text to stdout (D#2566 item 3),
#                               and the receipt must never be built from
#                               that shared stream. Defaults to a private
#                               temp file when omitted; the receipt's
#                               `head_reported` is always read back from
#                               here, never from stdout.
#   --pr-head-sha SHA           Skip this wrapper's own `gh pr view` call
#                               and use SHA directly. Still validated
#                               against ^[0-9a-f]{40}$ before it becomes
#                               part of a receipt path (CWE-22) — this is
#                               also how D#2566 item 11 is exercised.
#   --changed-files-from FILE   Skip this wrapper's own `gh pr diff` call
#                               and read the changed-file list from FILE
#                               (one path per line) instead.
#   --receipt-path-out PATH     On success, also write the written receipt's
#                               path to PATH (one line, nothing else). A
#                               caller that needs the path programmatically
#                               must not grep it out of stdout OR a log
#                               capturing this script's stderr — both can
#                               also carry head-authored suite output, and
#                               a forged `gate1_receipt_path=` line in a
#                               contended stream is exactly the shape
#                               --manifest-out exists to avoid for the
#                               manifest itself (security review, D#2566).
#                               Left untouched when omitted, or when no
#                               receipt was written.
#
# Env:
#   GATE1_RUNNER_UID  Optional. Read from the process environment only —
#                     never derived from anything under --tree. When set,
#                     the suites are invoked via
#                     `sudo -u "$GATE1_RUNNER_UID" --preserve-env=RUN_PR_TESTS_TREE_ROOT
#                      "$RUNNER" "$PR_NUMBER" ...` — the authorised command
#                     is the runner script's own path, invoked directly
#                     rather than via `bash "$RUNNER"` (a sudoers Cmnd
#                     naming a specific bash binary can break when that
#                     binary resolves to a different path than the one the
#                     rule was written against); not `env` or anything else
#                     either — the tree root reaches it via sudo's own
#                     env-preservation, which requires the target uid's
#                     sudoers policy to `env_keep` that one variable name
#                     (see wiki/Gate-1-Containment-Runbook.md). If that
#                     user does not exist on this host, this script exits
#                     non-zero and runs NO suite at all — it never falls
#                     back to same-uid silently. When unset (today's
#                     default on this host), the suites run same-uid and
#                     that is reported honestly, both on stderr and in the
#                     receipt, as "gate1_containment=NONE (same-uid)".
#   GATE1_NO_GH       Optional, same-uid arm only. When "1", the immediate
#                     child (the runner invocation) is given a PATH with
#                     every directory containing a `gh` executable removed,
#                     so a run using --pr-head-sha and --changed-files-from
#                     can be shown reaching no `gh` credential even without
#                     a provisioned containment uid. This wrapper's own
#                     resolution of pr_head_sha / changed-files (when NOT
#                     supplied via flags) still uses the real PATH — the
#                     stripping applies only to the process that actually
#                     runs the suites. Not applied to the contained (sudo)
#                     arm: introducing `env` into that Cmnd to rewrite PATH
#                     would defeat the reason this wrapper avoids `env` in
#                     the sudoers Cmnd in the first place (see below); the
#                     sudo arm's own uid boundary is what denies `gh` there.
#
# Output:
#   stdout — exactly what the invoked run-pr-tests.sh writes to stdout (the
#            routing/tests_run JSON manifest, and any head-authored suite
#            output), untouched, so an existing caller doing
#            `TESTS_JSON=$(...)` keeps working unmodified. The receipt is
#            never built from this stream — see --manifest-out above.
#   stderr — gate1_runner_copy=<path>, gate1_tree_root=<path>,
#            gate1_containment=<state>, gate1_receipt_path=<path> (or a
#            one-line failure reason if the receipt could not be written),
#            plus whatever run-pr-tests.sh and gate1-verify-containment.sh
#            themselves write to stderr/stdout.
#
# Exit code is exactly whatever the invoked run-pr-tests.sh returns, EXCEPT
# for a usage error or an invalid/unresolvable pr_head_sha, both of which
# exit non-zero before the runner is ever invoked and before any receipt
# directory is touched (D#2566 item 11). A receipt-write failure AFTER the
# runner has already run is reported on stderr but does not change the
# exit code — this script's contract for existing callers is the runner's
# exit code, unconditionally; the receipt is new, additive behaviour.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$SCRIPT_DIR/run-pr-tests.sh"
CONTAINMENT_SCRIPT="$SCRIPT_DIR/gate1-verify-containment.sh"

# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# shellcheck source=scripts/lib/gate1-receipt.sh
source "$SCRIPT_DIR/lib/gate1-receipt.sh"

PR_NUMBER=""
TREE_ROOT=""
MANIFEST_OUT_ARG=""
PR_HEAD_SHA_ARG=""
CHANGED_FILES_FROM_ARG=""
RECEIPT_PATH_OUT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pr)
      PR_NUMBER="${2:-}"
      shift 2
      ;;
    --tree)
      TREE_ROOT="${2:-}"
      shift 2
      ;;
    --manifest-out)
      MANIFEST_OUT_ARG="${2:-}"
      shift 2
      ;;
    --pr-head-sha)
      PR_HEAD_SHA_ARG="${2:-}"
      shift 2
      ;;
    --changed-files-from)
      CHANGED_FILES_FROM_ARG="${2:-}"
      shift 2
      ;;
    --receipt-path-out)
      RECEIPT_PATH_OUT_ARG="${2:-}"
      shift 2
      ;;
    *)
      echo "gate1-invoke: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ -z "$PR_NUMBER" ] || [ -z "$TREE_ROOT" ]; then
  echo "Usage: $0 --pr PR_NUMBER --tree TREE_ROOT [--manifest-out PATH] [--pr-head-sha SHA] [--changed-files-from FILE] [--receipt-path-out PATH]" >&2
  exit 1
fi

# --pr must be a bare number: quoting elsewhere prevents shell injection, but
# a value starting with "-" would still reach `gh pr view` inside this
# wrapper (or run-pr-tests.sh) as a flag rather than an argument.
if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "gate1-invoke: --pr must be a positive integer, got: $PR_NUMBER" >&2
  exit 1
fi

if [ ! -d "$TREE_ROOT" ]; then
  echo "gate1-invoke: --tree $TREE_ROOT does not exist or is not a directory" >&2
  exit 1
fi
TREE_ROOT="$(cd "$TREE_ROOT" && pwd)"

if [ ! -f "$RUNNER" ]; then
  echo "gate1-invoke: resolved runner copy $RUNNER does not exist" >&2
  exit 1
fi
if [ ! -f "$CONTAINMENT_SCRIPT" ]; then
  echo "gate1-invoke: resolved containment verifier $CONTAINMENT_SCRIPT does not exist" >&2
  exit 1
fi

echo "gate1_runner_copy=$RUNNER" >&2
echo "gate1_tree_root=$TREE_ROOT" >&2

REPO="$(_require_code_repo "gate1-invoke")" || exit 1

# Resolve pr_head_sha and the changed-file list CALLER-SIDE (D#2566 change
# 2): a contained uid holds no `gh` credential, so these can no longer be
# resolved by run-pr-tests.sh itself on the path a caller wants contained.
# Narrow claim, on purpose: this removes the RUNNER's *need* for a `gh`
# credential on this path. It does not remove head-authored code's *reach*
# to one under GATE1_RUNNER_UID unset — same-uid, that code can still run
# `gh auth token` directly and get a live token (the credential lives in
# the system keyring, not a dotfile PATH/HOME games can hide — see
# gate1-verify-containment.sh's own note on this). Only a provisioned
# GATE1_RUNNER_UID closes the reach; see the top-of-file comment.
if [ -n "$PR_HEAD_SHA_ARG" ]; then
  PR_HEAD_SHA="$PR_HEAD_SHA_ARG"
else
  PR_HEAD_SHA="$(gh pr view "$PR_NUMBER" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
fi

if [ -z "$PR_HEAD_SHA" ]; then
  echo "gate1-invoke: could not resolve PR #$PR_NUMBER's head sha (gh pr view --repo $REPO --json headRefOid, or pass --pr-head-sha) -- refusing to run any suite or write a receipt" >&2
  exit 1
fi

# CWE-22: validated BEFORE it becomes any part of a filesystem path, and
# before any receipts directory is touched or the runner is invoked at all
# — a bad sha must leave no trace anywhere outside a normal failure message
# (D#2566 item 11).
if ! gate1_receipt_validate_sha "$PR_HEAD_SHA"; then
  echo "gate1-invoke: refusing — pr_head_sha does not match ^[0-9a-f]{40}\$: $PR_HEAD_SHA" >&2
  exit 1
fi

_CHANGED_FILES_IS_TEMP=false
if [ -n "$CHANGED_FILES_FROM_ARG" ]; then
  if [ ! -f "$CHANGED_FILES_FROM_ARG" ]; then
    echo "gate1-invoke: --changed-files-from $CHANGED_FILES_FROM_ARG does not exist" >&2
    exit 1
  fi
  CHANGED_FILES_FROM="$CHANGED_FILES_FROM_ARG"
else
  CHANGED_FILES_FROM="$(mktemp)"
  _CHANGED_FILES_IS_TEMP=true
  gh pr diff "$PR_NUMBER" --repo "$REPO" --name-only >"$CHANGED_FILES_FROM" 2>/dev/null || true
  if [ ! -s "$CHANGED_FILES_FROM" ]; then
    gh pr view "$PR_NUMBER" --repo "$REPO" --json files --jq '[.files[].path] | .[]' >"$CHANGED_FILES_FROM" 2>/dev/null || true
  fi
fi

_MANIFEST_IS_TEMP=false
if [ -n "$MANIFEST_OUT_ARG" ]; then
  MANIFEST_PATH="$MANIFEST_OUT_ARG"
else
  MANIFEST_PATH="$(mktemp)"
  _MANIFEST_IS_TEMP=true
fi

# Containment reporting (D#2566 change 4): shell out to the existing
# reporter and fold its four probe verdicts + verdict line into the
# receipt. This script never rewrites gate1-verify-containment.sh's own
# logic — it only reads its output.
CONTAINMENT_OUTPUT="$(bash "$CONTAINMENT_SCRIPT" 2>&1)" || true
PROBES_JSON="$(printf '%s\n' "$CONTAINMENT_OUTPUT" | python3 -c '
import re, sys, json
probes = {}
for line in sys.stdin:
    m = re.match(r"gate1_probe\s+([A-Za-z0-9_-]+)=(\S+)", line.strip())
    if m:
        probes[m.group(1)] = m.group(2)
print(json.dumps(probes))
')"
CONTAINMENT_VERDICT="$(printf '%s\n' "$CONTAINMENT_OUTPUT" | grep -oE '^gate1_containment_verdict=.*' | tail -1 | cut -d= -f2-)"
if [ -z "$CONTAINMENT_VERDICT" ]; then
  CONTAINMENT_VERDICT="INDETERMINATE"
fi

# Identity is caller-imposed and read from the process environment only — no
# path under $TREE_ROOT ever resolves this value (D#2560 item 5).
RUNNER_UID="${GATE1_RUNNER_UID:-}"

# Environment block (D#2566 constraint 4): which seed files this tree had,
# and which of the two plane-sensitive variables were set for this run.
# Snapshotted BEFORE the runner is invoked below — every other `caller`
# field is either computed by the caller's own tooling (repo-resolve.sh,
# gate1-verify-containment.sh) or fixed before the runner starts, and this
# must be no different: a head-authored suite that creates or removes
# .autonomous-team/*.json mid-run must never be able to move a `caller`
# field after the fact (security review, D#2566). Checked against
# $TREE_ROOT — the tree the suites are about to run in — not against this
# wrapper's own checkout.
SEED_CONFIG_PRESENT=false
[ -f "$TREE_ROOT/.autonomous-team/config.json" ] && SEED_CONFIG_PRESENT=true
SEED_PROJECT_PRESENT=false
[ -f "$TREE_ROOT/.autonomous-team/project.json" ] && SEED_PROJECT_PRESENT=true
STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"
ENV_JSON="$(python3 -c '
import json, sys
print(json.dumps({
    "AUTONOMOUS_TEAM_REPO": sys.argv[1],
    "AUTONOMOUS_TEAM_STATE_DIR": sys.argv[2],
    "seed_files": {
        ".autonomous-team/config.json": sys.argv[3] == "true",
        ".autonomous-team/project.json": sys.argv[4] == "true",
    },
}))
' "${AUTONOMOUS_TEAM_REPO:-}" "$STATE_DIR" "$SEED_CONFIG_PRESENT" "$SEED_PROJECT_PRESENT")"

RUNNER_ARGS=("$PR_NUMBER" --manifest-out "$MANIFEST_PATH" --pr-head-sha "$PR_HEAD_SHA" --changed-files-from "$CHANGED_FILES_FROM")

# _gate1_strip_gh_from_path — prints a PATH with every directory containing
# an executable named `gh` removed. Used only to exercise, for verification,
# that the runner needs no `gh` credential once given --pr-head-sha and
# --changed-files-from — see GATE1_NO_GH above.
_gate1_strip_gh_from_path() {
  local dir out=""
  local IFS=:
  for dir in $PATH; do
    [ -x "$dir/gh" ] && continue
    if [ -z "$out" ]; then out="$dir"; else out="$out:$dir"; fi
  done
  printf '%s' "$out"
}

set +e
if [ -z "$RUNNER_UID" ]; then
  echo "gate1_containment=NONE (same-uid)" >&2
  CONTAINMENT_MODE="NONE (same-uid)"
  if [ "${GATE1_NO_GH:-}" = "1" ]; then
    RUN_PR_TESTS_TREE_ROOT="$TREE_ROOT" PATH="$(_gate1_strip_gh_from_path)" "$RUNNER" "${RUNNER_ARGS[@]}"
  else
    RUN_PR_TESTS_TREE_ROOT="$TREE_ROOT" "$RUNNER" "${RUNNER_ARGS[@]}"
  fi
  rc=$?
else
  # GATE1_RUNNER_UID set: fail closed if the named user is absent on this
  # host. Never silently fall back to same-uid — an operator who set this
  # variable expecting containment must not get a same-uid run that looks
  # identical to one.
  if ! id -u "$RUNNER_UID" >/dev/null 2>&1; then
    set -e
    echo "gate1-invoke: GATE1_RUNNER_UID=$RUNNER_UID has no matching user on this host — refusing to run any suite (fail closed, not same-uid fallback). Provision the user per wiki/Gate-1-Containment-Runbook.md first." >&2
    [ "$_CHANGED_FILES_IS_TEMP" = "true" ] && rm -f "$CHANGED_FILES_FROM"
    [ "$_MANIFEST_IS_TEMP" = "true" ] && rm -f "$MANIFEST_PATH"
    exit 1
  fi
  echo "gate1_containment=UID($RUNNER_UID)" >&2
  CONTAINMENT_MODE="UID($RUNNER_UID)"
  # Exported (not passed via `env VAR=val ...`) so the command sudo is asked
  # to authorise stays a fixed, literal sudoers Cmnd target — see the
  # header comment on why `env` never appears in this Cmnd. No `exec` here
  # (D#2566 change 1): this process must survive the run to write a
  # receipt.
  export RUN_PR_TESTS_TREE_ROOT="$TREE_ROOT"
  sudo -u "$RUNNER_UID" --preserve-env=RUN_PR_TESTS_TREE_ROOT "$RUNNER" "${RUNNER_ARGS[@]}"
  rc=$?
fi
set -e

if [ -f "$MANIFEST_PATH" ]; then
  if RECEIPT_PATH="$(gate1_receipt_write "$STATE_DIR" "$REPO" "$PR_NUMBER" "$PR_HEAD_SHA" \
        "$TREE_ROOT" "$RUNNER" "$CONTAINMENT_MODE" "$PROBES_JSON" "$CONTAINMENT_VERDICT" \
        "$ENV_JSON" "$MANIFEST_PATH")"; then
    echo "gate1_receipt_path=$RECEIPT_PATH" >&2
    # A caller needing the path programmatically must not have to grep it
    # out of a stream that can also carry head-authored suite output (this
    # wrapper's own stdout, and any log a caller redirects our stderr
    # into) — that is exactly the shape --manifest-out exists to avoid for
    # the manifest itself, and the receipt path deserves the same
    # dedicated channel rather than an incidental one (security review,
    # D#2566).
    if [ -n "$RECEIPT_PATH_OUT_ARG" ]; then
      printf '%s\n' "$RECEIPT_PATH" > "$RECEIPT_PATH_OUT_ARG"
    fi
  else
    echo "gate1-invoke: receipt write failed — see the reason above on stderr (runner exit was $rc; this does not change that exit code)" >&2
  fi
else
  echo "gate1-invoke: no manifest produced at $MANIFEST_PATH (runner exit $rc) — skipping receipt write" >&2
fi

[ "$_CHANGED_FILES_IS_TEMP" = "true" ] && rm -f "$CHANGED_FILES_FROM"
[ "$_MANIFEST_IS_TEMP" = "true" ] && rm -f "$MANIFEST_PATH"

exit "$rc"
