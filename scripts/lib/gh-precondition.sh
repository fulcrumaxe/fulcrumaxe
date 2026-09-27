#!/usr/bin/env bash
# scripts/lib/gh-precondition.sh — assert the active `gh` account can see a
# given repo before any script trusts what `gh` reports about it.
#
# Why this exists (D#1787): `gh` writes GraphQL/REST error bodies to STDOUT
# (not stderr) and exits non-zero when the active account can't resolve a
# repo. A caller that redirects `2>/dev/null` on individual calls does NOT
# stop that error text from landing in its data — it just hides the real
# stderr diagnostics while the garbage keeps flowing on stdout. That garbage
# then gets word-split, counted, and reported as real numbers
# (fabricated PR counts, fabricated Discussion numbers, a green freshness
# tick with nothing behind it).
#
# The fix is one central assert, run once, before any sweep runs — not a
# `2>/dev/null` fix at each of the dozens of call sites that touch `gh`.
#
# Usage (source this file, then call the function):
#   source "$(dirname "${BASH_SOURCE[0]}")/gh-precondition.sh"
#   assert_gh_can_see_repo "$REPO" || exit 1

# assert_gh_can_see_repo <owner/name>
# Returns 0 if the active gh account can resolve the repo. On failure,
# prints a diagnostic (active account + the exact recovery command) to
# stderr and returns 1. Never touches stderr suppression to hide anything —
# the check itself decides pass/fail from the real exit code.
assert_gh_can_see_repo() {
  local slug="$1"
  local out rc

  out=$(gh api "repos/${slug}" --jq .full_name 2>&1)
  rc=$?

  if [[ $rc -eq 0 && "$out" == "$slug" ]]; then
    return 0
  fi

  local account
  account="$(_gh_precondition_active_account)"

  {
    echo "FATAL: gh account '${account}' cannot resolve repo '${slug}'."
    echo "  gh said: ${out}"
    echo "  This is NOT the same as '${slug}' not existing — a 404 here just means"
    echo "  '${account}' can't see it. It may well already exist, private, under a"
    echo "  different active account. Do not treat this as license to create it."
    echo "  '${account}' is the account that just failed — switch to a"
    echo "  different one that has access: gh auth switch --hostname github.com --user <account>"
    echo "  (list available accounts with: gh auth status)"
  } >&2
  return 1
}

# Reads the active account directly out of gh's own config file rather than
# calling `gh` again — a second call would be subject to the exact same
# broken-auth failure this function exists to report on.
_gh_precondition_active_account() {
  local hosts="${GH_CONFIG_DIR:-$HOME/.config/gh}/hosts.yml"
  if [[ -f "$hosts" ]]; then
    local u
    u=$(awk '/^[[:space:]]*user:/ { print $2; exit }' "$hosts")
    [[ -n "$u" ]] && { echo "$u"; return; }
  fi
  echo "unknown (could not read $hosts)"
}
