#!/usr/bin/env bash
# scripts/lib/pr-tree.sh — provision a writable, pushable worktree at a PR's
# head commit (D#2014).
#
# An executor spawned to amend an open PR needs a tree whose HEAD is the PR's
# head SHA, not main's. Nothing in this repo created that tree before this
# file existed — scripts/spawn-agent.sh only ever CLAIMED a path (see its
# resolve-worktree-path block); it never provisioned one. This is the
# provisioning mechanism that claim was missing.
#
#   source scripts/lib/pr-tree.sh
#   DEST="$(pr_tree_provision "$PR_NUMBER" "$HEAD_SHA" "$dest_path" "$PLANE")" || exit 1
#
# pr_tree_provision <pr_number> <head_sha> <dest> <plane> [parent_repo]
#   <plane> is "code" or "discussion" (see scripts/lib/pr-plane.sh) — it
#   selects which git remote is fetched AND which repo the cross-check below
#   reads from. An unrecognised plane value is rejected outright.
#   1. Fetches the PR head ref (refs/pull/<N>/head) into the parent repo's
#      object store, so <head_sha> is reachable even when it never landed on
#      a local branch.
#   2. `git worktree add --detach <dest> <head_sha>`.
#   3. Verifies `git -C <dest> rev-parse HEAD` equals <head_sha> exactly.
#   4. Prints the absolute <dest> path on stdout.
# Returns non-zero with a one-line reason on stderr on any failure. Anything
# this function created before a failing step is removed again, so a retry
# never has to clean up a half-built tree first.
#
# Why a worktree, not a clone
# ---------------------------
# `git worktree add --detach` shares the parent's object store AND its
# `origin` remote for free — that is what keeps `git push origin
# HEAD:<pr-branch>` working completely unchanged once the executor is done.
# A clone would need `git remote set-url` before it could push anywhere
# useful, which is extra machinery this avoids.
#
# Why this is NOT scripts/lib/verify-tree.sh
# -------------------------------------------
# verify-tree.sh's verify_tree_build is the sanctioned mechanism for
# READ-ONLY roles (code-reviewer, acceptance-tester) to materialise a PR head
# for inspection — but it is unusable here on two independent grounds:
#   - it write-protects every tracked file (`chmod a-w`), so an executor
#     could not edit in the tree it built;
#   - its clone's `origin` is the local parent checkout
#     (`git clone --shared ... "$parent" "$dest"`), so a push would land in
#     the local checkout, never on GitHub.
# Do not point an amend-a-PR spawn at verify-tree.sh; this file is the split
# that exists so neither mechanism has to be stretched to cover the other's
# job.
#
# Where <head_sha> must be reachable from
# ----------------------------------------
# GitHub always creates `refs/pull/<N>/head` for an open PR, whether or not
# the PR branch itself still exists on the fork. Fetching that ref (rather
# than the branch name) is what makes this work even after the PR author has
# deleted their branch.
#
# Which remote, and which repo the cross-check reads (D#1940 FM-5, D#2563)
# --------------------------------------------------------------------------
# PR numbers are not unique across planes: the code plane and the Discussion
# plane each number their own PRs from 1, so "PR #67" names two different
# commits. Fetching `refs/pull/<N>/head` from the git remote literally named
# "origin" ALWAYS means the Discussion plane here — never guessed as a
# fallback for "code" — and a wrong-plane fetch succeeds silently (exit 0,
# no warning). <plane> selects the remote (via
# scripts/lib/repo-resolve.sh's `_resolve_code_plane_remote` for "code",
# literal "origin" for "discussion") AND the repo the belt-and-braces
# cross-check below reads its authoritative headRefOid from — both must
# name the plane the caller actually resolved <pr_number> against
# (scripts/lib/pr-plane.sh), or the cross-check itself would silently defeat
# the fix by re-asserting the wrong plane's answer.
#
# Why the cross-check exists at all: objects fetched under an old remote
# naming convention can already sit in the parent's object store from a
# prior era, which would otherwise let a wrong-plane head_sha resolve as
# "reachable" even after the fetch itself is correctly pinned by remote. The
# plane-qualified ref this function fetches into (refs/pr-tree/<plane>/<N>)
# closes most of that gap by construction; the independent `gh pr view`
# cross-check against the resolved plane's own repo is the second,
# independent line of defense — belt-and-braces, not either/or.
#
# Why these trees are not registered (D#2041)
# ---------------------------------------------
# pr_tree_provision never calls `worktree_registry register`, and this file
# has no teardown function at all — so there is nothing to deregister on. An
# earlier draft of D#2041 proposed adding that register/deregister pair; it
# was dropped because there is no teardown to hook a deregister into, and a
# `worktrees.json` entry with no writer to ever clear it would protect the
# tree permanently — trading a reap-too-early risk for a never-reap-again
# one, which is worse given the worktree cap is already under pressure.
#
# What actually protects a pr-tree, since it is never in worktrees.json:
#   - the mtime guard in scripts/sweep-stale-worktrees.sh (>= 1h old)
#   - the commits-behind guard there (must be > the stale threshold)
#   - the tracked-changes ("dirty") guard there (uncommitted work is kept)
#   - the unpushed-commit guard there (D#2041) — a commit made *inside* this
#     tree that exists on no remote-tracking ref is kept even when the tree
#     is otherwise clean and stale, which is the gap a `--detach` checkout
#     otherwise leaves: no branch is left pointing at such a commit once the
#     worktree itself is removed.
# None of these read worktrees.json. A pr-tree is exactly as protected as any
# other unregistered worktree under .claude/worktrees/ — see
# scripts/lib/worktree-registry.sh's own note that the registry has no
# production caller.

