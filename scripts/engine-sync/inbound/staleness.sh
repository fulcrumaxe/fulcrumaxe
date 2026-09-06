#!/usr/bin/env bash
# staleness.sh -- Slice A of D#2439 (engine-sync inbound).
#
# READ-ONLY. Reports whether the marker ref (the last code-plane commit the
# inbound sync has processed) is behind the code plane's current `main` tip.
#
# It never writes anything, never applies a patch, and never spawns an
# agent. Its only network operation is one `git ls-remote` -- it does NOT
# fetch objects; it assumes the marker's and the remote tip's commit objects
# are already present locally (the engine repo already has a `code-plane`
# remote that gets fetched routinely). If the remote tip's object is not
# present locally, that is treated the same as an unreachable remote:
# undecidable, never in-sync.
#
# Usage:
#   staleness.sh
#
# Env overrides (mainly for tests):
#   ENGINE_SYNC_MARKER_REF     default: refs/synced/code-plane
#   ENGINE_SYNC_REMOTE         default: code-plane
#   ENGINE_SYNC_REMOTE_BRANCH  default: main
#   ENGINE_SYNC_GIT_DIR        if set, run all git commands as if invoked
#                              from this directory instead of the caller's
#                              cwd -- lets a test point this script at a
#                              throwaway repo without touching the real one.
#
# Exit codes:
#   0  in-sync      -- marker == remote tip (behind == 0)
#   1  stale         -- marker is behind the remote tip (behind > 0)
#   2  undecidable   -- marker or remote tip could not be resolved. NEVER
#                       conflated with in-sync -- a transport failure must
#                       never produce exit 0.
#
# stdout: exactly one line of JSON, always, on every exit path:
#   {"status": "in-sync"|"stale"|"undecidable", "behind": <int>|null, ...}

set -uo pipefail

MARKER_REF="${ENGINE_SYNC_MARKER_REF:-refs/synced/code-plane}"
REMOTE="${ENGINE_SYNC_REMOTE:-code-plane}"
REMOTE_BRANCH="${ENGINE_SYNC_REMOTE_BRANCH:-main}"

if [ -n "${ENGINE_SYNC_GIT_DIR:-}" ]; then
  if ! cd "$ENGINE_SYNC_GIT_DIR" 2>/dev/null; then
    printf '{"status":"undecidable","behind":null,"reason":"ENGINE_SYNC_GIT_DIR not accessible"}\n'
    exit 2
  fi
fi

emit() {
  # emit <status> <behind-int-or-null> [extra-json-fields-with-leading-comma]
  printf '{"status":"%s","behind":%s%s}\n' "$1" "$2" "${3:-}"
}

marker_sha="$(git rev-parse --verify -q "${MARKER_REF}^{commit}" 2>/dev/null)"
if [ -z "$marker_sha" ]; then
  emit "undecidable" "null" ",\"reason\":\"marker ref not resolvable: ${MARKER_REF}\""
  exit 2
fi

remote_line="$(git ls-remote --exit-code "$REMOTE" "refs/heads/${REMOTE_BRANCH}" 2>/dev/null)"
rc=$?
if [ "$rc" -ne 0 ] || [ -z "$remote_line" ]; then
  emit "undecidable" "null" ",\"reason\":\"ls-remote failed for remote ${REMOTE}\""
  exit 2
fi
remote_sha="$(printf '%s' "$remote_line" | awk '{print $1}')"

if ! git cat-file -e "${remote_sha}^{commit}" 2>/dev/null; then
  emit "undecidable" "null" ",\"reason\":\"remote tip ${remote_sha} not present locally\""
  exit 2
fi

behind="$(git rev-list --count "${marker_sha}..${remote_sha}" 2>/dev/null)"
if [ -z "$behind" ]; then
  emit "undecidable" "null" ",\"reason\":\"rev-list failed for ${marker_sha}..${remote_sha}\""
  exit 2
fi

if [ "$behind" -eq 0 ]; then
  emit "in-sync" "$behind"
  exit 0
fi

emit "stale" "$behind"
exit 1
