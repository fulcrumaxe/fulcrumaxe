#!/usr/bin/env bash
# scripts/lib/resolve-pr-discussion.sh — derive a PR's originating Discussion number.
#
# Shared by post-merge-hook.sh's own inline auto-detect logic and
# merge-and-hook.sh's HG-7 check (D#1588 Batch B security-needs-fix round).
#
# Usage (source this file, then call the function):
#   source "$(dirname "${BASH_SOURCE[0]}")/resolve-pr-discussion.sh"
#   disc="$(resolve_pr_discussion <pr_number> <repo_slug>)"
#
# Matches (case-insensitive): Closes/Fixes/Resolves followed by D#N or #N in the
# PR body. Bare "D#N" / "Discussion #N" mentions with no closing keyword are
# intentionally excluded — they may reference related-but-not-originating work.
# Each candidate is validated via GraphQL so Issues/PRs sharing the same number
# space are rejected; only real Discussions are returned.
#
# Echoes the first valid Discussion number found, or nothing (empty string) if
# none could be resolved. Callers MUST treat an empty result as "unresolvable"
# and fail closed for any check that depends on knowing the Discussion — do NOT
# treat "not found" as "no Discussion, check not applicable".
resolve_pr_discussion() {
  local pr="$1" repo_slug="$2"
  local owner name pr_body raw_nums cand disc_valid

  owner="${repo_slug%%/*}"
  name="${repo_slug##*/}"

  pr_body=$(gh pr view "$pr" --repo "$repo_slug" --json body --jq '.body' 2>/dev/null || echo "")

  raw_nums=$(echo "$pr_body" \
    | grep -oiE '([Cc]loses|[Rr]esolves|[Ff]ixes) (D#|#)[0-9]+' \
    | grep -oE '[0-9]+' \
    | sort -u)

  for cand in $raw_nums; do
    disc_valid=$(gh api graphql \
      -f query="query { repository(owner:\"${owner}\", name:\"${name}\") { discussion(number:$cand) { id } } }" \
      --jq '.data.repository.discussion.id' 2>/dev/null || echo "")
    if [[ -n "$disc_valid" && "$disc_valid" != "null" ]]; then
      echo "$cand"
      return 0
    fi
  done

  # Unresolvable — echo nothing, caller fails closed.
  return 1
}
