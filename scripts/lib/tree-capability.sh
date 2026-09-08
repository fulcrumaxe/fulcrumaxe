#!/usr/bin/env bash
# scripts/lib/tree-capability.sh — assert a materialised tree is CAPABLE of
# producing a trustworthy comparison or test result (D#1940 PR-b).
#
#   source scripts/lib/tree-capability.sh
#   tree_capability_assert <dir> [<expected_sha>]
#
# D#1940's re-derivation found a comparison tree can be incapable in five
# independent ways — a fix for one is not a fix for another. FM-5 (wrong repo
# plane) is closed separately, in scripts/lib/repo-resolve.sh's
# _resolve_code_plane_remote and scripts/lib/pr-tree.sh's pr_tree_provision
# (D#1940 PR-a, code-plane PR #74). This file closes the remaining four, all
# about the MATERIALISED TREE ITSELF rather than which remote it came from:
#
#   FM-1 no git metadata     — a `git archive HEAD | tar -x` extraction has
#                               no .git. backend/repo_root.py resolves
#                               through `git rev-parse --show-toplevel`; with
#                               no git dir the hook subprocess classifies its
#                               cwd differently from the in-process fixture,
#                               and confidently reports the wrong test
#                               result. Measured on this repo at 7471d5de:
#                               6 failed/16 passed (archive extraction) vs
#                               22 passed (real checkout), same commit, same
#                               scratch AUTONOMOUS_TEAM_STATE_DIR.
#   FM-2 synthetic history   — `git archive` + `git init` yields a single
#                               parentless root commit: every differential
#                               against it compares against nothing. Already
#                               avoided by verify_tree_build (real clone, not
#                               archive-plus-init) — this is the assertion
#                               that would catch a caller who skipped it.
#   FM-3 commit not present  — the tree has real git metadata, but the
#                               caller's expected commit is not reachable in
#                               it (stale, wrong, or half-built clone).
#   FM-4 unresolvable base   — the review protocol's comparison base is the
#                               code plane's `main`. If a tree that has
#                               otherwise fetched remote branches still can't
#                               resolve that base (fetch skipped, wrong
#                               remote, network hiccup), no differential
#                               against "main" can mean anything, even though
#                               the tree itself is otherwise fine.
#
# tree_capability_assert examines the tree in the order above and returns on
# the FIRST failure — one shape at a time, not a bundle, because a caller
# fixing FM-1 gains nothing from being told about FM-4 in the same breath.
#
# Exit codes
# ----------
#   0  capable — a one-line evidence record was emitted to stderr (below)
#   1  FM-1 — no git metadata ("no git metadata")
#   2  FM-2 — synthetic history ("synthetic history")
#   3  FM-3 — expected commit not reachable ("commit not present")
#   4  FM-4 — comparison base unresolvable ("unresolvable comparison base")
#   5  usage error (missing <dir>)
# Mirrors scripts/lib/verify-tree.sh's convention of a small, closed set of
# documented exit codes rather than a single boolean — a caller can tell
# "wrong shape" from "wrong commit" from "usage mistake" without parsing
# stderr text.
#
# What "capable" does NOT mean (item 11: this records what it examined, not
# just a code) — read before trusting a 0:
#   - It does not run any test and does not check test OUTPUT. It only
#     checks that the tree COULD produce a trustworthy result.
#   - It does not check content drift. A tree that passed this, then had a
#     file silently rewritten mid-run, is verify-tree.sh's job
#     (verify_tree_assert) on a different axis: this fires on a tree that
#     was never valid to begin with; verify-tree.sh fires on a tree that WAS
#     valid and then changed under you. Neither substitutes for the other.
#   - FM-4 is skipped, not failed, when the tree has NO remote-tracking refs
#     at all (`refs/remotes/*` empty) — the shape verify_tree_build
#     deliberately produces (`git clone --revision=<sha>` makes no
#     remote-tracking branch by design, so it can protect+hash a tree with
#     real ancestor history without ever claiming to know what "main" is).
#     A verify_tree_build tree is exactly as capable as this check can
#     determine; it is not exempted from FM-1/2/3, only from a base check
#     that does not apply to a tree that never claimed to track one.
#     Practical consequence, stated plainly rather than left implicit: every
#     call site wired up so far (code-reviewer.tmpl, security-reviewer.tmpl)
#     calls this ONLY on verify_tree_build trees, so as currently wired FM-4
#     never actually fires in production — it is exercised here only by this
#     file's own dedicated fixture, which deliberately builds a tree WITH
#     remote-tracking refs to reach it. A caller that materialises a tree
#     with `git fetch`/`git worktree add` (real remote-tracking refs) instead
#     of `verify_tree_build` is the one that would actually exercise this
#     path; none does yet.
#   - Known hole rather than assumed cover: a tree with real git metadata,
#     real ancestor history, and no remote-tracking refs, whose HEAD is
#     simply the WRONG (but well-formed, non-root) commit, passes here when
#     no <expected_sha> is given — this check cannot invent a fact the
#     caller never told it. Always pass <expected_sha> when the caller knows
#     one; FM-3 is exactly the check that closes this hole when it does.
#   - Does not itself resolve the code plane's slug — FM-4 reuses
#     scripts/lib/repo-resolve.sh's `_resolve_code_plane_remote`, the one
#     existing resolver (D#1940's own constraint: no second one), rather
#     than hardcoding a remote name. A remote literally named "origin" is
#     the common case for a fresh single-remote clone but is NOT assumed —
#     this checkout's own "origin" is the Discussion plane, not the code
#     plane, which is exactly the shape a hardcoded "origin" would get wrong.
#
# Model: scripts/lib/verify-tree.sh's shape (sourceable, documented exit
# codes, _vt_log-style stderr). Kept a separate file rather than folded in —
# see verify-tree.sh's own header for why materialisation and assertion are
# split there, and scripts/lib/pr-tree.sh:32-45 for the parallel split
# between the read-only and amend-a-PR mechanisms. tree-capability.sh is an
# assertion callers of EITHER can make; it does not build or fetch anything
# itself.

