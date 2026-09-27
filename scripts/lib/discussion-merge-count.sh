#!/usr/bin/env bash
# scripts/lib/discussion-merge-count.sh — count distinct merged PRs, across
# BOTH planes, that close a given Discussion (D#2590).
#
# `resolve_merged_count` in scripts/post-merge-hook.sh used to read
# `pr_state list --discussion N --repo <this plane>` for the plane the
# current merge happened on. That misses every merge on the other plane, and
# also misses same-plane merges recorded before pr_state rows were namespaced
# by repo (D#2379) — those sit on a legacy unnamespaced key a repo-scoped read
# never looks at. Four Discussions (D#2148, D#2524, D#2558, D#2585) each got
# stuck reporting "1 of 2" after their second PR merged, and had to be closed
# by hand.
#
# The fix asks GitHub directly, on both planes, for the authoritative answer:
# a merged PR closes a Discussion if its body carries a closing reference
# (Closes/Resolves/Fixes D#N or #N), the same convention
# scripts/lib/resolve-pr-discussion.sh already uses the other direction (PR ->
# Discussion). That is unioned with the repo-scoped pr_state rows on both
# planes (never the legacy key — a legacy key doesn't say which plane it
# belongs to, and trusting it risks crediting the wrong Discussion, D#2379)
# and with the PR the current hook run is for, since the GitHub search index
# can lag a PR that merged seconds ago.
#
# Usage (library):
#   source scripts/lib/discussion-merge-count.sh
#   discussion_merge_count <disc> <current_pr> <current_plane_repo>
#
# Usage (CLI):
#   bash scripts/lib/discussion-merge-count.sh <disc> <current_pr> <current_plane_repo>
#
# Echoes the integer count of distinct (plane, PR number) pairs. A PR number
# alone is never the dedup key — the same number can be a different, unrelated
# PR on each plane, so two hits with the same number on different planes count
# as two.
#
# Error handling: a `gh` failure on either plane is non-fatal. That plane's
# GitHub-sourced matches are dropped, a one-line warning goes to stderr, and
# the count still includes that plane's pr_state rows plus the current PR.
# The count never rises on a guess and this function never aborts the caller
# on a `gh` failure — undercounting (staying open) is the only permitted
# error direction (D#2272). An unresolved plane slug is a different kind of
# failure — a static misconfiguration, not a transient API hiccup — and is
# NOT swallowed the same way; see the `:?` guards below.

set -uo pipefail

_DMC_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_DMC_REPO_ROOT="$(cd "$_DMC_SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$_DMC_SCRIPT_DIR/repo-resolve.sh"

# _dmc_github_matches <disc> <plane_repo>
#
# Echoes (one per line) the PR numbers of merged PRs on <plane_repo> that
# close Discussion <disc> — body matches
# "(closes|resolves|fixes) (D#|#)<disc>" (case-insensitive, not followed by
# another digit, so D#2524 never matches D#25240), or title starts with
# "#<disc>:" (the older Discussion-plane convention). `--search` is only a
# prefilter to keep the candidate list small; the regex decides. Returns
# non-zero and prints nothing on a `gh` failure — a successful empty result
# set is not an error and is not distinguished from "no matches" by the
# caller, which is correct: both mean "GitHub contributes nothing here".
_dmc_github_matches() {
  local disc="$1" plane_repo="$2" prs rc

  prs=$(gh pr list --repo "$plane_repo" --state merged \
    --search "${disc} in:title,body" --limit 1000 \
    --json number,title,body 2>&1)
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    echo "discussion-merge-count: gh pr list failed for plane '$plane_repo' (exit $rc): $(printf '%s' "$prs" | tr -d '\n' | cut -c1-200)" >&2
    return 1
  fi

  printf '%s' "$prs" | python3 -c "
import json, re, sys

disc = sys.argv[1]
try:
    prs = json.load(sys.stdin)
except Exception:
    prs = []

closing_re = re.compile(r'(?i)(closes|resolves|fixes) (D#|#)' + re.escape(disc) + r'(?!\d)')
title_re = re.compile(r'^#' + re.escape(disc) + r':')

for pr in prs:
    body = pr.get('body') or ''
    title = pr.get('title') or ''
    if closing_re.search(body) or title_re.match(title):
        print(pr.get('number'))
" "$disc"
}

# _dmc_pr_state_matches <disc> <plane_repo>
#
# Echoes (one per line) the PR numbers of repo-scoped pr_state rows under
# <plane_repo> with discussion == <disc> and merged == true. Repo-scoping is
# what keeps this off the legacy unnamespaced key — see the file header.
_dmc_pr_state_matches() {
  local disc="$1" plane_repo="$2"
  python3 "$_DMC_REPO_ROOT/backend/pr_state.py" list --discussion "$disc" --repo "$plane_repo" 2>/dev/null \
    | python3 -c "
import json, sys

try:
    entries = json.load(sys.stdin)
except Exception:
    entries = []

for e in entries:
    if e.get('merged') is True:
        print(e.get('pr'))
"
}

# discussion_merge_count <disc> <current_pr> <current_plane_repo>
#
# Echoes the integer count of distinct (plane, PR) pairs merged against
# Discussion <disc>, across both planes.
discussion_merge_count() {
  local disc="$1" current_pr="$2" current_plane_repo="$3"
  local code_repo disc_repo plane num github_nums

  # Fail loudly on an unresolved plane rather than letting it silently become
  # a bare `gh --repo ""` call (which resolves against the checkout's git
  # remote instead of refusing) — the same discipline every code-plane call
  # site in this repo uses.
  code_repo="$(_resolve_code_repo)"
  : "${code_repo:?discussion-merge-count: code plane unresolved}"
  disc_repo="$(_resolve_discussion_repo)"
  : "${disc_repo:?discussion-merge-count: discussion plane unresolved}"

  declare -A pairs=()

  for plane in "$code_repo" "$disc_repo"; do
    if github_nums=$(_dmc_github_matches "$disc" "$plane"); then
      while IFS= read -r num; do
        [[ -n "$num" ]] && pairs["${plane}#${num}"]=1
      done <<< "$github_nums"
    else
      echo "discussion-merge-count: dropping GitHub-sourced matches for plane '$plane' — falling back to pr_state rows and the current PR only (undercounting is the safe direction here)" >&2
    fi

    while IFS= read -r num; do
      [[ -n "$num" ]] && pairs["${plane}#${num}"]=1
    done < <(_dmc_pr_state_matches "$disc" "$plane")
  done

  if [[ -n "$current_plane_repo" && -n "$current_pr" ]]; then
    pairs["${current_plane_repo}#${current_pr}"]=1
  fi

  echo "${#pairs[@]}"
}

# CLI entry point — only when executed directly, not when sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  discussion_merge_count "$@"
fi
