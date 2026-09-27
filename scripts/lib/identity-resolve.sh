#!/usr/bin/env bash
# scripts/lib/identity-resolve.sh — resolve the login the boss-login
# forbidden pattern protects: the account actually running the tooling,
# not a value shipped in the rules file.
#
# Usage (source, then call the function):
#   source "$(dirname "${BASH_SOURCE[0]}")/identity-resolve.sh"
#   LOGIN="$(resolve_self_login)" || LOGIN=""   # empty/failure = unresolved
#
# Resolution order, first non-empty wins:
#   1. .autonomous-team/config.json -> "boss_github_username" (engine-side:
#      this file never ships in the open-source export, so only an engine
#      checkout ever has it).
#   2. `git config fulcrumaxe.selfLogin` (adopter-side: offline, set once
#      per clone, persists across pulls).
#   3. unset.
#
# Deliberately NOT in this chain: `gh api user`. That is a network call on
# every pre-push scan (dead offline, and a real cost paid on every push),
# and it resolves to a CI bot identity under GitHub Actions rather than to
# a human, which protects nobody. See the Discussion this shipped with for
# the full reasoning; this file just implements the decision.
#
# GRAMMAR VALIDATION, NOT ESCAPING. A resolved value is checked against the
# GitHub login grammar before it is returned. A bot-shaped login carrying
# brackets (e.g. a CI actor's "name[bot]" form) is valid text but not
# something this file turns into a regex fragment: bracket-escaping it
# would make it match successfully, just not the string a naive reader
# expects, and enforcing a pattern that quietly matches the wrong thing is
# worse than not enforcing one at all. Anything that fails the grammar is
# treated exactly like an unresolved login — callers must degrade for it
# the same way they degrade for the unset case.
#
# No `set -e`/`set -u` here on purpose: this file is sourced into a caller
# that has already chosen its own shell options, and flipping them out from
# under it is a worse surprise than a stray unset variable in here.

# _self_login_trim <value> — strip leading/trailing whitespace.
_self_login_trim() {
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf '%s' "$v"
}

# _self_login_grammar_ok <value> — GitHub login grammar: starts and ends
# with an alphanumeric, single hyphens only in between, 1-39 characters
# total. Anything else (including an empty string, and including a
# bracket-bearing bot login) fails.
_self_login_grammar_ok() {
  [[ "$1" =~ ^[A-Za-z0-9](-?[A-Za-z0-9]){0,38}$ ]]
}

# resolve_self_login — prints the resolved login on stdout and returns 0,
# or prints nothing and returns 1 when nothing in the chain resolves to a
# grammar-valid value.
resolve_self_login() {
  local repo_root cj val=""

  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  cj="$repo_root/.autonomous-team/config.json"
  if [[ -f "$cj" ]]; then
    val="$(python3 -c '
import json, sys
try:
    v = json.load(open(sys.argv[1])).get("boss_github_username", "")
except Exception:
    v = ""
print(v if isinstance(v, str) else "")' "$cj" 2>/dev/null || true)"
    val="$(_self_login_trim "$val")"
  fi

  if [[ -z "$val" ]]; then
    val="$(git -C "$repo_root" config --get fulcrumaxe.selfLogin 2>/dev/null || true)"
    val="$(_self_login_trim "$val")"
  fi

  [[ -n "$val" ]] || return 1
  _self_login_grammar_ok "$val" || return 1
  printf '%s\n' "$val"
}
