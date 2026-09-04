#!/usr/bin/env bash
# scripts/sweep-hook-events.sh — periodic retention sweep for .autonomous-team/hook-events/
#
# Retention rules (all relative to file mtime):
#   .lock  files at top-level   — delete if older than 24h  (stale; hook died or completed)
#   .json  files at top-level   — delete if older than 48h  AND no matching done/ entry
#                                  (orphan marker: neither active nor completed cleanly)
#   done/*.json                  — delete if older than 7 days
#   blocks-YYYY-MM-DD.jsonl     — gzip if older than 7 days; keep current week uncompressed
#
# Does NOT touch active markers (top-level .json with a matching .lock < 24h old).
# Does NOT touch the active flock mechanism in scripts/lib/hook-event.sh.
#
# Usage:
#   bash scripts/sweep-hook-events.sh [--dry-run] [--hook-dir <path>]
#
# Options:
#   --dry-run      List what would be removed/gzipped without making changes.
#   --hook-dir     Override the hook-events directory path (default: auto-detected).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=""
HOOK_DIR_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY_RUN="1"; shift ;;
    --hook-dir)  HOOK_DIR_OVERRIDE="$2"; shift 2 ;;
    *) echo "[sweep-hook-events] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ── Locate hook-events directory ──────────────────────────────────────────────

if [[ -n "$HOOK_DIR_OVERRIDE" ]]; then
  HOOK_DIR="$HOOK_DIR_OVERRIDE"
else
  HOOK_DIR="$REPO_ROOT/.autonomous-team/hook-events"
fi

if [[ ! -d "$HOOK_DIR" ]]; then
  echo "[sweep-hook-events] hook-events directory not found: $HOOK_DIR — nothing to sweep"
  exit 0
fi

DONE_DIR="$HOOK_DIR/done"
MAX_DELETIONS=2000  # Safety cap: never delete more than this in one run

# ── Counters ──────────────────────────────────────────────────────────────────
deleted_locks=0
deleted_orphans=0
deleted_done=0
gzipped_blocks=0

dry() { [[ "$DRY_RUN" == "1" ]]; }

log_action() {
  local action="$1"
  local target="$2"
  if dry; then
    echo "[sweep-hook-events] DRY-RUN: would $action: $(basename "$target")"
  else
    echo "[sweep-hook-events] $action: $(basename "$target")"
  fi
}

# ── 1. Stale .lock files (top-level, older than 24h) ─────────────────────────
# A lock is stale when the hook either completed (and removed the lock) or died.
# After 24h any remaining lock file is garbage.

echo "[sweep-hook-events] Pass 1: stale .lock files (> 24h)"
while IFS= read -r -d '' lock_file; do
  log_action "delete stale lock" "$lock_file"
  if ! dry; then
    rm -f "$lock_file" 2>/dev/null || true
  fi
  ((deleted_locks++)) || true
done < <(find "$HOOK_DIR" -maxdepth 1 -type f -name "*.lock" -mmin +$((24 * 60)) \
           -print0 2>/dev/null | head -c 999999 || true)

echo "[sweep-hook-events]   stale locks found: $deleted_locks"

# ── 2. Orphan .json markers (top-level, older than 48h, no done/ entry) ───────
# An orphan is a top-level marker file that:
#   - is older than 48h (giving long-running hooks generous time to complete)
#   - has no corresponding entry in done/ (not cleanly finished)
#   - has no corresponding active .lock file younger than 24h (not being held right now)
#
# These arise from SIGKILL / sandbox blocks that bypassed the trap cleanup.

echo "[sweep-hook-events] Pass 2: orphan .json markers (> 48h, not in done/)"
while IFS= read -r -d '' json_file; do
  event_id="$(basename "$json_file" .json)"
  done_entry="$DONE_DIR/${event_id}.json"
  active_lock="$HOOK_DIR/${event_id}.lock"

  # Skip if done/ entry exists (marker was cleanly finished then someone
  # recreated the top-level marker, which shouldn't happen, but be safe).
  if [[ -f "$done_entry" ]]; then
    continue
  fi

  # Skip if an active lock file exists (hook still running or completed < 24h ago)
  # find -mmin returns files OLDER than N minutes; we want to skip files NEWER than 24h.
  if [[ -f "$active_lock" ]]; then
    # Check if the lock is recent (< 24h = 1440 min)
    lock_age_minutes=$(python3 -c "
import os, time
try:
    mtime = os.path.getmtime('$active_lock')
    print(int((time.time() - mtime) / 60))
except:
    print(99999)
" 2>/dev/null || echo "99999")
    if [[ "$lock_age_minutes" -lt 1440 ]]; then
      # Lock is fresh — hook may still be running; skip this marker
      continue
    fi
  fi

  log_action "delete orphan marker" "$json_file"
  if ! dry; then
    rm -f "$json_file" 2>/dev/null || true
    # Also clean up any stale lock for this orphan
    rm -f "$active_lock" 2>/dev/null || true
  fi
  ((deleted_orphans++)) || true
done < <(find "$HOOK_DIR" -maxdepth 1 -type f -name "*.json" -mmin +$((48 * 60)) \
           -print0 2>/dev/null | head -c 999999 || true)

echo "[sweep-hook-events]   orphan markers found: $deleted_orphans"

# ── 3. done/ entries older than 7 days ────────────────────────────────────────

echo "[sweep-hook-events] Pass 3: done/ entries (> 7 days)"
if [[ -d "$DONE_DIR" ]]; then
  done_candidates=()
  while IFS= read -r -d '' f; do
    done_candidates+=("$f")
  done < <(find "$DONE_DIR" -maxdepth 1 -type f -mtime +7 -print0 2>/dev/null || true)

  # Apply safety cap
  cap=$MAX_DELETIONS
  for done_file in "${done_candidates[@]+"${done_candidates[@]}"}"; do
    if [[ $cap -le 0 ]]; then
      echo "[sweep-hook-events]   WARN: hit safety cap ($MAX_DELETIONS); will finish on next run"
      break
    fi
    log_action "delete done entry" "$done_file"
    if ! dry; then
      rm -f "$done_file" 2>/dev/null || true
    fi
    ((deleted_done++)) || true
    ((cap--)) || true
  done
else
  echo "[sweep-hook-events]   done/ directory not found — skipping"
fi

echo "[sweep-hook-events]   done entries found: $deleted_done"

# ── 4. blocks-YYYY-MM-DD.jsonl — gzip if older than 7 days ──────────────────
# Current week's files (mtime <= 7 days) stay uncompressed for easy grepping.
# Files older than 7 days are gzipped in place. Already-gzipped files are skipped.

echo "[sweep-hook-events] Pass 4: gzip old blocks-YYYY-MM-DD.jsonl files (> 7 days)"
while IFS= read -r -d '' blocks_file; do
  log_action "gzip blocks file" "$blocks_file"
  if ! dry; then
    gzip -f "$blocks_file" 2>/dev/null || true
  fi
  ((gzipped_blocks++)) || true
done < <(find "$HOOK_DIR" -maxdepth 1 -type f -name "blocks-*.jsonl" -mtime +7 \
           -print0 2>/dev/null || true)

echo "[sweep-hook-events]   blocks files gzipped: $gzipped_blocks"

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
if dry; then
  echo "[sweep-hook-events] DRY-RUN summary (no changes made):"
else
  echo "[sweep-hook-events] Sweep complete:"
fi
echo "  stale locks deleted:  $deleted_locks"
echo "  orphan markers removed: $deleted_orphans"
echo "  done/ entries removed: $deleted_done"
echo "  blocks files gzipped: $gzipped_blocks"
