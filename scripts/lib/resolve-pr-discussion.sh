#!/usr/bin/env bash
# scripts/lib/resolve-pr-discussion.sh — derive a PR's originating Discussion number.
#
# Shared by post-merge-hook.sh's own inline auto-detect logic and
# merge-and-hook.sh's HG-7 check (D#1588 Batch B security-needs-fix round).
#
# Usage (source this file, then call the function):
#   source "$(dirname "${BASH_SOURCE[0]}")/resolve-pr-discussion.sh"
#   disc="$(resolve_pr_discussion <pr_number> <code_repo> [<discussion_repo>])"
#
# Pass --all (anywhere in the argument list) to get EVERY validated Discussion
# number, one per line, instead of just the first. The two callers want
# different questions answered from the same extraction: merge-and-hook.sh's
# HG-7 check wants the single originating Discussion to check provenance
# against, and post-merge-hook.sh closes every Discussion the PR closes. That
# difference is the whole reason post-merge-hook.sh carried its own copy of
# this logic; folding it in here as a flag is what let the copy go. Measured on
# the 200 most recently merged PRs of autonomous-agent-7/fulcrumaxe
# (2026-09-04, operator host): one — #2224 — names three distinct Discussions,
# so the plural case is live rather than hypothetical.
#
# The two halves of this function read different repos once code and Discussions
# live apart: the PR body is fetched from <code_repo>, and each candidate number
# is validated against <discussion_repo>. Omit the third argument, or pass an
# empty string, and it falls back to <code_repo> — which is exactly the
# single-repo behaviour this function had before, and the right answer for a
# fork with no private twin.
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
#
# The split does not soften that contract. An empty <discussion_repo> means "no
# Discussion plane configured", and falling back to <code_repo> is honest: the
# GraphQL validation can only ever return a number that is a real Discussion in
# a repo we were told to look in. It cannot manufacture one, so a caller that
# fails closed on the empty string stays correct either way.

# _rpd_failure_cause <error_body> <disc_repo> <candidate>
#
# Turn a failed validation into the sentence an operator can act on. The raw
# GraphQL body already says what GitHub refused; it does not say what to DO,
# and after the cutover the wrong guess costs an hour in the exact window
# where merges are refused across the board.
#
# The mapping below is measured, not inferred — seven live cases run against
# the real API from the operator host (Linux) on 2026-09-04 with the project
# token, one `gh api graphql ... --jq '.data.repository.discussion.id'` per
# case:
#
#   real Discussion (fulcrumaxe#2348)      rc=0, stdout is the node id
#   number is a PR (fulcrumaxe#2361)       rc=1, "repository":{"discussion":null}
#   number does not exist (#999999)        rc=1, "repository":{"discussion":null}
#   repo slug does not exist               rc=1, "repository":null
#   repo exists, token cannot read it      rc=1, "repository":null   <-- same
#   bad credentials                        rc=1, {"message":"Bad credentials",...}
#   unreachable host                       rc=1, EMPTY stdout
#
# Five of seven are distinguishable. The one collision that matters is the
# fourth and fifth: `"repository":null` cannot tell a slug that does not exist
# from a real private repo this token may not read. GitHub answers NOT_FOUND
# rather than FORBIDDEN there **on purpose**, so it does not leak whether a
# private repo exists — which means no field in the response will ever split
# them and the message has to carry both causes. Measured against
# `github/github`, a repo that certainly exists and that this token cannot
# read: it returns `"repository":null` with the error path `["repository"]`,
# byte-identical to a slug typed wrong. That is structural, not incidental —
# `discussion` is a child field of `repository`, so a repo the token cannot
# read can never produce the `"repository":{"discussion":null}` shape at all.
#
# That matters most in exactly the configuration this repo is cutting over to:
# a public code repo and a private Discussion repo. A token scoped to the
# public repo only — the natural CI setup and the natural operator mistake —
# reads the PR body fine and cannot see the Discussion. It lands here.
#
# The two-and-a-half-line collision the numbers do NOT support is worth naming
# because a Discussion comment asserted it and this function was nearly built
# from it: "a repo the token cannot read returns `"repository":{"discussion":
# null}`, byte-identical to a bad number". It does not. Re-measured above.
#
# The remaining collision, bad number vs. number-is-a-PR, is benign: both mean
# "that number is not a Discussion here" and both have the same remedy.
#
# Retry advice belongs on the EMPTY body and nowhere else. An unreachable host
# is the only measured case that clears itself; every other branch here is a
# permanent misconfiguration, and telling an operator to wait out a permanent
# misconfiguration is how an outage gets long.
_rpd_failure_cause() {
  local body="$1" disc_repo="$2" cand="$3"

  if [[ -z "${body//[[:space:]]/}" ]]; then
    printf 'gh returned no error body at all — GitHub was unreachable (network, DNS or proxy) rather than refusing. This is the one transient case: retry.'
    return 0
  fi

  case "$body" in
    *'Bad credentials'*|*'"status": "401"'*|*'"status":"401"'*)
      printf 'the token was rejected (401). Re-authenticate gh / refresh GH_TOKEN.'
      return 0
      ;;
    *'"repository":null'*|*'Could not resolve to a Repository'*)
      printf '%s is not a repository this token can read. Either it does not exist (check the discussion_repo setting), or it exists and is private and this token lacks access to it (check the token'\''s scope) — GitHub answers NOT_FOUND for both so the response cannot tell them apart. Check the slug AND the scope. Not transient; retrying will not clear it.' "$disc_repo"
      return 0
      ;;
    *'Could not resolve to a Discussion'*)
      printf '#%s is not a Discussion in %s — it may be an Issue or PR number, or a typo. Fix the closing reference in the PR body.' "$cand" "$disc_repo"
      return 0
      ;;
  esac

  printf 'unrecognised gh failure — read the body above.'
}

