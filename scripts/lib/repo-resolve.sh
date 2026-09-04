#!/usr/bin/env bash
# scripts/lib/repo-resolve.sh — resolve project repo slug for portability.
#
# Usage (source this file, then call the function):
#   source "$(dirname "${BASH_SOURCE[0]}")/repo-resolve.sh"
#   REPO="$(_resolve_repo)"
#
# Resolution order (matches ts-backend/src/config/repo.ts's resolveRepo()):
#   1. .autonomous-team/config.json "repo" field
#   2. AUTONOMOUS_TEAM_REPO environment variable
#   3. Fail loudly (prints an error to stderr, returns 1, no stdout).
#
# There is deliberately no hard-coded slug fallback in step 3: .autonomous-team/
# never ships in the open-source export, so a forked adopter with neither
# config.json nor the env var set would otherwise silently inherit this
# project's own repo slug (D#1870). This repo's config.json is committed
# with the real slug, so step 1 always resolves here and step 3 is never
# reached in our own runtime.

_resolve_repo() {
  local repo_root cj
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  cj="$repo_root/.autonomous-team/config.json"
  if [[ -f "$cj" ]]; then
    local r
    r=$(python3 -c "import json,sys; print(json.load(open('$cj')).get('repo',''))" 2>/dev/null || true)
    if [[ -n "$r" ]]; then echo "$r"; return; fi
  fi
  if [[ -n "${AUTONOMOUS_TEAM_REPO:-}" ]]; then
    echo "$AUTONOMOUS_TEAM_REPO"
    return
  fi
  echo "error: could not resolve repo slug — set AUTONOMOUS_TEAM_REPO or add a \"repo\" field to .autonomous-team/config.json" >&2
  return 1
}
