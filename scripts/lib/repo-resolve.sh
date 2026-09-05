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

# --- Two names, one value ----------------------------------------------------
#
# Code, PRs and CI are moving to a public repo while Discussions stay in the
# private one. These accessors give that split a vocabulary *before* any call
# site uses it, so the consumers of _resolve_repo can be reclassified one
# subsystem at a time instead of in a flag day.
#
# Two optional config.json keys drive them:
#
#   "code_repo"        the repo that holds commits, PRs and CI.
#   "discussion_repo"  the repo that holds Discussions and Issues.
#
# Neither key is set in this tree, and neither is set by this change. With both
# absent every accessor returns exactly what _resolve_repo returns today, which
# is what makes adding them a no-op. Setting "code_repo" *is* the cutover, and
# belongs to the change that performs it.
#
# Precedence: the new keys are checked BEFORE AUTONOMOUS_TEAM_REPO, matching
# _resolve_repo's own documented order above (config.json first, env second).
# backend/_repo.py documents the reverse — env is "highest priority" there — so
# its accessors check the env var first instead. That asymmetry is deliberate:
# each accessor obeys the resolver it lives in, because silently changing which
# lever an operator can trust is worse than the inconsistency. See
# backend/_repo_planes.py's module docstring for the full reasoning.
#
# The asymmetry between the two accessors is deliberate:
#
#   _resolve_code_repo        falls back to _resolve_repo, fail-loudly step
#                             included. Every checkout has a code repo, so
#                             failing to resolve one is a real error.
#
#   _resolve_discussion_repo  prints nothing and returns 0 when nothing
#                             resolves. A forked adopter has no private twin,
#                             so "no Discussion plane" is a legitimate state and
#                             callers must branch on the empty string rather
#                             than treat it as a failure. It must not fall back
#                             to a hard-coded slug either: that would point a
#                             fork's Discussion reads at *our* repo, which is
#                             the hazard D#1870 exists to prevent.

# Read one string field from .autonomous-team/config.json. Prints nothing and
# returns 0 when the file, the key, or the value is missing — absent is the
# normal case here, so it is never an error.
_repo_config_field() {
  local key="$1" repo_root cj
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  cj="$repo_root/.autonomous-team/config.json"
  [[ -f "$cj" ]] || return 0
  python3 -c 'import json,sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2], "")
except Exception:
    v = ""
print(v if isinstance(v, str) else "")' "$cj" "$key" 2>/dev/null || true
}

# The repo that holds commits, PRs and CI. Identical to _resolve_repo unless
# "code_repo" is set.
_resolve_code_repo() {
  local r
  r="$(_repo_config_field code_repo)"
  if [[ -n "$r" ]]; then echo "$r"; return; fi
  _resolve_repo
}

# The repo that holds Discussions and Issues. Identical to _resolve_repo unless
# "discussion_repo" is set — except that an unresolvable Discussion plane is
# empty-and-fine rather than an error. See the block comment above.
_resolve_discussion_repo() {
  local r
  r="$(_repo_config_field discussion_repo)"
  if [[ -n "$r" ]]; then echo "$r"; return; fi
  _resolve_repo 2>/dev/null || return 0
}

# _require_code_repo [context] — the code-plane slug, or abort without printing
# one. Prints the slug on stdout and returns 0; on failure prints an actionable
# error to stderr, prints NOTHING on stdout, and returns 1.
#
# Why every code-plane call site needs this rather than bare _resolve_code_repo:
#
#   `gh --repo ""` is not an error. gh exits 0 after silently resolving the slug
#   from the checkout's git remote, so an empty value does not fail — it
#   succeeds against whatever repo the caller happens to be standing in. The
#   REST spelling degrades the same way: `repos//pulls/1` is a 404 rather than a
#   refusal. An unresolved plane must therefore stop the caller *before* `gh`
#   runs, which is what the `|| exit 1` at each call site does with this.
#
# Usage at a call site that may exit:
#     REPO="$(_require_code_repo "auto-plan")" || exit 1
# Usage inside a sourced library (a top-level `exit` would kill the caller —
# resolve at source time, then check in each entry point):
#     _FOO_REPO="$(_resolve_code_repo 2>/dev/null || true)"
#     foo() { [[ -n "$_FOO_REPO" ]] || { echo "..." >&2; return 1; }; ... }
_require_code_repo() {
  local context="${1:-code-plane call}" r
  r="$(_resolve_code_repo 2>/dev/null || true)"
  if [[ -z "$r" ]]; then
    echo "error: ${context}: could not resolve the code repo — refusing to run against an unresolved plane, because \`gh --repo \"\"\` silently falls back to the checkout's git remote instead of failing. Add a \"code_repo\" (or \"repo\") field to .autonomous-team/config.json, or set AUTONOMOUS_TEAM_REPO." >&2
    return 1
  fi
  printf '%s\n' "$r"
}
