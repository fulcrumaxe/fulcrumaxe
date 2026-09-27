#!/usr/bin/env bash
# scripts/lib/pr-plane.sh — resolve which repo plane a PR number lives on
# (D#2563).
#
# PR numbers are not unique across the two planes: refs/pull/<N>/head exists
# independently on both the code plane and the Discussion plane, at unrelated
# commits, for the same N. Every tool that takes a bare --pr <N> therefore
# needs to know WHICH repo that number refers to before it does anything.
#
# Usage (source, then call):
#   source "$(dirname "${BASH_SOURCE[0]}")/pr-plane.sh"
#   pr_plane_resolve <pr_number> [plane_name]
#   # on success: PR_PLANE_NAME is "code" or "discussion",
#   #             PR_PLANE_REPO is the resolved repo slug.
#
# <plane_name> — pass "code" or "discussion" (an explicit plane NAME, never a
# free-form repo slug) to pin the resolution. A slug argument, or any value
# other than those two literals, is rejected outright with nothing on stdout.
#
# Omit <plane_name> (or pass an empty string) to probe both planes:
#   - exactly one plane has the PR                 -> that plane is used
#   - both planes have a PR with this number       -> refused; both slugs and
#                                                      both head shas are
#                                                      named, remedy: --plane
#   - neither plane has the PR                      -> refused
#
# Prints nothing on stdout on failure; an actionable message on stderr always.
# Returns non-zero on any failure, and clears PR_PLANE_NAME/PR_PLANE_REPO
# first so a caller that forgets to check the return code cannot mistake a
# stale success from a previous call for a fresh one.
#
# gh --repo "" exits 0 and silently resolves from the checkout's git remote —
# an empty resolved repo must never reach a `gh` call. Every call site in this
# file goes through _require_code_repo (which already refuses to print an
# empty string) or checks _resolve_discussion_repo's result for emptiness
# itself before using it.

_PRP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$_PRP_LIB_DIR/repo-resolve.sh"

PR_PLANE_NAME=""
PR_PLANE_REPO=""

# _prp_pr_exists <repo> <pr_number> — true (rc 0) iff the PR exists on <repo>.
# Never guesses: gh api answers non-zero on a 404, which is exactly what
# "does not exist here" should mean. An empty <repo> is treated as "does not
# exist" rather than risking `gh api repos//pulls/N`, a malformed path that
# behaves unpredictably rather than failing cleanly.
_prp_pr_exists() {
  local repo="$1" pr="$2"
  [[ -n "$repo" ]] || return 1
  gh api "repos/${repo}/pulls/${pr}" --jq '.number' >/dev/null 2>&1
}

# _prp_head_sha <repo> <pr_number> — best-effort, only used to make an
# ambiguous-PR refusal actionable (naming the sha on each plane).
_prp_head_sha() {
  local repo="$1" pr="$2"
  gh api "repos/${repo}/pulls/${pr}" --jq '.head.sha' 2>/dev/null || true
}

