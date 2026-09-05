#!/usr/bin/env bash
# scripts/lib/gh-label.sh — label helpers for the current project.
#
# Two code paths:
#   - Outside a worktree: REST API (`gh api -X POST/DELETE .../labels`).
#   - Inside a worktree: the sandbox blocks that REST spelling, so we route
#     through the GraphQL mutations already allowlisted for worktrees
#     (addLabelsToLabelable / removeLabelsFromLabelable — see
#     hooks/sandbox_rules.py:_GH_API_GRAPHQL_MUTATION_ALLOWLIST, D#1148).
#
# Neither path swallows a real failure: apply_label/remove_label return the
# actual exit status, and stderr is left visible. Re-read the label
# afterwards with an independent `gh pr view --json labels` — don't trust
# the exit code alone (D#2031).
#
# Usage:
#   source scripts/lib/gh-label.sh
#   apply_label  <pr_number> <label>
#   remove_label <pr_number> <label>
#
# Testing override: set GH_LABEL_FORCE_WORKTREE=1 or =0 to force a code path
# regardless of the actual git state (used by tests/test_verdict_label_relay.sh).

# Resolve the repo these labels live on.
#
# PR labels are a CODE-plane surface, so this resolves through
# _resolve_code_repo, not _resolve_repo. _resolve_repo is the Discussion
# plane; the two return the same string today because "code_repo" is unset,
# and they stop agreeing the moment it is set. Every label this file writes
# is read back from the code plane — scripts/loop-phased-step5.sh's merging
# phase reads them off the PR — so a Discussion-plane write here would put
# the gate labels somewhere the gate never looks, and the merge gate would
# block forever on a label that was applied successfully to the wrong repo.
#
# The variable is named _GH_LABEL_REPO, not REPO, because this file is SOURCED.
# A bare `REPO=` here executes in the caller's shell and silently overwrites the
# caller's own variable of that name. scripts/sweep-stuck-prs.sh did exactly
# this to itself: it set REPO="$(_resolve_repo)", then sourced this file three
# lines later and had it replaced with the code-plane value — so the `gh pr
# list --repo "$REPO"` further down was reading a plane the script never chose.
# Harmless while both resolvers return the same slug, and a silent
# reclassification of that whole script the moment they stop. Every other
# variable in this file was already underscore-prefixed; this was the one that
# was not.
#
# shellcheck source=repo-resolve.sh
source "$(dirname "${BASH_SOURCE[0]}")/repo-resolve.sh"
_GH_LABEL_REPO="${LABEL_REPO:-$(_resolve_code_repo 2>/dev/null || true)}"

# An unresolved code plane must abort before `gh` runs, never fall through
# to a bare call. `gh --repo ""` is not an error: it exits 0 after silently
# resolving the slug from the checkout's git remote, so an empty
# _GH_LABEL_REPO does not fail — it succeeds against whatever repo the caller
# happens to be standing in. The REST path is the same shape:
# repos//issues/N/labels is a 404 rather than a refusal. That is the failure
# direction this closes.
#
# Checked in the entry points rather than at source time because this file
# is sourced, not executed: a top-level `exit` would kill the caller, and a
# top-level `return` would leave the functions defined but unguarded.
_gh_label_require_repo() {
  if [[ -z "${_GH_LABEL_REPO:-}" ]]; then
    echo "gh-label: could not resolve the code repo — refusing to run a label operation against an unresolved plane. Set LABEL_REPO, or add a \"code_repo\" (or \"repo\") field to .autonomous-team/config.json." >&2
    return 1
  fi
  return 0
}

# _gh_label_in_worktree — true (0) when running from inside a linked worktree.
# Same git-dir vs git-common-dir comparison used by post-agent-hook.sh's
# HEAD-recovery guard (see tests/test_post_agent_hook_recovery.sh).
_gh_label_in_worktree() {
  case "${GH_LABEL_FORCE_WORKTREE:-}" in
    1) return 0 ;;
    0) return 1 ;;
  esac
  local gd cgd
  gd=$(git rev-parse --git-dir 2>/dev/null || true)
  cgd=$(git rev-parse --git-common-dir 2>/dev/null || true)
  [[ -n "$gd" && -n "$cgd" && "$gd" != "$cgd" ]]
}

# _gh_label_owner / _gh_label_name — split _GH_LABEL_REPO ("owner/name") for GraphQL vars.
_gh_label_owner() { echo "${_GH_LABEL_REPO%%/*}"; }
_gh_label_name()  { echo "${_GH_LABEL_REPO##*/}"; }

