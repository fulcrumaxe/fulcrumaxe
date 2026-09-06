#!/usr/bin/env bash
# scripts/hooks/post-agent.d/anomaly-check.sh
#
# Runs the stat regression detector after each agent completes.
# Flags any metric that swings >10x (configurable per metric) between the
# two most recent readings in stats.duckdb.
#
# Environment:
#   _REPO         — "owner/name" slug for team-log comments (optional)
#
# REPO_ROOT is NOT read from the environment any more. It is derived from
# BASH_SOURCE below. post-agent-hook.sh does set a REPO_ROOT and this file is
# sourced, not executed, so that variable is in fact in scope on that one path
# — but it describes the caller's tree, and the tree this file needs is its
# own. BASH_SOURCE gives that on every path, including a bare invocation.
#
# Why any of this matters: `python3 -m backend...` resolves `backend` from
# sys.path[0], which is the *working directory*. Sourced from
# post-agent-hook.sh, this file inherits the finishing agent's cwd, and that
# script never cd's to its own root (its two `cd`s at :26-27 are inside command
# substitutions, so they set variables and move nothing). For an executor the
# inherited cwd is its worktree. `backend` is a namespace package — no
# __init__.py — so a worktree cwd does not even error: the import resolves
# against the finishing agent's own branch, in whatever state that tree was
# left. From a cwd with no backend/ it fails outright. Neither outcome was
# visible, because the call ended in `2>/dev/null || true`.
#
# The failure line below goes to STDOUT, deliberately. post-agent-hook.sh
# sources this file as `source ... 2>/dev/null || true`, so anything written to
# stderr here is discarded by the caller before it can reach anyone. Its stdout
# is merged and captured (subagent-stop-hook.sh:303 tees the whole hook into
# /tmp/post-agent-hook-<event-id>.err), so stdout is the channel that survives.
#
# Non-fatal by construction: the body runs in a subshell, so the `exit`s below
# end that subshell and never the hook that sourced this file.

(
    _acheck_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 0
    _acheck_root="$(cd "$_acheck_dir/../../.." && pwd)" || exit 0

    command -v python3 >/dev/null 2>&1 || exit 0

    # Hoisted once, at the top of the subshell that holds every command in this
    # file, rather than prefixed onto the one call below — a python3 call added
    # here later is then correct without anyone having to remember. The
    # subshell is what keeps it from leaking: a bare `export` in a sourced file
    # lands in post-agent-hook.sh's own environment and in every subprocess it
    # starts afterwards.
    #
    # Prepend, never overwrite: on the operator host PYTHONPATH already carries
    # the interpreter's site-packages, and clobbering it costs this call the
    # duckdb import that the detector needs.
    export PYTHONPATH="$_acheck_root${PYTHONPATH:+:$PYTHONPATH}"

    _acheck_err="$(mktemp 2>/dev/null)" || exit 0
    _acheck_rc=0
    python3 -m backend.stats.anomaly_detector --repo "${_REPO:-}" 2>"$_acheck_err" || _acheck_rc=$?

    if [ "$_acheck_rc" -ne 0 ]; then
        _acheck_why="$(head -n 1 "$_acheck_err" 2>/dev/null)"
        echo "[anomaly-check] backend.stats.anomaly_detector exited ${_acheck_rc} (repo_root=${_acheck_root}): ${_acheck_why:-no stderr output}"
    fi

    rm -f "$_acheck_err"
    exit 0
) || true
