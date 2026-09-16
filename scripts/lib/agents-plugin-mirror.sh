#!/usr/bin/env bash
# scripts/lib/agents-plugin-mirror.sh — generate the plugin-loaded agents/
# copy from the canonical .claude/agents/ (D#2598 fix-round 2 item 2).
#
# Why this exists
# ----------------
# .claude/agents/ is what THIS project's own team runs, and it correctly
# hardcodes this project's own Discussion-plane repo (there is no adopter
# equivalent to resolve for our own operational use — see repo-resolve.sh's
# own docs on why _resolve_discussion_repo is allowed to come back empty for
# a fork but never for us).
#
# agents/ at the plugin root is different: Claude Code's plugin loader reads
# it directly, with NO install step and NO identifier rewrite in between
# (unlike .claude/agents/, which loop-bootstrap/bootstrap.sh rewrites at
# install time via rewrite_tree_identifiers). A user who installs this
# project as a Claude Code plugin and invokes a namespaced role
# (`fulcrumaxe:executor`) before ever running /coldstart was getting this
# project's own Discussion-plane repo baked into their session, permanently
# and silently -- the file never mentions that it needs rewriting.
#
# Dropping agents/ entirely was considered (Claude Code's plugin manifest
# does support it -- an empty "agents" array in plugin.json suppresses
# discovery, confirmed against code.claude.com/docs/en/plugins-reference)
# but scripts/build-public-seed.sh already documents why that breaks a real
# use case: "a `claude plugin install` user needs agents/" for anyone who
# installs the plugin without ever running /coldstart. So agents/ still
# ships -- generated, not mirrored byte-for-byte.
#
# What the generator does
# ------------------------
# Two literal-substring replacements, applied to EVERY .claude/agents/*.md
# file (content-gated, matching rewrite_tree_identifiers' own "grep -Iq .,
# skip binaries" convention -- these are always text, so no gate is needed
# beyond existing):
#
#   autonomous-agent-7/fulcrumaxe
#     -> $(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo)
#
#   owner:"autonomous-agent-7", name:"fulcrumaxe"
#     -> owner:"$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo | cut -d/ -f1)", name:"$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo | cut -d/ -f2)"
#
# Both replacements are valid, EXECUTABLE bash wherever the literal slug
# used to sit -- every occurrence in .claude/agents/ is already inside a
# `--repo <value>` or `repository(owner:"<value>", ...)` position an agent
# reads as an instruction to run, not decorative prose (grepped and
# confirmed: no occurrence of either pattern anywhere else in these files).
# An agent loaded as a plugin role executes the command substitution at
# read time and resolves whatever repo the LOADING project's own
# scripts/lib/repo-resolve.sh / .autonomous-team/config.json says --
# exactly the resolver .claude/agents/ already uses for the CODE plane in
# the same files (`_resolve_code_repo`), just applied to the one place that
# still hardcoded the Discussion plane.
#
# This project's own .claude/agents/ is unaffected -- the source files are
# read, never written, by this generator.
#
# Usage:
#   source scripts/lib/agents-plugin-mirror.sh
#   generate_agents_plugin_mirror <src-claude-agents-file>
#     -> prints the generated agents/ content to stdout

generate_agents_plugin_mirror() {
  local src="$1"
  sed \
    -e 's|owner:"autonomous-agent-7", name:"fulcrumaxe"|owner:"$(source scripts/lib/repo-resolve.sh \&\& _resolve_discussion_repo \| cut -d/ -f1)", name:"$(source scripts/lib/repo-resolve.sh \&\& _resolve_discussion_repo \| cut -d/ -f2)"|g' \
    -e 's|autonomous-agent-7/fulcrumaxe|$(source scripts/lib/repo-resolve.sh \&\& _resolve_discussion_repo)|g' \
    "$src"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  if [[ $# -ne 1 ]]; then
    echo "usage: bash scripts/lib/agents-plugin-mirror.sh <src-claude-agents-file>" >&2
    exit 2
  fi
  generate_agents_plugin_mirror "$1"
fi
