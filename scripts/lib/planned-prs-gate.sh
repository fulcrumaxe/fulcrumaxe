#!/usr/bin/env bash
# scripts/lib/planned-prs-gate.sh — hard-blocks an executor spawn against a
# Discussion whose Spec carries no anchored `planned_prs:` declaration
# (D#2272).
#
# .claude/agents/project-manager.md already said the field is "required" and
# then, two lines later, licensed a Spec written without it to still ship —
# a rule that licenses its own omission is not a rule. 11 of the 16
# Discussions filed 2026-09-02 shipped without the field, which is the
# measurement of that self-cancelling wording. This file moves the
# requirement off the role card and onto the executor spawn path itself, at
# the same enforcement point scripts/lib/spec-ready-gate.sh (D#1798) already
# occupies.
#
# scripts/spawn-agent.sh is on the PreToolUse forbidden-command list and
# cannot be executed to test it directly — same reasoning as
# spec-ready-gate.sh and resolve-spec-text.sh — so this file defines a
# sourceable function a shell test can call directly instead of paraphrasing
# the gate's logic.
#
# Resolution is NOT reimplemented here: discussion_close_decision (the merge
# guard) and planned_prs_gate_check (this spawn gate) both call the one
# shared resolve_planned_prs() in discussion-close-guard.sh. Two copies of
# "where does the number live" is the exact drift that caused D#1566.
#
# Usage (source, then call):
#   source "$REPO_ROOT/scripts/lib/planned-prs-gate.sh"
#   if ! planned_prs_gate_check "$DISCUSSION"; then
#     exit 1
#   fi
#
# Override: SPAWN_AGENT_ALLOW_NO_PLANNED_PRS=1 plus a required
# SPAWN_AGENT_ALLOW_NO_PLANNED_PRS_REASON (same idiom as
# SPAWN_AGENT_ALLOW_NO_SPEC) — refused unless both are set.

_PLANNED_PRS_GATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PLANNED_PRS_GATE_REPO_ROOT="${REPO_ROOT:-$(cd "$_PLANNED_PRS_GATE_DIR/../.." && pwd)}"

# shellcheck source=scripts/lib/discussion-close-guard.sh
source "$_PLANNED_PRS_GATE_DIR/discussion-close-guard.sh"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$_PLANNED_PRS_GATE_DIR/repo-resolve.sh"

# planned_prs_gate_check <discussion_number>
#   Returns 0 (prints nothing on the happy path) when the Spec — body or any
#   comment — carries an anchored `planned_prs: N` declaration.
#   Returns 1 and prints the reason plus a fix and the override to stderr
#   otherwise.
#   Fails closed: a failed GraphQL fetch blocks the spawn rather than
#   silently passing — matches how the SPEC_READY gate treats an unreadable
#   body at this same call site.
planned_prs_gate_check() {
  local disc_num="${1:-}"
  if [[ -z "$disc_num" ]]; then
    echo "planned_prs_gate_check: usage: planned_prs_gate_check <discussion_number>" >&2
    return 1
  fi
  # Defense in depth: $disc_num is interpolated directly into the GraphQL
  # query text below. No untrusted caller exists today, but a numeric guard
  # costs one line and closes the class rather than trusting every future
  # caller to keep validating upstream (same guard resolve_spec_text.sh uses).
  if [[ ! "$disc_num" =~ ^[0-9]+$ ]]; then
    echo "planned_prs_gate_check: discussion number must be numeric, got: $disc_num" >&2
    return 1
  fi

  local repo owner name
  repo="$(_resolve_repo)"
  if [[ -z "$repo" ]]; then
    echo "planned_prs_gate_check: could not resolve target repo — blocking Discussion #$disc_num spawn." >&2
    return 1
  fi
  owner="${repo%%/*}"
  name="${repo##*/}"

  # Same shape as scripts/post-merge-hook.sh's discussion_close step: body +
  # first 100 comments in one read, including its hasNextPage warning. PMs
  # post Specs — and therefore planned_prs — as comments (D#2064); a
  # body-only query would silently miss two of the three verified
  # auto-closes (D#2272's own Spec comment).
  local disc_data
  disc_data=$(gh api graphql \
    -f query="query { repository(owner:\"${owner}\", name:\"${name}\") { discussion(number:$disc_num) { body comments(first:100) { pageInfo { hasNextPage } nodes { body } } } } }" \
    --jq '.data.repository.discussion' 2>/dev/null || echo "")

  if [[ -z "$disc_data" ]]; then
    echo "planned_prs_gate_check: could not fetch Discussion #$disc_num (GraphQL read failed) — blocking spawn rather than trusting an unread Spec. Retry, or check GitHub API connectivity." >&2
    return 1
  fi

  local body comments_text has_next
  body=$(echo "$disc_data" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('body','') or '')" 2>/dev/null || echo "")
  comments_text=$(echo "$disc_data" | python3 -c "
import json, sys
d = json.load(sys.stdin)
nodes = (d.get('comments') or {}).get('nodes') or []
print('\n'.join(n.get('body', '') for n in nodes))
" 2>/dev/null || echo "")
  has_next=$(echo "$disc_data" | python3 -c "import json,sys; d=json.load(sys.stdin); print('true' if (d.get('comments') or {}).get('pageInfo',{}).get('hasNextPage') else 'false')" 2>/dev/null || echo "false")

  if [[ "$has_next" == "true" ]]; then
    echo "planned_prs_gate_check: Warning: Discussion #$disc_num has more than 100 comments — planned_prs resolution only saw the first 100, a later comment may be missed (non-fatal)" >&2
  fi

  resolve_planned_prs "$body" "$comments_text"

  if [[ -n "$PLANNED_PRS" ]]; then
    return 0
  fi

  if [[ "${SPAWN_AGENT_ALLOW_NO_PLANNED_PRS:-0}" == "1" ]]; then
    local reason="${SPAWN_AGENT_ALLOW_NO_PLANNED_PRS_REASON:-}"
    if [[ -z "$reason" ]]; then
      echo "planned_prs_gate_check: SPAWN_AGENT_ALLOW_NO_PLANNED_PRS=1 set without SPAWN_AGENT_ALLOW_NO_PLANNED_PRS_REASON — override refused. State a reason." >&2
      return 1
    fi
    echo "WARN: SPAWN_AGENT_ALLOW_NO_PLANNED_PRS override for Discussion #$disc_num — reason: $reason" >&2
    return 0
  fi

  echo "Spawn blocked: Discussion #$disc_num's Spec (body or comments) carries no anchored 'planned_prs:' declaration." >&2
  echo "  Fix: the Spec frontmatter must declare planned_prs: N (the number of PRs this plan needs), or planned_prs: 0 with a one-line reason when completion is operational rather than a PR." >&2
  echo "  Override: SPAWN_AGENT_ALLOW_NO_PLANNED_PRS=1 plus SPAWN_AGENT_ALLOW_NO_PLANNED_PRS_REASON=\"...\" (document why in the spawn context)." >&2
  return 1
}
