#!/usr/bin/env bash
# scripts/lib/pr-browser-tree.sh — materialize a code-plane PR's head into a
# scratch tree for browser testing (D#2549).
#
# Why this exists: the long-running vite dev server on 5173 serves the
# shared checkout on main, not any PR's code — every "browser test"
# screenshot of 5173 was a screenshot of main (measured, D#2549). Nothing
# here restarts, rebinds, or touches that server or its ports; this only
# ever builds a SEPARATE, private tree for a NEW vite instance to serve.
#
# scripts/pr-browser-preview.sh is the CLI. This file holds the pieces worth
# unit-testing without a real network call or a real npm/vite install:
#
#   pbt_fetch_head <pr_number> <remote> [repo_dir]
#     Fetches refs/pull/<pr_number>/head from <remote> into a repo-local ref
#     (refs/pr-browser-preview/<pr_number>) and prints the resolved commit
#     SHA on stdout. <remote> must be the code-plane remote name (resolve it
#     with _resolve_code_plane_remote from repo-resolve.sh) — fetching a PR
#     ref from the wrong remote is exactly the kind of silent wrong-plane
#     read the repo-scope card warns about, so this function never guesses
#     a remote name itself.
#
#   pbt_materialize <sha> <dest> [parent_repo]
#     Thin wrapper over verify_tree_build (scripts/lib/verify-tree.sh) — the
#     same clone+protect+snapshot mechanism already trusted for read-only PR
#     inspection, reused here rather than re-implemented, so a PR head
#     served for browser-testing is provably the commit it claims to be and
#     is write-protected against drifting under the vite process that is
#     about to be pointed at it.
#
#   pbt_pick_free_port
#     Binds an OS-assigned free port on ::1 (matching vite's own bind
#     address — see scripts/start-dashboard.sh's Bug 1 note) and prints it,
#     falling back to 127.0.0.1 when IPv6 is unavailable. The OS never hands
#     out a port already bound by another process, so this can never collide
#     with 5173 or anything else already listening.
#
# Sourcing this file also sources scripts/lib/verify-tree.sh, which
# pbt_materialize depends on.

_pbt_lib_dir() { cd "$(dirname "${BASH_SOURCE[0]}")" && pwd; }
# shellcheck source=scripts/lib/verify-tree.sh
source "$(_pbt_lib_dir)/verify-tree.sh"

_pbt_log() { printf 'pr-browser-tree: %s\n' "$*" >&2; }

# pbt_fetch_head <pr_number> <remote> [repo_dir]
pbt_fetch_head() {
  local pr="${1:-}" remote="${2:-}" dir="${3:-.}"
  if [[ -z "$pr" || -z "$remote" ]]; then
    _pbt_log "usage: pbt_fetch_head <pr_number> <remote> [repo_dir]"
    return 2
  fi

  local ref="refs/pr-browser-preview/${pr}"
  local err
  if ! err="$(git -C "$dir" fetch --quiet "$remote" "+refs/pull/${pr}/head:${ref}" 2>&1)"; then
    _pbt_log "fetch of refs/pull/${pr}/head from remote '$remote' failed: $err"
    return 1
  fi

  local sha
  sha="$(git -C "$dir" rev-parse --verify --quiet "${ref}^{commit}" 2>/dev/null)"
  if [[ -z "$sha" ]]; then
    _pbt_log "fetched $ref from '$remote' but it does not resolve to a commit"
    return 1
  fi
  printf '%s\n' "$sha"
}

# pbt_materialize <sha> <dest> [parent_repo]
pbt_materialize() {
  local sha="${1:-}" dest="${2:-}" parent="${3:-}"
  if [[ -z "$sha" || -z "$dest" ]]; then
    _pbt_log "usage: pbt_materialize <sha> <dest> [parent_repo]"
    return 3
  fi
  verify_tree_build "$sha" "$dest" "$parent"
}

# pbt_pick_free_port — print a free TCP port, preferring ::1 (vite's own
# bind address) with a 127.0.0.1 fallback for hosts without IPv6.
pbt_pick_free_port() {
  python3 - <<'PYEOF'
import socket
import sys

for fam, addr in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, 0))
        print(s.getsockname()[1])
        s.close()
        sys.exit(0)
    except OSError:
        continue
sys.exit(1)
PYEOF
}
