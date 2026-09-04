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
export GH_REPO="$(_resolve_repo)"
unset _SCRIPT_DIR
export PATH="$HOME/.local/bin:$PATH"
