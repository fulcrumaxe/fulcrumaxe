#!/usr/bin/env bash
# reap-worktrees.sh — worktree lifecycle reaper, invoked from /loop step 5.0.
#
# Policy (in order):
#   1. Auto-cleanup merged worktrees still on disk
#   2. Detect orphans (stale heartbeat + dead parent PID)
#   3. Archive orphan diffs to archive/orphan-diffs/<id>-<date>.patch
#   4. Discard orphan worktrees (git worktree remove --force)
#   5. Back-compat: on-disk worktrees with no registry entry — archive + remove
#   6. Enumeration + skip-reason report; also removes a git-tracked worktree
#      once it clears every safety condition (D#2001 PR2) — see
#      --enable-git-tracked-removal below.
#
# Usage:
#   bash scripts/reap-worktrees.sh [--ttl-min N] [--dry-run] [--clean-generated-wiki] [--enable-git-tracked-removal]
#
# --enable-git-tracked-removal is OFF by default. This script is invoked
# live (no --dry-run) after every agent completion
# (post-agent-hook.sh:533) — shipping git-tracked removal enabled there by
# default would silently start deleting real worktrees on the very next
# agent completion. Enable it only for a deliberate, human-run invocation.
#
# D#2149: --dry-run previews the invocation you actually typed, not some
# other invocation -- it no longer reports removals that a real run under
# the same flags would never make.
#
#   invocation                              | classification        | reaped
#   -----------------------------------------+------------------------+-------
#   real, no opt-in                         | skipped-git-tracked    | no
#   --dry-run, no opt-in                    | skipped-git-tracked*   | no
#   real, --enable-git-tracked-removal      | removed, cap-limited   | yes
#   --dry-run --enable-git-tracked-removal  | preview, cap-limited   | yes
#
#   * candidate visibility survives as an informational
#     "candidate-git-tracked" line to stderr; it does not touch `reaped`.
#
# A dry-run is not sufficient Gate 2 evidence for a change to this path --
# the rm -rf path guards in worktree-registry.sh live in the real,
# dry_run==false arm and are unreachable by any dry-run. See D#2149
# acceptance item 3 for the real-run differential that substitutes for it.
#
# Output (single line printed to stdout + team-log):
#   worktrees: N active, N reaped, N patch archived, N merged-cleanup
#
# TTL default: 60 minutes (override with --ttl-min or WORKTREE_TTL_MIN env var)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source registry helper
# shellcheck source=scripts/lib/worktree-registry.sh
source "$SCRIPT_DIR/lib/worktree-registry.sh"

TTL_MIN="${WORKTREE_TTL_MIN:-60}"
DRY_RUN_FLAG=""
CLEAN_GENERATED_WIKI_FLAG=""
ENABLE_GIT_TRACKED_REMOVAL_FLAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ttl-min)                    TTL_MIN="$2"; shift 2 ;;
    --dry-run)                    DRY_RUN_FLAG="--dry-run"; shift ;;
    --clean-generated-wiki)       CLEAN_GENERATED_WIKI_FLAG="--clean-generated-wiki"; shift ;;
    --enable-git-tracked-removal) ENABLE_GIT_TRACKED_REMOVAL_FLAG="--enable-git-tracked-removal"; shift ;;
    --quiet)                shift ;;  # accepted for back-compat with scan-orphan-worktrees.sh
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 [--ttl-min N] [--dry-run] [--clean-generated-wiki]" >&2
      exit 1
      ;;
  esac
done

# Force dry-run under pytest — test files execute this script directly and are
# a sandbox-bypass channel the PreToolUse hook cannot see (D#1864 security
# review). Real removals from under pytest require an explicit override.
if [[ -n "${PYTEST_CURRENT_TEST:-}" && "${WORKTREE_REAP_ALLOW_UNDER_PYTEST:-}" != "1" ]]; then
  DRY_RUN_FLAG="--dry-run"
  echo "reap-worktrees: PYTEST_CURRENT_TEST set — forcing --dry-run (set WORKTREE_REAP_ALLOW_UNDER_PYTEST=1 to override)" >&2
fi

# Ensure the registry file exists (first run)
mkdir -p "${REPO_ROOT}/.autonomous-team"
mkdir -p "${REPO_ROOT}/archive/orphan-diffs"
if [[ ! -f "${REPO_ROOT}/.autonomous-team/worktrees.json" ]]; then
  printf '[\n]\n' > "${REPO_ROOT}/.autonomous-team/worktrees.json"
fi

