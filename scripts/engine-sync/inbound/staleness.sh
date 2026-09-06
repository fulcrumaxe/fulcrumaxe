#!/usr/bin/env bash
# staleness.sh -- is the engine checkout behind the code plane?
#
# Reports whether the marker ref (the last code-plane commit the inbound
# sync has processed) is behind the code plane's current `main` tip.
#
# It never writes the working tree, never writes the index, never moves a
# branch or the marker, never applies a patch, and never spawns an agent.
#
# It DOES fetch one ref. That is a deliberate change from the first version,
# which made a single `git ls-remote` call and nothing else. `ls-remote`
# returns the remote tip's *name*, not its *object*, so `git rev-list
# marker..tip` had nothing to walk and the check reported `undecidable` from
# the moment the code plane moved ahead until something else happened to
# fetch -- that is, exactly the window right after a merge, when drift has
# just increased and this check has the most to say. Read-only here means
# "no side effects on the working tree"; writing a remote-tracking ref and
# FETCH_HEAD into the object store is not that. The cost is one network
# round trip per call instead of one, against a ref that is usually already
# current.
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
#   ENGINE_SYNC_NO_FETCH       set to 1 to skip the fetch. For a caller that
#                              has just fetched the same ref itself. It does
#                              NOT make the check permissive: a remote tip
#                              whose object is missing locally is still
#                              undecidable, never in-sync.
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

# Bring the one branch this compares against into the local object store.
# A failure here is deliberately not fatal on its own -- the objects may
# already be present from an earlier fetch, and the ls-remote below is what
# decides reachability. What a fetch failure must never do is produce
# in-sync, and it cannot: the tip still has to resolve locally further down,
# and if it does not, that path emits undecidable.
if [ "${ENGINE_SYNC_NO_FETCH:-0}" != "1" ]; then
  git fetch --quiet --no-tags "$REMOTE" "refs/heads/${REMOTE_BRANCH}" >/dev/null 2>&1 || true
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