_prt_log() { printf 'pr-tree: %s\n' "$*" >&2; }
_prt_repo_root() { (cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd); }
_prt_abs() { readlink -f "$1" 2>/dev/null || printf '%s\n' "$1"; }

# shellcheck source=./repo-resolve.sh
source "$(dirname "${BASH_SOURCE[0]}")/repo-resolve.sh"

# _prt_headref_lookup <repo> <pr_number> — the one call site that actually
# talks to `gh`, kept in its own scope (same shape as scripts/lib/pr-plane.sh's
# _prp_pr_exists) so which plane's repo string reaches it is always a
# parameter, never a same-scope case-branch assignment straight into the
# `gh` invocation.
#
# This parameter boundary is also why repo-plane-cutover-guard.py cannot
# trace <repo> back to a CODE/DISCUSSION-classified assignment from here: the
# call one level up (_prt_expected_head_sha's own case-branch binding) is
# fully checkable, but a function argument is "unknown" to that detector by
# construction — it reports NEEDS CALLER TRACE, not a defect. What actually
# guarantees <repo> is plane-correct is the real caller graph, not the guard —
# and that graph is documented, tree-wide, in ts-backend/PARITY-CAVEATS.md §7
# rather than restated here, so this comment and that caveat can't drift apart
# the way an earlier draft of this comment (which claimed two callers, both in
# scripts/spawn-agent.sh) already had by the time it was reviewed. Per that
# caveat, _prt_expected_head_sha is called only by pr_tree_provision, which has
# exactly one call site in scripts/spawn-agent.sh (the --dry-run-env-dump
# block; D#2542 removed the main-spawn-path call), passing the literal
# $PR_PLANE_NAME that script resolves once via pr_plane_resolve() beforehand —
# plus three docs-writer consumers the caveat also names
# (backend/spawn_templates/docs-writer.tmpl, agents/docs-writer.md,
# .claude/agents/docs-writer.md), none of which go through pr_plane_resolve():
# each passes the literal plane name "code" instead, matching the
# {{CODE_REPO}}/_resolve_code_repo call directly above it in the same snippet.
# The docs-writer lane is code-plane-bound by construction — wiki pages are
# synced from the code plane only — so a literal "code" here is correct, not a
# shortcut; making it plane-generic is out of scope for this file. Re-verify
# against the caveat (and search tree-wide, not just scripts/, since .tmpl and
# .md consumers live outside it) before trusting a restatement of this again.
_prt_headref_lookup() {
  local repo="$1" pr_number="$2"
  gh pr view "$pr_number" --repo "$repo" --json headRefOid --jq .headRefOid 2>/dev/null
}

# _prt_expected_head_sha <pr_number> <plane> — the resolved plane's
# authoritative headRefOid for <pr_number>, or non-zero with nothing on
# stdout on failure.
#
# PRT_EXPECTED_HEAD_OVERRIDE — test-only escape hatch, same convention as
# CODE_PLANE_REMOTE_OVERRIDE: when set, its value is returned as-is and no
# `gh pr view` call is made, so fixtures can exercise pr_tree_provision's
# cross-check without hitting the live API.
#
# Gated on `-n` alone, deliberately not also on PYTEST_CURRENT_TEST — see
# CODE_PLANE_REMOTE_OVERRIDE's comment in scripts/lib/repo-resolve.sh for the
# full reasoning: the only consumers are bash suites run directly (never
# under pytest), so that check would break the real consumer while adding no
# production safety.
_prt_expected_head_sha() {
  local pr_number="${1:-}" plane="${2:-}"
  [ -n "$pr_number" ] && [ -n "$plane" ] || { _prt_log "usage: _prt_expected_head_sha <pr_number> <plane>"; return 3; }

  if [ -n "${PRT_EXPECTED_HEAD_OVERRIDE:-}" ]; then
    printf '%s\n' "$PRT_EXPECTED_HEAD_OVERRIDE"
    return 0
  fi

  local repo sha
  case "$plane" in
    code)
      repo="$(_require_code_repo "pr-tree provisioning")" || return 1
      ;;
    discussion)
      repo="$(_resolve_discussion_repo 2>/dev/null || true)"
      if [ -z "$repo" ]; then
        _prt_log "could not resolve the Discussion plane — refusing the cross-check against an unresolved plane"
        return 1
      fi
      ;;
    *)
      _prt_log "unrecognised plane '$plane' — must be 'code' or 'discussion'"
      return 1
      ;;
  esac
  sha="$(_prt_headref_lookup "$repo" "$pr_number")"
  if [ -z "$sha" ]; then
    _prt_log "could not resolve PR #${pr_number}'s headRefOid from the resolved ${plane} plane ($repo)"
    return 1
  fi
  printf '%s\n' "$sha"
}

