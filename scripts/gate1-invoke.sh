#!/usr/bin/env bash
# scripts/gate1-invoke.sh — caller-side wrapper around scripts/run-pr-tests.sh.
#
# This script provides NO SECURITY GAIN by itself. It runs at the same uid as
# its caller. Actual containment (a real uid boundary) is a host-provisioning
# gesture, tracked in wiki/Gate-1-Containment-Runbook.md, and is not part of
# this change.
#
# What this DOES do: separate which COPY of scripts/run-pr-tests.sh executes
# from which TREE it runs the test suites against. run-pr-tests.sh resolves
# its own working tree from $SCRIPT_DIR/.. by default — that is correct for
# every existing caller, but it means "run the suites inside a PR-head
# worktree" and "run the head's own copy of the runner" used to be the same
# action. This wrapper resolves the runner from ITS OWN path
# (${BASH_SOURCE[0]}, independent of --tree) and points it at an arbitrary
# tree via RUN_PR_TESTS_TREE_ROOT, so a caller that invokes this script by an
# absolute path into the operator's checkout always runs the operator's copy,
# no matter what --tree contains — even a --tree whose own scripts/
# directory has been edited.
#
# The separation only holds if the CALLER invokes this file by an absolute
# path into the operator's checkout. A relative "scripts/gate1-invoke.sh"
# run from inside a head worktree resolves to the head's own copy of this
# file, welding runner-copy and tree back together exactly as before. This
# script cannot enforce that on itself — see
# scripts/gate1-verify-containment.sh, which reports (does not gate) how
# little is actually denied today.
#
# Usage:
#   bash /abs/path/to/operator/checkout/scripts/gate1-invoke.sh \
#     --pr PR_NUMBER --tree /abs/path/to/pr-head/tree
#
# Env:
#   GATE1_RUNNER_UID  Optional. Read from the process environment only —
#                     never derived from anything under --tree. When set,
#                     the suites are invoked via
#                     `sudo -u "$GATE1_RUNNER_UID" --preserve-env=RUN_PR_TESTS_TREE_ROOT
#                      "$RUNNER" "$PR_NUMBER"` — the authorised command is
#                     the runner script's own path, invoked directly rather
#                     than via `bash "$RUNNER"` (a sudoers Cmnd naming a
#                     specific bash binary can break when that binary
#                     resolves to a different path than the one the rule
#                     was written against); not `env` or anything else
#                     either — the tree root reaches it via sudo's own
#                     env-preservation, which requires the target uid's
#                     sudoers policy to `env_keep` that one variable name
#                     (see wiki/Gate-1-Containment-Runbook.md). If that user
#                     does not exist on this host, this script exits
#                     non-zero and runs NO suite at all — it never falls
#                     back to same-uid silently. When unset (today's default
#                     on this host), the suites run same-uid and that is
#                     reported honestly on stderr as
#                     "gate1_containment=NONE (same-uid)".
#
# Output:
#   stdout — exactly what the invoked run-pr-tests.sh writes to stdout (the
#            routing/tests_run JSON manifest), untouched, so an existing
#            caller doing `TESTS_JSON=$(...)` keeps working unmodified.
#   stderr — gate1_runner_copy=<path>, gate1_tree_root=<path>,
#            gate1_containment=<state>, plus whatever run-pr-tests.sh itself
#            writes to stderr.
#
# Exit code is exactly whatever the invoked run-pr-tests.sh (or, when
# GATE1_RUNNER_UID is set and absent, this script's own fail-closed check)
# returns.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$SCRIPT_DIR/run-pr-tests.sh"

PR_NUMBER=""
TREE_ROOT=""
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
    *)
      echo "gate1-invoke: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ -z "$PR_NUMBER" ] || [ -z "$TREE_ROOT" ]; then
  echo "Usage: $0 --pr PR_NUMBER --tree TREE_ROOT" >&2
  exit 1
fi

# --pr must be a bare number: quoting elsewhere prevents shell injection, but
# a value starting with "-" would still reach `gh pr view` inside
# run-pr-tests.sh as a flag rather than an argument.
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

echo "gate1_runner_copy=$RUNNER" >&2
echo "gate1_tree_root=$TREE_ROOT" >&2

# Identity is caller-imposed and read from the process environment only — no
# path under $TREE_ROOT ever resolves this value (D#2560 item 5).
RUNNER_UID="${GATE1_RUNNER_UID:-}"

if [ -z "$RUNNER_UID" ]; then
  echo "gate1_containment=NONE (same-uid)" >&2
  RUN_PR_TESTS_TREE_ROOT="$TREE_ROOT" exec "$RUNNER" "$PR_NUMBER"
fi

# GATE1_RUNNER_UID set: fail closed if the named user is absent on this host.
# Never silently fall back to same-uid — an operator who set this variable
# expecting containment must not get a same-uid run that looks identical to
# one.
if ! id -u "$RUNNER_UID" >/dev/null 2>&1; then
  echo "gate1-invoke: GATE1_RUNNER_UID=$RUNNER_UID has no matching user on this host — refusing to run any suite (fail closed, not same-uid fallback). Provision the user per wiki/Gate-1-Containment-Runbook.md first." >&2
  exit 1
fi

echo "gate1_containment=UID($RUNNER_UID)" >&2
# Exported (not passed via `env VAR=val ...`) so the command sudo is asked to
# authorise is exactly `"$RUNNER" "$PR_NUMBER"` — a fixed, literal sudoers
# Cmnd target. Routing the tree root through `env` instead would put
# `/usr/bin/env` itself in the Cmnd, and a Cmnd that permits `env` with
# arbitrary arguments permits arbitrary execution as that user (see the
# runbook). `--preserve-env` only lets this one named variable survive
# sudo's env_reset; it still requires the target's sudoers policy to
# `env_keep` (or SETENV) that exact name — see
# wiki/Gate-1-Containment-Runbook.md.
#
# $RUNNER is invoked directly, NOT via `bash "$RUNNER"`: run-pr-tests.sh is
# mode 755 with its own `#!/usr/bin/env bash` shebang, and sudo matches its
# Cmnd against the argv it is directly asked to run, not against whatever
# the kernel resolves the shebang's interpreter to. Naming `bash` in both
# the invocation and the sudoers Cmnd binds the rule to one specific bash
# binary's resolved path — which can differ between the interactive shell's
# PATH and the review lane's, even on the same host, when the two resolve
# `bash` through different symlinks. Invoking the script's own path removes
# that binary entirely from what sudo needs to match, so the rule only ever
# needs to name the script.
export RUN_PR_TESTS_TREE_ROOT="$TREE_ROOT"
exec sudo -u "$RUNNER_UID" --preserve-env=RUN_PR_TESTS_TREE_ROOT "$RUNNER" "$PR_NUMBER"
