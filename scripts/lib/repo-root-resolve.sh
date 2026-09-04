#!/usr/bin/env bash
# scripts/lib/repo-root-resolve.sh — canonical checkout-path resolver (shell).
#
# Shell twin of backend/repo_root.py, and the shell counterpart to
# scripts/lib/repo-resolve.sh: that one resolves the repo *slug*, this one
# resolves the repo *path*. Read repo_root.py's module docstring for the
# reasoning; this file keeps only what differs in shell.
#
# Usage (source this file, then call the functions):
#   source "$(dirname "${BASH_SOURCE[0]}")/repo-root-resolve.sh"
#   ROOT="$(_resolve_repo_root)"
#   MAIN="$(_resolve_main_repo_root)"
#
# _resolve_repo_root       the checkout the caller is running in; inside a
#                          linked git working tree, that linked tree.
# _resolve_main_repo_root  the checkout a linked working tree was branched
#                          from; equal to _resolve_repo_root outside one.
#
# Resolution is anchored to this file's own location, captured at *source*
# time (see below), never to $PWD at call time, so a script invoked from
# inside some other repository still resolves this tree. Neither function
# fails: the floor is the anchor itself, which is correct by construction
# because this file lives at <root>/scripts/lib/.
#
# Physical paths (`pwd -P`) throughout, matching both `git rev-parse
# --show-toplevel` and Python's Path.resolve(), so the two resolvers agree
# byte-for-byte on a tree reached through a symlink.

# Anchor: <root>/scripts/lib/ → two parents up.
#
# This runs at SOURCE time, at file scope, and that timing is the whole point.
# BASH_SOURCE[0] names this file, but when the caller sources us by a relative
# path it is itself relative — and a relative path is only meaningful against
# the cwd that was current when the source happened. Resolving it inside a
# function instead would re-resolve it against wherever the caller had wandered
# to by the time it called us: measured, sourcing relatively and then cd'ing
# into a sibling checkout of this same project silently returned the sibling,
# and cd'ing somewhere unrelated returned nothing with rc=1. Capturing here
# freezes the answer while the relative path is still valid.
#
# The `:=` floor only fires if this file's own directory cannot be entered at
# source time — a tree that has been deleted out from under the running shell.
# It is the single place $PWD is consulted, and it exists so the "neither
# function fails" contract above holds even then.
_REPO_ROOT_RESOLVE_ANCHOR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd -P)"
: "${_REPO_ROOT_RESOLVE_ANCHOR:=$PWD}"

# Cap every git call the way the Python twin does (subprocess timeout=10), so a
# wedged git cannot hang a sourced caller indefinitely. `timeout` exits 124 when
# it fires, which every call site below already treats as "git cannot answer".
_repo_root_resolve__git() {
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=5s 10 git "$@" 2>/dev/null
  else
    git "$@" 2>/dev/null
  fi
}

# Normalise an AUTONOMOUS_TEAM_REPO_ROOT value to the same physical absolute
# path Python's Path(env).expanduser().resolve() produces.
#
# Without this the shell echoed the raw string while Python normalised it, so
# every non-canonical spelling diverged — a trailing slash, `./`, `..`, `~`, or
# a relative path. The trailing slash is the likeliest real spelling and the
# one that bites: the contamination classifier prefix-matches write paths
# against this value, and `/repo/` is not the same prefix as `/repo`.
#
# `~user` (as opposed to `~` and `~/`) is deliberately not expanded here;
# bash does not expand it in a variable and Python does. It falls through to
# the absolute-ising branch below rather than being silently mangled into
# "$HOMEuser". No caller has ever set it that way.
_repo_root_resolve__normalise() {
  local raw="$1" expanded p
  case "$raw" in
    "~")   expanded="$HOME" ;;
    "~/"*) expanded="$HOME/${raw#\~/}" ;;
    *)     expanded="$raw" ;;
  esac

  if p="$(cd "$expanded" 2>/dev/null && pwd -P)"; then
    printf '%s\n' "$p"
    return 0
  fi

  # The path does not exist, or is not a directory, so `cd` cannot normalise
  # it. Still never emit a relative path — returning one is precisely the
  # defect class this module exists to remove.
  if [[ "$expanded" == /* ]]; then
    printf '%s\n' "$expanded"
  else
    printf '%s\n' "$PWD/$expanded"
  fi
}

_repo_root_resolve__anchor() {
  printf '%s\n' "$_REPO_ROOT_RESOLVE_ANCHOR"
}

_resolve_repo_root() {
  if [[ -n "${AUTONOMOUS_TEAM_REPO_ROOT:-}" ]]; then
    _repo_root_resolve__normalise "$AUTONOMOUS_TEAM_REPO_ROOT"
    return 0
  fi

  local anchor top
  anchor="$(_repo_root_resolve__anchor)"

  if top="$(_repo_root_resolve__git -C "$anchor" rev-parse --show-toplevel)" && [[ -n "$top" ]]; then
    printf '%s\n' "$top"
    return 0
  fi

  printf '%s\n' "$anchor"
}

_resolve_main_repo_root() {
  local root common parent
  root="$(_resolve_repo_root)"

  # --path-format=absolute needs git 2.31+; the bare form is the fallback and
  # may answer with a path relative to the work tree.
  common="$(_repo_root_resolve__git -C "$root" rev-parse --path-format=absolute --git-common-dir)" || common=""
  if [[ -z "$common" ]]; then
    common="$(_repo_root_resolve__git -C "$root" rev-parse --git-common-dir)" || common=""
  fi
  if [[ -z "$common" ]]; then
    printf '%s\n' "$root"
    return 0
  fi
  [[ "$common" != /* ]] && common="$root/$common"

  # A shared git dir not named .git is a bare repo or a --separate-git-dir
  # layout, where the parent directory is not a checkout at all.
  if [[ "$(basename "$common")" != ".git" ]]; then
    printf '%s\n' "$root"
    return 0
  fi

  parent="$(cd "$common/.." 2>/dev/null && pwd -P)" || parent=""
  if [[ -z "$parent" || ! -d "$parent" ]]; then
    printf '%s\n' "$root"
    return 0
  fi

  printf '%s\n' "$parent"
}
