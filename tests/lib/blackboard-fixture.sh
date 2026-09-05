#!/usr/bin/env bash
# tests/lib/blackboard-fixture.sh — resolve the blackboard pr_state directory
# that the code under test will actually read, regardless of whether
# AUTONOMOUS_TEAM_STATE_DIR is exported. (D#2119)
#
# backend/blackboard.py's _resolve_default_root() picks one of two roots:
#   1. AUTONOMOUS_TEAM_STATE_DIR set  -> <STATE_DIR>/blackboard
#   2. unset                          -> ~/.autonomous-forever-state/blackboard
#
# Several bash suites write pr_state fixtures. Getting the root wrong moves
# the *reader* to one branch while the fixture stays behind at the other, so
# the suite reports failures that are actually a fixture in the wrong place,
# not a defect in the code under test.
#
# This helper asks the real resolver instead of reimplementing its
# precedence a second time in bash (that second copy is the bug D#2119 is
# about). It shells out to the public backend/state_paths.py API once per
# suite — callers should resolve it into a variable at suite setup, not once
# per fixture write, since each call is a python3 spawn.
#
# BLACKBOARD_DIR is resolved at call time via PEP 562 module __getattr__
# (backend/state_paths.py), so it tracks an AUTONOMOUS_TEAM_STATE_DIR
# override rather than freezing it. backend/blackboard.py's own
# _resolve_default_root() is literally `from backend.state_paths import
# BLACKBOARD_DIR; return BLACKBOARD_DIR` — this helper evaluates the same
# expression the code under test evaluates, not a parallel reimplementation.
#
# Usage:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/blackboard-fixture.sh"
#   BB_PR_STATE_DIR="$(blackboard_pr_state_dir "$REPO_ROOT")" || exit 1

blackboard_pr_state_dir() {
  local repo_root="${1:?blackboard_pr_state_dir: repo root argument required}"
  local root
  root="$(cd "$repo_root" && python3 -c '
import sys
sys.path.insert(0, ".")
from backend.state_paths import BLACKBOARD_DIR
print(BLACKBOARD_DIR)
' 2>&1)" || {
    printf 'tests/lib/blackboard-fixture.sh: could not resolve the blackboard root via backend.state_paths.BLACKBOARD_DIR from %s\n%s\n' "$repo_root" "$root" >&2
    return 1
  }
  printf '%s/pr_state\n' "$root"
}

# blackboard_scratch_state_dir — create a per-run scratch directory and
# export AUTONOMOUS_TEAM_STATE_DIR to point at it, then print the path.
#
# This is what actually keeps synthetic pr_state rows and audit.jsonl lines
# out of the production state dir (D#2283): a killed run now leaks into this
# scratch dir instead of ~/.autonomous-forever-state, where a leak is
# harmless. A `trap`-based cleanup is hygiene on top of this, not a
# substitute for it — SIGKILL never runs a trap, so only redirecting the
# state dir closes the hole on every exit path, clean or crashed.
#
# Because blackboard_pr_state_dir() and backend/blackboard.py's own
# _resolve_default_root() both resolve BLACKBOARD_DIR at call time (no
# import-time freeze anywhere in the chain), calling this *before*
# resolving BB_PR_STATE_DIR is the only ordering requirement — reader and
# fixture then move together.
#
# Does NOT register a trap. Callers that already have one must fold the
# cleanup into it by hand (see tests/test_hook_caller_failure_surfacing.sh
# and tests/test_loop_phased_step5.sh) rather than sourcing a second trap
# here, which would silently replace whatever trap a caller already had.
#
# Must be called directly, NOT via command substitution — `export` inside a
# function invoked as `x=$(fn)` runs in a subshell and the export is lost
# the instant that subshell exits, which would silently leave the caller's
# AUTONOMOUS_TEAM_STATE_DIR unset and defeat the whole point of this helper.
# The scratch path is exposed back to the caller as AUTONOMOUS_TEAM_STATE_DIR
# itself, which the direct call already leaves set in the caller's shell.
#
# Usage:
#   blackboard_scratch_state_dir || exit 1
#   SCRATCH_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
#   trap 'rm -rf "$SCRATCH_STATE_DIR"' EXIT
#   BB_PR_STATE_DIR="$(blackboard_pr_state_dir "$REPO_ROOT")" || exit 1
blackboard_scratch_state_dir() {
  local dir
  dir="$(mktemp -d)" || return 1
  export AUTONOMOUS_TEAM_STATE_DIR="$dir"
}
