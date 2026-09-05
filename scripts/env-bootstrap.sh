#!/usr/bin/env bash
# Source this script to set up GH_CONFIG_DIR, GH_REPO, and PATH for cron/TUI
# context. Usage: source scripts/env-bootstrap.sh
#
# Exports: GH_CONFIG_DIR, GH_REPO, PATH (augmented)
#
# This used to also load OPENAI_API_KEY/OPENAI_BASE_URL from a provider
# config file, back when the loop ran on a pre-SDK CLI's OpenAI-compat
# provider lane. That lane was retired with the Claude Agent SDK migration
# (D#1765) — backend/server.py now explicitly logs those vars as "set but
# ignored" — and no host has ever had the config file this block required,
# which meant sourcing this script (and so starting the TUI, or running the
# loop runner) hard-failed everywhere. Dropped 2026-08-21 rather than kept
# as dead weight that also broke a live entry point.

export GH_CONFIG_DIR="$HOME/.config/gh"
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/repo-resolve.sh"

# GH_REPO is the CODE plane, resolved through _resolve_code_repo rather than
# _resolve_repo.
#
# This is not one call site getting the wrong slug. GH_REPO is the variable `gh`
# itself consults as its default repo, and this file is sourced by start-tui.sh
# and the loop runner, so the value set here is inherited by *every unpinned gh
# invocation in the process* — including ones in scripts that never mention a
# repo at all. It has to be the code plane for two reasons:
#
#   1. The overwhelming majority of unpinned `gh` calls under the TUI and the
#      loop are PR, CI and label operations, which are code-plane surfaces.
#   2. Discussion-plane callers already pin their own repo explicitly (the
#      GraphQL queries name owner/name literally), so they do not read this
#      default and are unaffected by which plane it carries.
#
# Today _resolve_code_repo and _resolve_repo return the same string, so this
# line changes nothing. It stops being a no-op the moment "code_repo" is set,
# which is exactly the moment the default has to already be right.
_GH_REPO_RESOLVED="$(_require_code_repo "env-bootstrap GH_REPO")" || {
  unset _SCRIPT_DIR _GH_REPO_RESOLVED
  # Sourced, so `return` rather than `exit` — killing the caller's shell over
  # this would be worse than leaving GH_REPO unset, and gh's own error for an
  # unset default is at least visible.
  return 1 2>/dev/null || exit 1
}
export GH_REPO="$_GH_REPO_RESOLVED"
unset _SCRIPT_DIR _GH_REPO_RESOLVED
export PATH="$HOME/.local/bin:$PATH"
