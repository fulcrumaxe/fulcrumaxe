#!/usr/bin/env bash
# scripts/lib/muse-tl-worktree.sh — Team-Lead-provisioned agent worktrees.
#
# Fallback for sessions whose host cannot do native subagent worktree
# isolation: `muse serve` hosts (e.g. Helicon) in muse <= 1.4.14 reject
# isolated spawns (`isolation_not_enabled_in_profile`) and expose no flag
# or setting to enable it (D#368). Team Lead provisions the worktree
# itself, registers it, and confines the executor to the path via the
# spawn brief. The executor prompt's "already provisioned" step is then
# literally true; nothing is hand-rolled at spawn time.
#
# Usage:
#   bash scripts/lib/muse-tl-worktree.sh provision --id <event-id> \
#     --role <role> --discussion <N> --branch <branch> \
#     [--base <ref, default origin/main>] [--path <dir>]
#     → prints the absolute worktree path on success (sole stdout line)
#   bash scripts/lib/muse-tl-worktree.sh release --id <event-id> \
#     [--status merged|discarded, default discarded]
#
# provision refuses when: the path exists, the branch exists, the base ref
# does not resolve, the fetch fails, the worktree add fails, or the new
# HEAD differs from the base (rolls the worktree back). release refuses
# when the id has no registry entry.
#
# Single-executor rule: never provision a second executor worktree while
# one is active. The brief-confined child shares your shell profile, so
# parallel writers can still collide outside their trees.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGISTRY="$SCRIPT_DIR/worktree-registry.sh"

die() { echo "muse-tl-worktree: $*" >&2; exit 1; }

_caller_root() {
  git rev-parse --show-toplevel 2>/dev/null || die "not inside a git repo"
}

cmd_provision() {
  local id="" role="" discussion="" branch="" base="origin/main" path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --id) id="$2"; shift 2 ;;
      --role) role="$2"; shift 2 ;;
      --discussion) discussion="$2"; shift 2 ;;
      --branch) branch="$2"; shift 2 ;;
      --base) base="$2"; shift 2 ;;
      --path) path="$2"; shift 2 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  [[ -n "$id" && -n "$role" && -n "$discussion" && -n "$branch" ]] \
    || die "provision requires --id --role --discussion --branch"

  local root
  root="$(_caller_root)"
  export _WTR_REPO_ROOT="${_WTR_REPO_ROOT:-$root}"
  [[ -z "$path" ]] && path="$root/.muse/worktrees/$id"
  [[ "$path" != /* ]] && path="$root/$path"

  [[ -e "$path" ]] && die "path already exists: $path"
  if git -C "$root" show-ref --verify --quiet "refs/heads/$branch"; then
    die "branch already exists: $branch"
  fi
  git -C "$root" fetch -q origin 2>/dev/null \
    || die "fetch origin failed — refusing to base on a possibly stale tip"
  local base_sha
  base_sha="$(git -C "$root" rev-parse --verify "$base^{commit}" 2>/dev/null)" \
    || die "base ref does not resolve: $base"

  git -C "$root" worktree add -q "$path" -b "$branch" "$base" 2>/dev/null \
    || die "git worktree add failed for $path"
  local head_sha
  head_sha="$(git -C "$path" rev-parse HEAD 2>/dev/null || echo MISSING)"
  if [[ "$head_sha" != "$base_sha" ]]; then
    git -C "$root" worktree remove --force "$path" 2>/dev/null || true
    die "new HEAD ($head_sha) != base ($base_sha) — rolled back"
  fi

  # --pid is vestigial (parent-pid liveness archived 2026-08-24) but the
  # registry parser requires it; the entry id is the join key that matters.
  bash "$REGISTRY" register --id "$id" --role "$role" --path "$path" \
    --pid "$$" --discussion "$discussion" --branch "$branch" \
    --base "$base" >/dev/null 2>&1 \
    || { git -C "$root" worktree remove --force "$path" 2>/dev/null || true;
         die "registry register failed — rolled back"; }

  printf '%s\n' "$path"
}

cmd_release() {
  local id="" status="discarded"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --id) id="$2"; shift 2 ;;
      --status) status="$2"; shift 2 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  [[ -n "$id" ]] || die "release requires --id"
  case "$status" in
    active|committed|pushed|merged|orphaned|discarded) ;;
    *) die "invalid status: $status" ;;
  esac

  local root
  root="$(_caller_root)"
  export _WTR_REPO_ROOT="${_WTR_REPO_ROOT:-$root}"
  local reg="$_WTR_REPO_ROOT/.autonomous-team/worktrees.json"
  local path
  path="$(python3 -c "import json,sys; print('\n'.join(e.get('path','') for e in json.load(open(sys.argv[1])) if e.get('worktree_id')==sys.argv[2]))" "$reg" "$id" 2>/dev/null)" \
    || die "cannot read registry: $reg"
  [[ -n "$path" ]] || die "unknown worktree id: $id"

  if git -C "$root" worktree list --porcelain 2>/dev/null | grep -q "^worktree $path$"; then
    git -C "$root" worktree remove --force "$path" 2>/dev/null \
      || die "git worktree remove failed for $path"
    if git -C "$root" worktree list --porcelain 2>/dev/null | grep -q "^worktree $path$"; then
      die "worktree still listed after remove: $path"
    fi
  fi
  bash "$REGISTRY" mark-status "$id" "$status" >/dev/null 2>&1 \
    || die "registry mark-status failed for $id"
  echo "released: $id ($status)"
}

case "${1:-}" in
  provision) shift; cmd_provision "$@" ;;
  release) shift; cmd_release "$@" ;;
  *) die "usage: muse-tl-worktree.sh {provision|release} ..." ;;
esac
