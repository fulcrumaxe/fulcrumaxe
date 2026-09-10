#!/usr/bin/env bash
# scripts/lib/worktree-disk-guard.sh — free-disk warning for the worktree spawn path (D#2097).
#
# Replaces the old worktree-cap check in scripts/pre-spawn-check.sh, which compared a
# cumulative, monotonically-growing on-disk directory count (worktree-registry.sh's
# count-disk) against the literal 8. That 8 was never a disk number: it is
# backend/fleet/concurrency.py's DEFAULT_FLEET_CAP (max 4 executors + max 4 other
# agents, bottlenecked on state.db/GH API/preflight -- see
# feedback_concurrency_caps.md), copied onto an unrelated cumulative count. Measured
# 2026-09-07: 43 worktree directories at ~50MB each (~2.1G) against 104G free (88%) on
# a 899G filesystem -- disk was never actually close to a problem, and the check never
# blocked a spawn anyway (D#2059 removed its `exit 1`; nothing replaced it, so the
# team log kept claiming spawns were held back while every one of them went through).
#
# This checks the thing that was actually at risk instead: free bytes on the
# filesystem holding .claude/worktrees. It is a warning only -- it must never fail a
# spawn, and if the probe itself fails it fails open (verdict "unknown", spawn still
# allowed) rather than guessing.
#
# Floor: 10 GB. At the measured ~50MB/worktree that is ~200 worktrees of headroom, and
# roughly 10% of the 104G that was free at measurement time.
#
# Usage:
#   source scripts/lib/worktree-disk-guard.sh
#   verdict=$(worktree_disk_guard_check "$REPO_ROOT/.claude/worktrees")
#   case "$verdict" in
#     warn)    # free space below the floor -- warn, still allow the spawn
#     unknown) # probe failed -- fail open, still allow the spawn
#     ok)      # plenty of room, say nothing
#   esac
#
# Testability: tests/test_worktree_cap_guard.sh redefines
# _worktree_disk_guard_probe_free_kb (an ordinary shell function, redefinable after
# sourcing this file) to stub the free-space probe instead of touching a real
# filesystem.

WORKTREE_DISK_GUARD_FLOOR_GB="${WORKTREE_DISK_GUARD_FLOOR_GB:-10}"

# _worktree_disk_guard_probe_free_kb <dir>
#   Prints available KB on the filesystem holding <dir> on stdout. Prints nothing
#   and returns non-zero if the probe fails. Kept as its own function purely so
#   tests can redefine it to stub disk state.
_worktree_disk_guard_probe_free_kb() {
  local dir="$1"
  # Walk up to the nearest existing ancestor -- df fails outright on a path that
  # does not exist yet (e.g. a fresh checkout before .claude/worktrees is created).
  while [[ ! -d "$dir" && -n "$dir" && "$dir" != "/" && "$dir" != "." ]]; do
    dir="$(dirname "$dir")"
  done
  [[ -d "$dir" ]] || return 1
  df -Pk "$dir" 2>/dev/null | awk 'NR==2 {print $4}'
}

# worktree_disk_guard_check <dir> [floor_gb]
#   Prints one of: ok | warn | unknown
#   Boundary is inclusive on the "ok" side: free space >= the floor is "ok",
#   strictly below the floor is "warn". This function only reports the verdict --
#   it never exits non-zero and never decides what the caller does with it. The
#   caller must never turn "warn" into a block.
worktree_disk_guard_check() {
  local dir="$1"
  local floor_gb="${2:-$WORKTREE_DISK_GUARD_FLOOR_GB}"
  local floor_kb=$((floor_gb * 1024 * 1024))

  local free_kb
  free_kb="$(_worktree_disk_guard_probe_free_kb "$dir" 2>/dev/null)"
  if [[ -z "$free_kb" || ! "$free_kb" =~ ^[0-9]+$ ]]; then
    echo "unknown"
    return 0
  fi

  if [[ "$free_kb" -lt "$floor_kb" ]]; then
    echo "warn"
  else
    echo "ok"
  fi
  return 0
}
