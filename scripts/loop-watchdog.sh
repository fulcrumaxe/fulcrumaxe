#!/usr/bin/env bash
# scripts/loop-watchdog.sh — loop self-restart watchdog
#
# Checks ~/.autonomous-forever-state/loop-metrics.jsonl (or override via
# LOOP_METRICS_FILE env var) every time it runs. If the file is older than
# STALE_MINUTES (default 30) or does not exist, the loop is assumed dead and
# trigger.py is called to restart it.
#
# Cron install (run every minute so it catches a stale loop within ~1 min):
#
#   * * * * * bash /path/to/repo/scripts/loop-watchdog.sh
#
# For 10-minute granularity matching the loop cadence:
#
#   */10 * * * * bash /path/to/repo/scripts/loop-watchdog.sh
#
# Flags:
#   --dry-run   Print the trigger command instead of executing it. Exit 0.
#
# Environment:
#   LOOP_METRICS_FILE          Override default path to loop-metrics.jsonl
#   STALE_MINUTES              Override stale threshold (default: 30)
#   REPO_ROOT                  Override repo root detection
#   LOOP_WATCHDOG_DISABLED     Set to "1" to disable firing (kill switch)
#   WATCHDOG_COOLDOWN_MINUTES  Minimum minutes between trigger fires (default: 15)
#   WATCHDOG_LOCK_FILE         Override default flock path (default: /tmp/loop-watchdog.lock) —
#                              lets tests exercise the concurrency guard without racing a
#                              concurrently-running copy of this script on the real lock (D#2254)
#
# Runaway safeguards (all four required per feedback_no_runaway_loops):
#   1. Lock file  — flock -n prevents two concurrent instances from both firing
#   2. Cooldown   — WATCHDOG_COOLDOWN_MINUTES between successive fires
#   3. Kill switch — LOOP_WATCHDOG_DISABLED=1 stops all firing immediately
#   4. Fire counter — cumulative count in .autonomous-team/loop-watchdog.fire-count
#
# Log: .autonomous-team/loop-watchdog.log (one line per run)
# Exit codes: 0 = OK or dry-run or skipped, 1 = fatal error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# ── Config ────────────────────────────────────────────────────────────────

STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"
METRICS_FILE="${LOOP_METRICS_FILE:-$STATE_DIR/loop-metrics.jsonl}"
LOG_FILE="$REPO_ROOT/.autonomous-team/loop-watchdog.log"
STALE_MINUTES="${STALE_MINUTES:-30}"
COOLDOWN_MINUTES="${WATCHDOG_COOLDOWN_MINUTES:-15}"
LAST_FIRE_FILE="$REPO_ROOT/.autonomous-team/loop-watchdog.last-fire"
FIRE_COUNT_FILE="$REPO_ROOT/.autonomous-team/loop-watchdog.fire-count"
LOCK_FILE="${WATCHDOG_LOCK_FILE:-/tmp/loop-watchdog.lock}"
DRY_RUN=false

# ── Arg parsing ───────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "loop-watchdog: unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ── Helpers ───────────────────────────────────────────────────────────────

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

log() {
  local ts
  ts=$(now_iso)
  local line="[$ts] $*"
  echo "$line"
  mkdir -p "$(dirname "$LOG_FILE")"
  printf '%s\n' "$line" >> "$LOG_FILE"
}

# ── Safeguard 3: Kill switch ──────────────────────────────────────────────

if [[ "${LOOP_WATCHDOG_DISABLED:-0}" == "1" ]]; then
  log "DISABLED — LOOP_WATCHDOG_DISABLED=1 is set — exiting without firing"
  exit 0
fi

# ── Safeguard 1: Lock file (concurrency guard) ────────────────────────────
# Acquire a non-blocking lock. If another instance holds it, exit cleanly.
# The lock is released automatically when this process exits (fd closed).

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "SKIPPED — another watchdog instance is running (lock: $LOCK_FILE)"
  exit 0
fi

# ── Staleness check ───────────────────────────────────────────────────────

STALE=false
REASON=""
AGE_MINUTES=0

if [[ ! -f "$METRICS_FILE" ]]; then
  STALE=true
  REASON="loop-metrics.jsonl not found at $METRICS_FILE"
else
  # Get file mtime in seconds since epoch; support both GNU and BSD stat
  FILE_MTIME=$(stat -c %Y "$METRICS_FILE" 2>/dev/null \
    || stat -f %m "$METRICS_FILE" 2>/dev/null \
    || python3 -c "import os; print(int(os.path.getmtime('$METRICS_FILE')))" 2>/dev/null \
    || echo 0)
  NOW_EPOCH=$(date +%s)
  AGE_SECONDS=$(( NOW_EPOCH - FILE_MTIME ))
  AGE_MINUTES=$(( AGE_SECONDS / 60 ))

  if [[ "$AGE_MINUTES" -ge "$STALE_MINUTES" ]]; then
    STALE=true
    REASON="loop-metrics.jsonl is ${AGE_MINUTES}m old (threshold: ${STALE_MINUTES}m)"
  fi
fi

# ── Act ───────────────────────────────────────────────────────────────────

if [[ "$STALE" == "true" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    log "STALE — $REASON — dry-run: would run: python3 $REPO_ROOT/backend/trigger.py 'run /loop iteration'"
    echo "DRY-RUN trigger: python3 $REPO_ROOT/backend/trigger.py 'run /loop iteration'"
  else
    # ── Safeguard 2: Cooldown ─────────────────────────────────────────────
    NOW_EPOCH=$(date +%s)
    if [[ -f "$LAST_FIRE_FILE" ]]; then
      LAST_FIRE=$(cat "$LAST_FIRE_FILE" 2>/dev/null || echo 0)
      SINCE_LAST=$(( NOW_EPOCH - LAST_FIRE ))
      SINCE_LAST_MINUTES=$(( SINCE_LAST / 60 ))
      if [[ "$SINCE_LAST_MINUTES" -lt "$COOLDOWN_MINUTES" ]]; then
        log "COOLDOWN — last fire was ${SINCE_LAST_MINUTES}m ago (cooldown: ${COOLDOWN_MINUTES}m) — skipping"
        exit 0
      fi
    fi

    # ── Safeguard 4: Fire counter ─────────────────────────────────────────
    mkdir -p "$(dirname "$FIRE_COUNT_FILE")"
    FIRE_COUNT=0
    if [[ -f "$FIRE_COUNT_FILE" ]]; then
      FIRE_COUNT=$(cat "$FIRE_COUNT_FILE" 2>/dev/null || echo 0)
    fi
    FIRE_COUNT=$(( FIRE_COUNT + 1 ))
    printf '%d\n' "$FIRE_COUNT" > "$FIRE_COUNT_FILE"

    # Record last-fire timestamp
    printf '%d\n' "$NOW_EPOCH" > "$LAST_FIRE_FILE"

    log "STALE — $REASON — triggering loop restart fire_count=$FIRE_COUNT"
    python3 "$REPO_ROOT/backend/trigger.py" "run /loop iteration" 2>&1 | while IFS= read -r line; do
      log "trigger: $line"
    done || log "ERROR — trigger.py exited non-zero"
  fi
else
  log "OK — loop-metrics.jsonl is fresh (${AGE_MINUTES}m old, threshold: ${STALE_MINUTES}m)"
fi