# pr_tree_provision <pr_number> <head_sha> <dest> <plane> [parent_repo]
pr_tree_provision() {
  local pr_number="${1:-}" head_sha="${2:-}" dest="${3:-}" plane="${4:-}"
  local parent="${5:-${PR_TREE_PARENT:-$(_prt_repo_root)}}"

  if [ -z "$pr_number" ] || [ -z "$head_sha" ] || [ -z "$dest" ] || [ -z "$plane" ]; then
    _prt_log "usage: pr_tree_provision <pr_number> <head_sha> <dest> <plane> [parent_repo]"
    return 3
  fi

  local remote
  case "$plane" in
    code)
      remote="$(_resolve_code_plane_remote "$parent")" || {
        _prt_log "could not resolve the code plane's remote in $parent"
        return 3
      }
      ;;
    discussion)
      remote="origin"
      ;;
    *)
      _prt_log "unrecognised plane '$plane' — must be 'code' or 'discussion'"
      return 3
      ;;
  esac

  if [ -e "$dest" ]; then
    _prt_log "refusing to provision over an existing path: $dest"
    return 3
  fi

  # Plane-qualified ref, not a bare local branch: two different PRs sharing
  # the same number on different planes must never land in the same ref.
  local qualified_ref="refs/pr-tree/${plane}/${pr_number}"
  if ! git -C "$parent" fetch --quiet "$remote" "refs/pull/${pr_number}/head:${qualified_ref}" 2>/dev/null; then
    _prt_log "fetch of refs/pull/${pr_number}/head from remote '$remote' (plane=$plane) into $qualified_ref failed against $parent"
    return 3
  fi

  # Verify against the ref the fetch just wrote — never the object store's
  # prior contents (D#1940 FM-5, D#2563): a commit already reachable in
  # $parent for any other reason must never let this pass.
  local fetched
  fetched="$(git -C "$parent" rev-parse --verify --quiet "$qualified_ref" 2>/dev/null)"
  if [ -z "$fetched" ] || [ "$fetched" != "$head_sha" ]; then
    _prt_log "PR #${pr_number} head ${head_sha} does not match what the fetch landed at ${qualified_ref} (got: ${fetched:-nothing}) — refusing"
    return 3
  fi

  # Belt-and-braces (D#1940 FM-5): independently re-verify the caller-supplied
  # head_sha against a live headRefOid lookup on the resolved plane's own
  # repo. Objects fetched under an old remote naming convention could already
  # sit in the parent's object store from a prior era; this second,
  # independent line of defense catches that even if the plane-qualified-ref
  # check above were ever bypassed.
  local expected_sha
  expected_sha="$(_prt_expected_head_sha "$pr_number" "$plane")" || {
    _prt_log "could not verify PR #${pr_number}'s head_sha argument against the resolved ${plane} plane — refusing to trust an unverified sha"
    return 3
  }
  if [ "$expected_sha" != "$head_sha" ]; then
    _prt_log "PR #${pr_number} head_sha argument ($head_sha) does not match the ${plane} plane's headRefOid ($expected_sha) — refusing; the caller likely resolved this sha against the wrong repo plane"
    return 3
  fi

  if ! mkdir -p "$(dirname "$dest")" 2>/dev/null; then
    _prt_log "could not create parent directory for $dest"
    return 3
  fi

  local wt_err
  if ! wt_err="$(git -C "$parent" worktree add --quiet --detach "$dest" "$head_sha" 2>&1)"; then
    _prt_log "git worktree add failed: $wt_err"
    return 3
  fi

  local got
  got="$(git -C "$dest" rev-parse HEAD 2>/dev/null)"
  if [ "$got" != "$head_sha" ]; then
    _prt_log "worktree landed on $got, expected $head_sha — removing $dest"
    git -C "$parent" worktree remove --force "$dest" 2>/dev/null || rm -rf "$dest"
    return 3
  fi

  local origin_url
  origin_url="$(git -C "$dest" remote get-url origin 2>/dev/null)"
  _prt_log "provisioned $dest at $head_sha (PR #$pr_number, plane=$plane, remote=$remote, origin=$origin_url)"
  printf '%s\n' "$(_prt_abs "$dest")"
  return 0
}
