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
#   DEST="$(pr_tree_provision "$PR_NUMBER" "$HEAD_SHA" "$dest_path")" || exit 1
#
# pr_tree_provision <pr_number> <head_sha> <dest> [parent_repo]
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

_prt_log() { printf 'pr-tree: %s\n' "$*" >&2; }
_prt_repo_root() { (cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd); }
_prt_abs() { readlink -f "$1" 2>/dev/null || printf '%s\n' "$1"; }

# pr_tree_provision <pr_number> <head_sha> <dest> [parent_repo]
pr_tree_provision() {
  local pr_number="${1:-}" head_sha="${2:-}" dest="${3:-}"
  local parent="${4:-${PR_TREE_PARENT:-$(_prt_repo_root)}}"

  if [ -z "$pr_number" ] || [ -z "$head_sha" ] || [ -z "$dest" ]; then
    _prt_log "usage: pr_tree_provision <pr_number> <head_sha> <dest> [parent_repo]"
    return 3
  fi

  if [ -e "$dest" ]; then
    _prt_log "refusing to provision over an existing path: $dest"
    return 3
  fi

  if ! git -C "$parent" fetch --quiet origin "refs/pull/${pr_number}/head" 2>/dev/null; then
    _prt_log "fetch of refs/pull/${pr_number}/head failed against $parent's origin"
    return 3
  fi

  if ! git -C "$parent" rev-parse --verify --quiet "${head_sha}^{commit}" >/dev/null 2>&1; then
    _prt_log "PR #${pr_number} head $head_sha is not reachable in $parent after fetch"
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
  _prt_log "provisioned $dest at $head_sha (PR #$pr_number, origin=$origin_url)"
  printf '%s\n' "$(_prt_abs "$dest")"
  return 0
}
