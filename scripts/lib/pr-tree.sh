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
#
# Which remote (D#1940 FM-5)
# ---------------------------
# PR numbers are not unique across planes: the code plane and the Discussion
# plane each number their own PRs from 1, so "PR #67" names two different
# commits. Fetching `refs/pull/<N>/head` from the git remote literally named
# "origin" (the Discussion plane) succeeds — exit 0 — and silently lands the
# wrong plane's commit. This file resolves the code-plane remote via
# scripts/lib/repo-resolve.sh's `_resolve_code_plane_remote` instead of ever
# fetching from a hardcoded remote name, and independently re-verifies the
# fetched head against `gh pr view --repo "$(_require_code_repo)"` before
# handing the tree back to a caller — belt-and-braces, because objects fetched
# under an old remote name can already sit in the parent's object store from
# a prior era, which would otherwise let a wrong-plane head_sha resolve as
# "reachable" even after the fetch itself is correctly pinned.
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

# _prt_expected_head_sha <pr_number> — the code plane's authoritative
# headRefOid for <pr_number>, or non-zero with nothing on stdout on failure.
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
  local pr_number="${1:-}"
  [ -n "$pr_number" ] || { _prt_log "usage: _prt_expected_head_sha <pr_number>"; return 3; }

  if [ -n "${PRT_EXPECTED_HEAD_OVERRIDE:-}" ]; then
    printf '%s\n' "$PRT_EXPECTED_HEAD_OVERRIDE"
    return 0
  fi

  local repo sha
  repo="$(_require_code_repo "pr-tree provisioning")" || return 1
  sha="$(gh pr view "$pr_number" --repo "$repo" --json headRefOid --jq .headRefOid 2>/dev/null)"
  if [ -z "$sha" ]; then
    _prt_log "could not resolve PR #${pr_number}'s headRefOid from the code plane ($repo)"
    return 1
  fi
  printf '%s\n' "$sha"
}

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

  local code_remote
  code_remote="$(_resolve_code_plane_remote "$parent")" || {
    _prt_log "could not resolve the code-plane git remote in $parent — refusing to fetch against an unresolved plane"
    return 3
  }

  if ! git -C "$parent" fetch --quiet "$code_remote" "refs/pull/${pr_number}/head" 2>/dev/null; then
    _prt_log "fetch of refs/pull/${pr_number}/head failed against $parent's $code_remote remote"
    return 3
  fi

  if ! git -C "$parent" rev-parse --verify --quiet "${head_sha}^{commit}" >/dev/null 2>&1; then
    _prt_log "PR #${pr_number} head $head_sha is not reachable in $parent after fetch"
    return 3
  fi

  local expected_sha
  expected_sha="$(_prt_expected_head_sha "$pr_number")" || {
    _prt_log "could not verify PR #${pr_number}'s head_sha argument against the code plane — refusing to trust an unverified sha"
    return 3
  }
  if [ "$expected_sha" != "$head_sha" ]; then
    _prt_log "PR #${pr_number} head_sha argument ($head_sha) does not match the code plane's headRefOid ($expected_sha) — refusing; the caller likely resolved this sha against the wrong repo plane"
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
