#!/usr/bin/env bash
# scripts/interactive-metrics-tick.sh — emit one loop-metrics row from an interactive session.
#
# Called from two places:
#   1. post-merge-hook.sh  — piggyback on every merge (belt)
#   2. Optional cron job   — every 5 minutes when a Claude Code session is running (suspenders)
#
# Design:
#   - Counts PRs merged in last 5 minutes via git log (fast, no API call)
#   - Counts agents spawned in last 5 minutes via tail of agent-feed.jsonl (fast)
#   - Writes one row via append-loop-metrics.sh with AF_METRICS_ORIGIN=interactive
#   - Always writes a row, even if all counters are zero (prevents timeline gaps)
#   - Runs in <2s — no heavy queries, no GitHub API calls
#   - Idempotent: calling twice in the same minute writes duplicate rows but
#     those are fine for the chart (it aggregates by time bucket)
#
# Usage:
#   bash scripts/interactive-metrics-tick.sh [--window-seconds N]
#
# Options:
#   --window-seconds N   Look-back window for activity (default: 300 = 5 min)
#   --dry-run            Print the row instead of writing it
#
# Exit codes:
#   0   row appended (or dry-run printed)
#   1   fatal error

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WINDOW_SECONDS=300
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --window-seconds) WINDOW_SECONDS="$2"; shift 2 ;;
    --dry-run)        DRY_RUN=true;        shift   ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# ── 1. Count PRs merged in the look-back window ────────────────────────────
# git log is fast (local). We grep for the squash-merge "(#NNN)" pattern that
# GitHub adds to all squash merges on this repo.
# Use --since="N seconds ago" to avoid parsing dates ourselves.

WINDOW_LABEL="${WINDOW_SECONDS} seconds ago"
PRS_MERGED=$(git -C "$REPO_ROOT" log \
  --since="$WINDOW_LABEL" \
  --oneline \
  2>/dev/null \
  | grep -cE '\(#[0-9]+\)' || true)
PRS_MERGED="${PRS_MERGED:-0}"

# ── 2. Count agent spawns in the look-back window ─────────────────────────
# agent-feed.jsonl is append-only JSONL; we tail the last 500 lines (fast)
# and count spawn_attempt events within the time window.
AGENTS_SPAWNED=0
FEED_FILE="${REPO_ROOT}/.autonomous-team/agent-feed.jsonl"
if [[ -f "$FEED_FILE" ]]; then
  CUTOFF_TS=$(date -u -d "@$(($(date +%s) - WINDOW_SECONDS))" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || python3 -c "
import datetime
import sys
secs = int(sys.argv[1])
cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=secs)
print(cutoff.strftime('%Y-%m-%dT%H:%M:%SZ'))
" "$WINDOW_SECONDS")

  AGENTS_SPAWNED=$(tail -500 "$FEED_FILE" | python3 - "$CUTOFF_TS" <<'PYEOF'
import json, sys

cutoff = sys.argv[1]
count = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    ts = ev.get("ts", "")
    if ts >= cutoff and ev.get("event_type") in ("spawn_attempt", "agent_start"):
        count += 1
print(count)
PYEOF
)
  AGENTS_SPAWNED="${AGENTS_SPAWNED:-0}"
fi

# ── 3. Compute scan_to_spawn_ratio over last 24h ──────────────────────────
# Definition: (iterations_with_scan_no_spawn) / (total_iterations)
# When actionable_count is 0, ratio is 1.0 (no-op iteration is healthy).
SCAN_TO_SPAWN_RATIO=$(python3 - <<'PYEOF' 2>/dev/null
import json, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import os

metrics_file = Path(os.environ.get('REPO_ROOT', '.')) / '.autonomous-team' / 'loop-metrics.jsonl'
if not metrics_file.exists():
    print('null')
    sys.exit(0)

cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
total = 0
scan_no_spawn = 0
try:
    with metrics_file.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts_str = row.get('timestamp') or row.get('ts')
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            except Exception:
                continue
            if ts < cutoff:
                continue
            total += 1
            spawned = row.get('agents_spawned', 0) or 0
            if spawned == 0:
                scan_no_spawn += 1
except Exception:
    print('null')
    sys.exit(0)

if total == 0:
    print('null')
else:
    ratio = round(scan_no_spawn / total, 4)
    print(ratio)
PYEOF
)
SCAN_TO_SPAWN_RATIO="${SCAN_TO_SPAWN_RATIO:-null}"

# Emit to stats_writer (best-effort)
if [[ "$SCAN_TO_SPAWN_RATIO" != "null" ]] && [[ "$DRY_RUN" != "true" ]]; then
  REPO_ROOT="$REPO_ROOT" python3 -c "
import sys, os; sys.path.insert(0, os.environ.get('REPO_ROOT', '.'))
from backend.stats_writer import record
import os
record('scan_to_spawn_ratio', float('$SCAN_TO_SPAWN_RATIO'), 'ratio',
       tags={'window_hours': '24'}, source='interactive-metrics-tick')
" 2>/dev/null || true
fi

# ── 4. Emit metrics row ────────────────────────────────────────────────────
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START_ISO=$(date -u -d "@$(($(date +%s) - WINDOW_SECONDS))" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
  || python3 -c "
import datetime, sys
secs = int(sys.argv[1])
start = datetime.datetime.utcnow() - datetime.timedelta(seconds=secs)
print(start.strftime('%Y-%m-%dT%H:%M:%SZ'))
" "$WINDOW_SECONDS")

APPEND_ARGS=(
  --iter-start-iso   "$START_ISO"
  --iter-end-iso     "$NOW_ISO"
  --duration-seconds "$WINDOW_SECONDS"
  --agents-spawned   "$AGENTS_SPAWNED"
  --prs-merged       "$PRS_MERGED"
)

if [[ "$SCAN_TO_SPAWN_RATIO" != "null" ]]; then
  APPEND_ARGS+=(--scan-to-spawn-ratio "$SCAN_TO_SPAWN_RATIO")
fi

if [[ "$DRY_RUN" == "true" ]]; then
  APPEND_ARGS+=(--dry-run true)
fi

export AF_METRICS_ORIGIN=interactive

bash "$SCRIPT_DIR/append-loop-metrics.sh" "${APPEND_ARGS[@]}"
