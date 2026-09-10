#!/usr/bin/env bash
# scripts/lib/agent-scratchpad.sh — per-agent scratchpad convention block for spawn-prompt injection.
#
# Usage (source or call directly):
#   source scripts/lib/agent-scratchpad.sh
#   agent_scratchpad_block       # prints ## Scratchpad Convention block to stdout
#
# D#2360: the Claude Code harness hands every spawned agent the SAME scratchpad
# root — it is not per-agent, and nothing in this repo can partition a directory
# the harness supplies. Concurrent siblings sharing that root have already
# clobbered each other three times in one session: a wiped extraction tree
# underneath a manifest-generate step, in-progress edits reverted to pristine
# mid-task, and a `git archive` extraction silently populated from the wrong
# tree. Every one of those failures was silent — `git archive | tar -x` or a
# body-file write into a half-deleted directory just succeeds; nothing errors.
#
# This block is a CONVENTION, delivered through the one channel that already
# reaches every spawn without any individual prompt remembering to say so — it
# is not, and cannot be, an enforced isolation boundary.
#
# Mirrors the wiring pattern of scripts/lib/working-principles.sh /
# ## Working Principles block (D#519) — same injection point, same mechanism,
# in scripts/pre-spawn-check.sh.

# agent_scratchpad_block
# Prints the ## Scratchpad Convention markdown block to stdout (always — no
# config file needed, no agent id available at this layer to interpolate).
agent_scratchpad_block() {
  cat <<'SCRATCHPAD'
## Scratchpad Convention

Your scratchpad directory is shared with every agent running right now, not
private to you — a concurrent sibling can create, overwrite, or delete a file
at the same path with no error and no lock. Before writing anything there,
create and use only a subdirectory derived from your own identity — your
agent id, your worktree path's basename, or `$$` — never a fixed name. If you
cannot create a subdirectory, prefix every filename you write at the shared
root with that same identifier instead.

This is a convention, not enforcement: nothing in this repo can partition a
directory the harness supplies.
SCRATCHPAD
}

# Allow direct invocation: bash scripts/lib/agent-scratchpad.sh agent_scratchpad_block
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  cmd="${1:-}"
  shift || true
  case "$cmd" in
    agent_scratchpad_block)
      agent_scratchpad_block
      ;;
    *)
      echo "Usage: $0 agent_scratchpad_block" >&2
      exit 1
      ;;
  esac
fi