# pr_plane_resolve <pr_number> [plane_name]
pr_plane_resolve() {
  local pr="${1:-}"
  local plane_arg="${2:-}"

  PR_PLANE_NAME=""
  PR_PLANE_REPO=""

  if [[ -z "$pr" ]]; then
    echo "pr_plane_resolve: PR number is required" >&2
    return 1
  fi
  if [[ ! "$pr" =~ ^[0-9]+$ ]]; then
    echo "pr_plane_resolve: PR number must be numeric, got '$pr' — refusing to interpolate it into a gh api path" >&2
    return 1
  fi

  # Test-only escape hatch, mirrors CODE_PLANE_REMOTE_OVERRIDE's convention in
  # repo-resolve.sh: when set (even to an empty repo), short-circuits every
  # probe/resolve below and returns exactly what was asked for. This is what
  # lets a test simulate "the plane resolver returned empty with rc=0" — a
  # defensive case a caller must guard against regardless of whether this
  # function's own contract can currently produce it — without touching `gh`
  # at all. Gated on -n alone (never PYTEST_CURRENT_TEST), same reasoning as
  # CODE_PLANE_REMOTE_OVERRIDE: only bash suites invoked directly ever need
  # this, and no operator has reason to export it.
  #
  # Placed below the empty/numeric checks (not above): those checks validate
  # the caller's *input*, which the override does not stand in for — it only
  # replaces what pr_plane_resolve would have discovered about the PR. A
  # validation hatch that sits above the validation it is meant to bypass is
  # precedent for every override that follows it to skip validation too, so
  # this one earns its way past the same input checks every real call must
  # pass, even though its own body never uses $pr again after this point.
  if [[ -n "${PR_PLANE_RESOLVE_OVERRIDE_NAME:-}" ]]; then
    PR_PLANE_NAME="${PR_PLANE_RESOLVE_OVERRIDE_NAME:-}"
    PR_PLANE_REPO="${PR_PLANE_RESOLVE_OVERRIDE_REPO:-}"
    return 0
  fi

  if [[ -n "$plane_arg" ]]; then
    case "$plane_arg" in
      code|discussion) : ;;
      *)
        echo "pr_plane_resolve: --plane must be 'code' or 'discussion' — got '$plane_arg'. A free-form repo slug is never accepted here; pass the plane NAME." >&2
        return 1
        ;;
    esac

    local repo=""
    if [[ "$plane_arg" == "code" ]]; then
      repo="$(_require_code_repo "pr_plane_resolve")" || return 1
    else
      repo="$(_resolve_discussion_repo 2>/dev/null || true)"
      if [[ -z "$repo" ]]; then
        echo "pr_plane_resolve: could not resolve the Discussion plane — refusing to run against an unresolved plane (gh --repo \"\" silently falls back to the checkout's git remote instead of failing). Set discussion_repo/repo in .autonomous-team/config.json or AUTONOMOUS_TEAM_REPO." >&2
        return 1
      fi
    fi

    if ! _prp_pr_exists "$repo" "$pr"; then
      echo "pr_plane_resolve: PR #${pr} does not exist on the resolved ${plane_arg} plane (${repo})." >&2
      return 1
    fi

    PR_PLANE_NAME="$plane_arg"
    PR_PLANE_REPO="$repo"
    return 0
  fi

  # No --plane given: probe both planes. Never guess — exactly one hit wins,
  # both or neither is a refusal.
  local code_repo disc_repo
  code_repo="$(_require_code_repo "pr_plane_resolve" 2>/dev/null || true)"
  disc_repo="$(_resolve_discussion_repo 2>/dev/null || true)"

  local code_hit=false disc_hit=false
  if [[ -n "$code_repo" ]] && _prp_pr_exists "$code_repo" "$pr"; then
    code_hit=true
  fi
  # A fork with no private twin (or a checkout where the two names happen to
  # resolve identically, e.g. code_repo unset in a fixture) must not probe
  # the same repo twice and call that "ambiguous".
  if [[ -n "$disc_repo" && "$disc_repo" != "$code_repo" ]] && _prp_pr_exists "$disc_repo" "$pr"; then
    disc_hit=true
  fi

  if [[ "$code_hit" == "true" && "$disc_hit" == "true" ]]; then
    local code_sha disc_sha
    code_sha="$(_prp_head_sha "$code_repo" "$pr")"
    disc_sha="$(_prp_head_sha "$disc_repo" "$pr")"
    echo "pr_plane_resolve: PR #${pr} exists on BOTH planes — code (${code_repo} @ ${code_sha:-unknown}) and discussion (${disc_repo} @ ${disc_sha:-unknown}). Ambiguous — never guessing. Remedy: pass --plane code or --plane discussion explicitly." >&2
    return 1
  fi

  if [[ "$code_hit" == "true" ]]; then
    PR_PLANE_NAME="code"
    PR_PLANE_REPO="$code_repo"
    echo "pr_plane_resolve: PR #${pr} resolved to the code plane (${code_repo})." >&2
    return 0
  fi

  if [[ "$disc_hit" == "true" ]]; then
    PR_PLANE_NAME="discussion"
    PR_PLANE_REPO="$disc_repo"
    echo "pr_plane_resolve: PR #${pr} resolved to the discussion plane (${disc_repo})." >&2
    return 0
  fi

  echo "pr_plane_resolve: PR #${pr} was not found on either plane (code=${code_repo:-unresolved}, discussion=${disc_repo:-unresolved})." >&2
  return 1
}
