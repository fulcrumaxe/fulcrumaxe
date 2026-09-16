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
# How call sites are identified
# -----------------------------
# By literal-substring match on the hardcoded Discussion-plane slug
# `autonomous-agent-7/fulcrumaxe` in the canonical source, classified by the
# line's surrounding context (assumption, round 2b — verified against the
# current corpus, not a parser):
#
#   1. Single-line `gh issue ... --repo <slug>` command lines (including
#      `LOG=$(gh issue ...)` and mid-prose `Bug: gh issue view ...` forms —
#      all matched by the literal `gh issue` on the same line as the slug).
#   2. The multi-line `gh issue create \` statement whose `--repo <slug>`
#      sits on the immediately following continuation line (today: only
#      incident-commander.md; the `create` line itself carries no slug, so it
#      is buffered one line to decide).
#   3. Policy prose naming the `--repo` flag (`or \`--repo <slug>\`` in the
#      "Every `gh` call passes..." sentence; `must use \`--repo <slug>\`` in
#      the "All `gh` CLI calls must use..." sentence). These lines contain no
#      `gh issue` command.
#   4. GraphQL owner/name pairs, in both corpus spellings
#      (`owner:"<slug>", name:"<slug>"` and `owner:"<owner>",
#      name:"<repo>"`) — fail closed at the API on empty, no remote-fallback
#      hazard, so no guard needed.
#   5. Bare slug in display prose ("You ONLY interact with ...", the
#      Discussion-plane bullet) — nothing there is passed to `gh --repo`.
#
# What the generator does
# ------------------------
# Classes 1-3 are rewritten to the full same-statement shape — resolution
# prefix AND guarded use joined by `;`, mirroring the code-plane exemplar the
# same files already document (`CODE_REPO="..."; gh pr view ... --repo
# "${CODE_REPO:?code plane unresolved}"`, "One statement, joined by `;` —
# not two lines and not two tool calls"):
#
#   DISCUSSION_REPO="$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo)"; gh <args> --repo "${DISCUSSION_REPO:?discussion plane unresolved}"
#
# An empty resolution then aborts before `gh` runs (fail-closed); a good one
# flows through. The guarded `${VAR:?}` use WITHOUT the same-statement
# prefix — the round-2a shape — is broken-by-construction: shell state does
# NOT survive between tool calls (agents/acceptance-tester.md documents
# this), so with zero `DISCUSSION_REPO=` assignments anywhere in the file the
# guard fails always, not just on empty resolution. The prefix must be part
# of the emitted statement, never a separate line or a line the agent is
# expected to have run earlier.
#
# Classes 4-5 keep the inline resolver call deliberately (no `gh --repo`
# hazard there, as noted above).
#
# An agent loaded as a plugin role resolves whatever repo the LOADING
# project's own scripts/lib/repo-resolve.sh / .autonomous-team/config.json
# says -- exactly the resolver .claude/agents/ already uses for the CODE
# plane in the same files (`_resolve_code_repo`), just applied to the one
# place that still hardcoded the Discussion plane.
#
# This project's own .claude/agents/ is unaffected -- the source files are
# read, never written, by this generator.
#
# Usage:
#   source scripts/lib/agents-plugin-mirror.sh
#   generate_agents_plugin_mirror <src-claude-agents-file>
#     -> prints the generated agents/ content to stdout
#   agents_mirror_same_statement_violations <generated-file>...
#     -> prints "path:line" for each backslash-joined statement whose
#        guarded ${DISCUSSION_REPO:?...} use shares no statement with its
#        DISCUSSION_REPO= resolution; prints nothing when clean.