# _gh_label_pr_node_id <pr_number> — echoes the PR's GraphQL node id, or empty.
_gh_label_pr_node_id() {
  local pr="$1"
  gh api graphql -f query='query($owner:String!,$name:String!,$pr:Int!){
    repository(owner:$owner,name:$name){ pullRequest(number:$pr){ id } }
  }' -f owner="$(_gh_label_owner)" -f name="$(_gh_label_name)" -F pr="$pr" 2>/dev/null \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    pr = d.get('data', {}).get('repository', {}).get('pullRequest')
    print(pr['id'] if pr else '')
except Exception:
    print('')
" 2>/dev/null
}

# _gh_label_label_id <label> — echoes the label's GraphQL node id, or empty
# (empty means the label does not exist in this repo).
_gh_label_label_id() {
  local label="$1"
  gh api graphql -f query='query($owner:String!,$name:String!,$label:String!){
    repository(owner:$owner,name:$name){ label(name:$label){ id } }
  }' -f owner="$(_gh_label_owner)" -f name="$(_gh_label_name)" -f label="$label" 2>/dev/null \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    lbl = d.get('data', {}).get('repository', {}).get('label')
    print(lbl['id'] if lbl else '')
except Exception:
    print('')
" 2>/dev/null
}

# _gh_label_graphql_mutate <mutation_name> <pr_number> <label>
# Shared body for the addLabelsToLabelable / removeLabelsFromLabelable path.
# Resolves both node ids first — either missing is a real, reported failure.
_gh_label_graphql_mutate() {
  local mutation="$1" pr="$2" label="$3"
  local pr_id lbl_id out rc

  pr_id="$(_gh_label_pr_node_id "$pr")"
  if [[ -z "$pr_id" ]]; then
    echo "${mutation}: could not resolve node id for PR #${pr} in ${_GH_LABEL_REPO}" >&2
    return 1
  fi

  lbl_id="$(_gh_label_label_id "$label")"
  if [[ -z "$lbl_id" ]]; then
    echo "${mutation}: label '${label}' does not exist in ${_GH_LABEL_REPO}" >&2
    return 1
  fi

  out=$(gh api graphql -f query="mutation { ${mutation}(input:{labelableId:\"${pr_id}\", labelIds:[\"${lbl_id}\"]}) { clientMutationId } }" 2>&1)
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "${mutation}: failed for #${pr}/${label}: ${out}" >&2
  fi
  return $rc
}

# apply_label <pr_number> <label>
# Adds a label to a PR/issue. REST outside a worktree, GraphQL inside one.
# Returns the real exit status either way — a nonexistent label is a
# reported failure, not a silent success (D#2031 criterion 2).
apply_label() {
  local pr=$1 label=$2
  if [ -z "$pr" ] || [ -z "$label" ]; then
    echo "apply_label: usage: apply_label <pr_number> <label>" >&2
    return 1
  fi
  _gh_label_require_repo || return 1

  if _gh_label_in_worktree; then
    _gh_label_graphql_mutate "addLabelsToLabelable" "$pr" "$label"
    return $?
  fi

  local out rc
  out=$(gh api -X POST "repos/${_GH_LABEL_REPO}/issues/${pr}/labels" -f "labels[]=${label}" 2>&1)
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "apply_label: failed to add '${label}' to #${pr}: ${out}" >&2
  fi
  return $rc
}

# remove_label <pr_number> <label>
# Removes a label from a PR/issue. REST outside a worktree, GraphQL inside one.
# Tolerates "label not present" as success (idempotent removal); reports any
# other failure with its real exit status.
remove_label() {
  local pr=$1 label=$2
  if [ -z "$pr" ] || [ -z "$label" ]; then
    echo "remove_label: usage: remove_label <pr_number> <label>" >&2
    return 1
  fi
  _gh_label_require_repo || return 1

  if _gh_label_in_worktree; then
    _gh_label_graphql_mutate "removeLabelsFromLabelable" "$pr" "$label"
    return $?
  fi

  local encoded_label out rc
  encoded_label=$(printf '%s' "$label" | jq -sRr @uri)
  out=$(gh api -X DELETE "repos/${_GH_LABEL_REPO}/issues/${pr}/labels/${encoded_label}" 2>&1)
  rc=$?
  if [[ $rc -eq 0 || "$out" == *"404"* || "$out" == *"Not Found"* ]]; then
    return 0
  fi
  echo "remove_label: failed to remove '${label}' from #${pr}: ${out}" >&2
  return $rc
}