resolve_pr_discussion() {
  local _rpd_all=0
  local -a _rpd_pos=()
  local _rpd_arg
  for _rpd_arg in "$@"; do
    if [[ "$_rpd_arg" == "--all" ]]; then
      _rpd_all=1
    else
      _rpd_pos+=("$_rpd_arg")
    fi
  done

  local pr="${_rpd_pos[0]:-}" code_repo="${_rpd_pos[1]:-}" disc_repo="${_rpd_pos[2]:-}"
  local owner name pr_body raw_nums cand disc_valid _rpd_rc _rpd_found=0

  [[ -n "$disc_repo" ]] || disc_repo="$code_repo"

  owner="${disc_repo%%/*}"
  name="${disc_repo##*/}"

  pr_body=$(gh pr view "$pr" --repo "$code_repo" --json body --jq '.body' 2>/dev/null || echo "")

  raw_nums=$(echo "$pr_body" \
    | grep -oiE '([Cc]loses|[Rr]esolves|[Ff]ixes) (D#|#)[0-9]+' \
    | grep -oE '[0-9]+' \
    | sort -u)

  for cand in $raw_nums; do
    # `gh api graphql --jq` exits non-zero on a GraphQL error but STILL prints
    # the raw error body to stdout, so `$(... || echo "")` captures that body
    # instead of the empty string it looks like it captures. Every NOT_FOUND —
    # the number is an Issue or a PR, the Discussion repo is unreachable, the
    # slug is wrong — therefore came back as a long non-empty string that
    # passed the `-n`/`!= null` test and got returned as a valid Discussion
    # number. Measured on this repo: `discussion(number:2361)` (2361 is a PR,
    # not a Discussion) returns rc=1 with a NOT_FOUND body, and the old code
    # answered "2361". Checking the exit status is what makes the
    # empty-means-unresolvable contract above true rather than aspirational,
    # and what keeps the number-space rejection this header claims from being
    # a fiction.
    #
    # It matters more once the two slugs diverge: an unreachable Discussion
    # repo is then an ordinary misconfiguration, and the old shape turned it
    # into a confidently wrong Discussion number for HG-7 to check provenance
    # against — a silent skip, which is the one failure mode a merge gate must
    # not have.
    _rpd_rc=0
    disc_valid=$(gh api graphql \
      -f query="query { repository(owner:\"${owner}\", name:\"${name}\") { discussion(number:$cand) { id } } }" \
      --jq '.data.repository.discussion.id' 2>/dev/null) || _rpd_rc=$?
    if [[ "$_rpd_rc" -ne 0 ]]; then
      # Say what failed, on stderr, because the caller fails closed on our
      # silence and a refused merge otherwise looks identical whether the PR
      # body named a number that is not a Discussion, the Discussion repo is
      # not readable with this token, or GitHub was simply unreachable. Those
      # need different responses and an operator cannot pick one from an empty
      # string.
      #
      # The error body is what `disc_valid` is holding here (see above), so
      # quoting it is free — but the body says what GitHub refused, not what to
      # do about it, and one of its shapes is ambiguous. _rpd_failure_cause
      # above turns it into the action; see its header for the measurements
      # that mapping comes from. Classify on the FULL body and print the
      # truncated one: the discriminator sits at roughly char 8 so truncation
      # is safe either way, but there is no reason to depend on that.
      printf 'resolve_pr_discussion: candidate #%s did not validate against %s (gh exit %s): %s\n' \
        "$cand" "$disc_repo" "$_rpd_rc" \
        "$(printf '%s' "$disc_valid" | tr -d '\n' | cut -c1-200)" >&2
      printf 'resolve_pr_discussion:   cause: %s\n' \
        "$(_rpd_failure_cause "$disc_valid" "$disc_repo" "$cand")" >&2
      continue
    fi
    if [[ -n "$disc_valid" && "$disc_valid" != "null" ]]; then
      echo "$cand"
      _rpd_found=1
      [[ "$_rpd_all" -eq 1 ]] || return 0
    fi
  done

  # Unresolvable — echo nothing, caller fails closed. Under --all this is
  # reached with nothing printed only when no candidate validated, so the
  # empty-means-unresolvable contract above holds in both modes.
  [[ "$_rpd_found" -eq 1 ]] && return 0
  return 1
}