generate_agents_plugin_mirror() {
  local src="$1"
  awk '
    BEGIN {
      slug = "autonomous-agent-7/fulcrumaxe"
      assign = "DISCUSSION_REPO=\"$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo)\""
      guardflag = "--repo \"${DISCUSSION_REPO:?discussion plane unresolved}\""
      fullstmt = assign "; gh <args> --repo \"${DISCUSSION_REPO:?discussion plane unresolved}\""
      ownerpair = "owner:\"$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo | cut -d/ -f1)\", name:\"$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo | cut -d/ -f2)\""
      resolver = "$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo)"
      pending = ""
    }
    # Literal-substring replacement (index/substr only — the replacement
    # texts contain $, ", && and |, none of which may pass through a
    # sub()/gsub() replacement where & and \ are magic).
    function rep_all(line, pat, rep,   out, i) {
      out = ""
      while ((i = index(line, pat)) > 0) {
        out = out substr(line, 1, i - 1) rep
        line = substr(line, i + length(pat))
      }
      return out line
    }
    function rep_one(line, pat, rep,   i) {
      i = index(line, pat)
      if (i > 0) line = substr(line, 1, i - 1) rep substr(line, i + length(pat))
      return line
    }
    # Prefix a single-line `gh issue` command with the resolution so the
    # statement resolves AND uses in one `;`-joined statement. A `LOG=$(gh
    # ...)` form puts the assignment at statement start (mirroring the
    # code-plane `CODE_REPO="..."; labels=$(gh ...)` shape in the same
    # files); any other form puts it immediately before `gh`.
    function insert_assign(line,   pos, head, indlen) {
      pos = index(line, "gh issue")
      head = substr(line, 1, pos - 1)
      if (head ~ /LOG=\$\($/) {
        match(line, /^[ \t]*/)
        indlen = RLENGTH
        return substr(line, 1, indlen) assign "; " substr(line, indlen + 1)
      }
      return head assign "; " substr(line, pos)
    }
    function transform(line) {
      # Class 3 first: policy prose names the --repo flag but runs no
      # command, so it must carry the full statement, not just the flag.
      line = rep_all(line, "or `--repo " slug "`", "or `" fullstmt "`")
      line = rep_all(line, "must use `--repo " slug "`", "must use `" fullstmt "`")
      # Class 1: single-line gh issue commands get the resolution prefix.
      if (index(line, "gh issue") > 0 && index(line, slug) > 0)
        line = insert_assign(line)
      # Class 4 before the bare-slug fallback. Two spellings occur in the
      # corpus: the combined-slug pair and the split owner/name pair; the
      # split pair contains no full slug, so without its own rule it would
      # survive generation (and trip the literal-mention scan in the guard).
      line = rep_all(line, "owner:\"" slug "\", name:\"" slug "\"", ownerpair)
      line = rep_all(line, "owner:\"autonomous-agent-7\", name:\"fulcrumaxe\"", ownerpair)
      # Any remaining --repo <slug>: the backslash-continued --repo line of
      # a class-2 statement, decided by the buffering rule below.
      line = rep_all(line, "--repo " slug, guardflag)
      # Class 5: display prose.
      line = rep_all(line, slug, resolver)
      return line
    }
    {
      # Class 2: `gh issue create \` carries no slug itself — buffer one
      # line; only a statement whose continuation names the Discussion plane
      # gets the prefix (bare `gh issue create` blocks elsewhere, e.g.
      # visual-verifier.md, are left untouched).
      if (pending != "") {
        held = pending; pending = ""
        if (index($0, "--repo " slug) > 0) {
          print rep_one(held, "gh issue create", assign "; gh issue create")
          print rep_one($0, "--repo " slug, guardflag)
          next
        }
        print transform(held)
      }
      if ($0 ~ /gh issue create \\$/) { pending = $0; next }
      print transform($0)
    }
    END { if (pending != "") print transform(pending) }
  ' "$src"
}

# Same-statement check shared by the CI guard and the suites: backslash
# continuations are joined first (a `gh issue create \` statement is one
# statement), then every joined statement containing the guarded
# ${DISCUSSION_REPO:?...} use must also contain its DISCUSSION_REPO=
# resolution. Prints "path:lineno" per violation; exit 0 when clean, 1 when
# any violation is found. Its target is the split shape — guarded use with
# the assignment nowhere in the statement (the fail-open unguarded expansion
# is caught by the guard's fail-open scan instead).
agents_mirror_same_statement_violations() {
  awk '
    BEGIN { buf = ""; start = 0; bad = 0 }
    function check(stmt, firstline) {
      if (index(stmt, "${DISCUSSION_REPO:?discussion plane unresolved}") > 0 && \
          index(stmt, "DISCUSSION_REPO=\"$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo)\"") == 0) {
        print FILENAME ":" firstline
        bad = 1
      }
    }
    {
      if (buf == "") start = FNR
      line = $0
      if (line ~ /\\$/) { sub(/\\$/, "", line); buf = buf line; next }
      check(buf line, start)
      buf = ""
    }
    END { if (buf != "") check(buf, start); exit (bad ? 1 : 0) }
  ' "$@"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  if [[ $# -ne 1 ]]; then
    echo "usage: bash scripts/lib/agents-plugin-mirror.sh <src-claude-agents-file>" >&2
    exit 2
  fi
  generate_agents_plugin_mirror "$1"
fi
