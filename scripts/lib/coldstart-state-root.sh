#!/usr/bin/env bash
# scripts/lib/coldstart-state-root.sh — one resolver for the directory a
# coldstart writes a project's state dir into.
#
# Source this, then call `coldstart_state_dir <project-name>`:
#
#   source "$SCRIPT_DIR/lib/coldstart-state-root.sh"
#   STATE_DIR="$(coldstart_state_dir "$PROJECT_NAME")"
#
# Resolution: ${COLDSTART_STATE_ROOT:-$HOME}/.<project-name>-state
#
# Why this exists (D#2317 PR-c). Both coldstart entry points hardcoded
# `$HOME/.<name>-state` with no override, so a test run had no way to
# redirect it. `tests/test_loop_metrics_path.sh` coldstarted a project named
# `test-proj-$$` on every run and then cleaned up `/tmp/${PROJECT_NAME}-state`
# — wrong root, and missing the leading dot — so it never once removed the
# `$HOME/.test-proj-<pid>-state` it actually created. That single suite is
# where 44 of the 75 dead fixture directories on the operator's Fleet page
# came from, at one directory per run, over about eight weeks.
#
# `AUTONOMOUS_TEAM_STATE_DIR` is deliberately NOT consulted here. Coldstart
# treats it as an *output* — it computes the state dir and exports that
# variable to its children so they agree on it. Reading it as an input too
# would make an operator's already-exported value silently redirect a
# coldstart of a *different* project into an existing project's state dir.
# `COLDSTART_STATE_ROOT` is a separate name precisely so the input direction
# and the output direction cannot be confused for each other.
#
# The default is `$HOME`, so operator behaviour is unchanged when the
# variable is unset.

# Root directory that coldstart state dirs are created under.
coldstart_state_root() {
    local root="${COLDSTART_STATE_ROOT:-$HOME}"
    if [[ "$root" != /* ]]; then
        echo "coldstart-state-root.sh: COLDSTART_STATE_ROOT must be an absolute path (got: '$root')" >&2
        return 1
    fi
    printf '%s' "$root"
}

# Absolute path of the state dir for a project name.
coldstart_state_dir() {
    local name="${1:?coldstart_state_dir: project name required}"
    local root
    root="$(coldstart_state_root)" || return 1
    printf '%s/.%s-state' "$root" "$name"
}