set -uo pipefail

_TCA_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$_TCA_LIB_DIR/repo-resolve.sh"

_tca_log() { printf 'tree-capability: %s\n' "$*" >&2; }

# tree_capability_assert <dir> [<expected_sha>]
tree_capability_assert() {
  local dir="${1:-}" expected_sha="${2:-}"
  if [ -z "$dir" ]; then
    _tca_log "usage: tree_capability_assert <dir> [<expected_sha>]"
    return 5
  fi

  # FM-1 — no git metadata (or HEAD does not resolve, same shape: a tree
  # with no usable git state at all).
  local gitdir
  if ! gitdir="$(git -C "$dir" rev-parse --git-dir 2>/dev/null)"; then
    _tca_log "FAIL — no git metadata in $dir (FM-1: no git metadata) — a \`git archive | tar -x\` extraction has no .git; anything that shells out to git resolves differently here than in a real checkout and reports confidently wrong results"
    return 1
  fi
  case "$gitdir" in
    /*) ;;
    *) gitdir="$dir/$gitdir" ;;
  esac

  local head_sha
  if ! head_sha="$(git -C "$dir" rev-parse --verify --quiet HEAD 2>/dev/null)"; then
    _tca_log "FAIL — git metadata present in $dir but HEAD does not resolve (FM-1: no git metadata)"
    return 1
  fi

  # FM-2 — synthetic history: HEAD is a parentless root commit, and it is
  # not the sha the caller actually asked for (a genuinely single-commit
  # repo the caller asked for by sha is not synthetic — it's what they got).
  local parent_line parent_count
  parent_line="$(git -C "$dir" log -1 --format='%P' HEAD 2>/dev/null)"
  if [ -z "$parent_line" ]; then
    parent_count=0
  else
    parent_count=$(printf '%s\n' "$parent_line" | wc -w)
  fi
  if [ "$parent_count" -eq 0 ]; then
    if [ -z "$expected_sha" ] || [ "$head_sha" != "$expected_sha" ]; then
      _tca_log "FAIL — HEAD ($head_sha) in $dir is a parentless root commit (FM-2: synthetic history) — an archive-plus-\`git init\` extraction yields exactly this shape; every differential against it compares against nothing"
      return 2
    fi
  fi

  # FM-3 — expected commit not reachable.
  if [ -n "$expected_sha" ]; then
    if ! git -C "$dir" cat-file -e "${expected_sha}^{commit}" 2>/dev/null; then
      _tca_log "FAIL — expected commit $expected_sha is not reachable in $dir (FM-3: commit not present)"
      return 3
    fi
  fi

  # FM-4 — unresolvable comparison base. Only applicable when the tree shows
  # evidence of tracking a remote at all (refs/remotes/* non-empty) — see
  # the header's "What capable does NOT mean" note for why a
  # verify_tree_build tree (zero remote-tracking refs by construction)
  # skips this rather than failing it.
  local base_desc="n/a (no remote-tracking refs)"
  local has_remote_refs
  has_remote_refs="$(git -C "$dir" for-each-ref --count=1 --format='x' refs/remotes 2>/dev/null)"
  if [ -n "$has_remote_refs" ]; then
    local base_remote
    if ! base_remote="$(_resolve_code_plane_remote "$dir" 2>/dev/null)"; then
      _tca_log "FAIL — could not resolve the code-plane remote in $dir (FM-4: unresolvable comparison base) — the tree has fetched remote branches but none identifies the code plane, so no base for a differential can be established"
      return 4
    fi
    local base_sha
    if ! base_sha="$(git -C "$dir" rev-parse --verify --quiet "${base_remote}/main" 2>/dev/null)" \
      || ! git -C "$dir" merge-base --is-ancestor "${base_remote}/main" HEAD 2>/dev/null; then
      _tca_log "FAIL — ${base_remote}/main is not an ancestor of HEAD in $dir (FM-4: unresolvable comparison base) — the review protocol's declared base could not be resolved against this tree; fetch main from the code-plane remote before diffing"
      return 4
    fi
    base_desc="${base_remote}/main @ ${base_sha}"
  fi

  # Item 11 — record what was examined. A run producing no such line is a
  # failed run: this is the only evidence that the checks above actually ran
  # against this dir, not a paraphrase of them.
  local has_parent="yes"
  [ "$parent_count" -eq 0 ] && has_parent="no"
  _tca_log "OK — git dir: $gitdir, HEAD: $head_sha, has parent: $has_parent, comparison base: $base_desc"
  return 0
}