# ---------------------------------------------------------------------------
# Pre-reap scan: warn if any worktree has real (non-symlink) state files at
# manifest paths. These indicate state forking — writes from that worktree
# never reached the shared external state dir. Warn before the worktree is
# torn down so Team Lead can see what data would have been lost.
# Non-fatal — scan errors must not break the reaper.
# ---------------------------------------------------------------------------
_MANIFEST="${REPO_ROOT}/.autonomous-team/state-symlinks.json"
if [[ -f "$_MANIFEST" ]] && command -v jq &>/dev/null; then
  # Read the list of managed in-repo paths from the manifest
  _MANIFEST_PATHS=$(jq -r '.entries[].in_repo' "$_MANIFEST" 2>/dev/null || true)

  # Get all active (on-disk) worktree paths from the registry
  _ACTIVE_WTS=$(python3 -c "
import json, sys, os
try:
    data = json.load(open('${REPO_ROOT}/.autonomous-team/worktrees.json'))
except Exception:
    sys.exit(0)
for e in data:
    if e.get('status') in ('active', 'committed', 'pushed', 'merged'):
        p = e.get('path', '')
        if not p:
            continue
        abs_p = p if os.path.isabs(p) else os.path.join('${REPO_ROOT}', p.lstrip('./'))
        if os.path.isdir(abs_p):
            print(abs_p)
" 2>/dev/null || true)

  while IFS= read -r _WT_ABS; do
    [[ -z "$_WT_ABS" ]] && continue
    while IFS= read -r _MPATH; do
      [[ -z "$_MPATH" ]] && continue
      _FULL="${_WT_ABS}/.autonomous-team/${_MPATH}"
      # Warn if the path exists and is NOT a symlink
      if [[ -e "$_FULL" && ! -L "$_FULL" ]]; then
        _SZ=$(du -sh "$_FULL" 2>/dev/null | cut -f1 || echo "?")
        _WARN_MSG="[$(date +%H:%M)] reap-worktrees: WARN — non-symlink state file in worktree (forked state): ${_FULL} (${_SZ}) — data will be lost on reap"
        echo "$_WARN_MSG" >&2
        bash "$SCRIPT_DIR/rotate-team-log.sh" comment "$_WARN_MSG" 2>/dev/null || true
      fi
    done <<< "$_MANIFEST_PATHS"
  done <<< "$_ACTIVE_WTS"
fi

# Record start time for rate calculation
_REAP_START_TS=$(date +%s)

# Run the reaper and capture the summary line
SUMMARY=$(worktree_registry reap --ttl-min "$TTL_MIN" ${DRY_RUN_FLAG:-} ${CLEAN_GENERATED_WIKI_FLAG:-} ${ENABLE_GIT_TRACKED_REMOVAL_FLAG:-} 2>&1)
echo "$SUMMARY"

# Log summary to team-log
SUMMARY_LINE=$(echo "$SUMMARY" | grep "^worktrees:" | tail -1 || true)
if [[ -n "$SUMMARY_LINE" ]]; then
  bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
    "[$(date +%H:%M)] reap-worktrees: $SUMMARY_LINE" \
    2>/dev/null || true
fi

# Emit orphan_worktree_rate metric — orphans_reaped / elapsed_hours (non-fatal).
# Emitted in dry-run too (tagged dry_run=true) — this is the reaper's only
# integration coverage from an unmocked pytest run, and forcing --dry-run
# under pytest (above) would otherwise silently zero out the assertion.
if [[ -n "$SUMMARY_LINE" ]]; then
  _REAPED=$(echo "$SUMMARY_LINE" | grep -oE '[0-9]+ reaped' | grep -oE '^[0-9]+' || echo "0")
  _REAPED="${_REAPED:-0}"
  _REAP_END_TS=$(date +%s)
  _ELAPSED_S=$(( _REAP_END_TS - _REAP_START_TS ))
  if [[ "$_ELAPSED_S" -lt 1 ]]; then _ELAPSED_S=1; fi
  _DRY_RUN_TAG="false"
  [[ "${DRY_RUN_FLAG:-}" == "--dry-run" ]] && _DRY_RUN_TAG="true"
  # A wedged self-exclusion guard (unresolvable toplevel — non-git CWD,
  # missing python3, pruned worktree metadata) refuses every removal for the
  # whole pass and otherwise looks identical to a healthy idle reaper in this
  # metric. Surface it as its own tag so it doesn't hide behind reaped=0.
  _SELF_ROOT_TAG="resolved"
  echo "$SUMMARY_LINE" | grep -q "self-exclusion unresolved" && _SELF_ROOT_TAG="unresolved"
  python3 -c "
import sys
sys.path.insert(0, '${REPO_ROOT}')
try:
    from backend.stats_writer import record
    reaped = int('${_REAPED}')
    elapsed_s = int('${_ELAPSED_S}')
    elapsed_h = elapsed_s / 3600.0
    # rate = orphans per hour; avoid div-by-zero with a 1-second floor
    rate = reaped / max(elapsed_h, 1 / 3600.0)
    record(
        'orphan_worktree_rate',
        round(rate, 6),
        'count',
        tags={'reaped': str(reaped), 'elapsed_s': str(elapsed_s), 'dry_run': '${_DRY_RUN_TAG}', 'self_root': '${_SELF_ROOT_TAG}'},
        source='reap-worktrees',
    )
except Exception as e:
    print(f'[reap-worktrees] orphan_worktree_rate emit failed (non-fatal): {e}', file=sys.stderr)
" 2>/dev/null || true
fi

# Orphan-diff triage nudge — post team-log line when untriaged pile exceeds threshold.
# Non-fatal: errors must not break the reaper.
# shellcheck source=scripts/lib/orphan-triage.sh
source "$SCRIPT_DIR/lib/orphan-triage.sh" 2>/dev/null || true
_ot_nudge_if_over_threshold 2>/dev/null || true
