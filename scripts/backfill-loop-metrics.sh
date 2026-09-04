#!/usr/bin/env bash
# scripts/backfill-loop-metrics.sh — reconstruct past-week loop-metrics rows
# from git log and agent-feed.jsonl.
#
# Run this once to populate the Loop Timeline chart for interactive sessions
# that predate the interactive-metrics-tick.sh integration.
#
# Writes rows with AF_METRICS_ORIGIN=backfill. Idempotent: timestamps that
# already appear in loop-metrics.jsonl are skipped.
#
# Usage:
#   bash scripts/backfill-loop-metrics.sh [--days N] [--dry-run]
#
# Options:
#   --days N     How many past days to reconstruct (default: 7)
#   --dry-run    Print rows instead of writing them
#
# Exit codes:
#   0   completed (zero or more rows written)
#   1   fatal error

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DAYS=7
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --days)    DAYS="$2";    shift 2 ;;
    --dry-run) DRY_RUN=true; shift   ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

METRICS_FILE="${METRICS_FILE:-$REPO_ROOT/.autonomous-team/loop-metrics.jsonl}"
FEED_FILE="${REPO_ROOT}/.autonomous-team/agent-feed.jsonl"

echo "[backfill] Reconstructing $DAYS days of metrics from git log + agent-feed.jsonl"
echo "[backfill] Output: $METRICS_FILE"
[[ "$DRY_RUN" == "true" ]] && echo "[backfill] DRY RUN — no writes"

# ── Collect existing timestamps to avoid duplicates ───────────────────────
# Extract .ts field from every existing row whose origin is backfill or interactive.
EXISTING_TS=()
if [[ -f "$METRICS_FILE" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    ts=$(echo "$line" | python3 -c "
import json,sys
try:
    d=json.loads(sys.stdin.read().strip())
    o=d.get('origin','')
    ts=d.get('timestamp','')
    if ts and o in ('backfill','interactive'):
        print(ts)
except Exception:
    pass
" 2>/dev/null || true)
    [[ -n "$ts" ]] && EXISTING_TS+=("$ts")
  done < "$METRICS_FILE"
fi

# ── Build 5-minute slot boundaries for the past N days ────────────────────
# We reconstruct one row per 5-minute slot. If a slot has no activity we
# still write a zero-counter row so the chart has no gaps.
SLOT_SECONDS=300
START_EPOCH=$(($(date +%s) - DAYS * 86400))
END_EPOCH=$(date +%s)

# Round start down to nearest slot boundary
START_EPOCH=$(( (START_EPOCH / SLOT_SECONDS) * SLOT_SECONDS ))

ROWS_WRITTEN=0
ROWS_SKIPPED=0

SLOT_START=$START_EPOCH
while [[ $SLOT_START -lt $END_EPOCH ]]; do
  SLOT_END=$((SLOT_START + SLOT_SECONDS))
  SLOT_START_ISO=$(date -u -d "@$SLOT_START" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || python3 -c "import datetime; print(datetime.datetime.utcfromtimestamp($SLOT_START).strftime('%Y-%m-%dT%H:%M:%SZ'))")
  SLOT_END_ISO=$(date -u -d "@$SLOT_END" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || python3 -c "import datetime; print(datetime.datetime.utcfromtimestamp($SLOT_END).strftime('%Y-%m-%dT%H:%M:%SZ'))")

  # Skip if this slot's end timestamp already exists in metrics
  ALREADY_EXISTS=false
  for existing in "${EXISTING_TS[@]:-}"; do
    if [[ "$existing" == "$SLOT_END_ISO" ]]; then
      ALREADY_EXISTS=true
      break
    fi
  done
  if [[ "$ALREADY_EXISTS" == "true" ]]; then
    ROWS_SKIPPED=$((ROWS_SKIPPED + 1))
    SLOT_START=$SLOT_END
    continue
  fi

  # Count PRs merged in this slot from git log
  PRS_MERGED=$(git -C "$REPO_ROOT" log \
    --after="$SLOT_START_ISO" \
    --before="$SLOT_END_ISO" \
    --oneline \
    2>/dev/null \
    | grep -cE '\(#[0-9]+\)' || true)
  PRS_MERGED="${PRS_MERGED:-0}"

  # Count agent spawns in this slot from agent-feed.jsonl
  AGENTS_SPAWNED=0
  if [[ -f "$FEED_FILE" ]]; then
    AGENTS_SPAWNED=$(grep -a '"ts"' "$FEED_FILE" 2>/dev/null | python3 - "$SLOT_START_ISO" "$SLOT_END_ISO" <<'PYEOF'
import json, sys

slot_start = sys.argv[1]
slot_end = sys.argv[2]
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
    if slot_start <= ts < slot_end and ev.get("event_type") in ("spawn_attempt", "agent_start"):
        count += 1
print(count)
PYEOF
)
    AGENTS_SPAWNED="${AGENTS_SPAWNED:-0}"
  fi

  # Only write rows where there was actual activity (backfill shouldn't
  # pollute the chart with fake zero rows for periods before the project existed)
  if [[ "$PRS_MERGED" -eq 0 && "$AGENTS_SPAWNED" -eq 0 ]]; then
    SLOT_START=$SLOT_END
    continue
  fi

  APPEND_ARGS=(
    --iter-start-iso   "$SLOT_START_ISO"
    --iter-end-iso     "$SLOT_END_ISO"
    --duration-seconds "$SLOT_SECONDS"
    --agents-spawned   "$AGENTS_SPAWNED"
    --prs-merged       "$PRS_MERGED"
  )
  if [[ "$DRY_RUN" == "true" ]]; then
    APPEND_ARGS+=(--dry-run true)
  fi

  AF_METRICS_ORIGIN=backfill METRICS_FILE="$METRICS_FILE" \
    bash "$SCRIPT_DIR/append-loop-metrics.sh" "${APPEND_ARGS[@]}" 2>/dev/null \
    && ROWS_WRITTEN=$((ROWS_WRITTEN + 1)) \
    || echo "[backfill] Warning: append failed for slot $SLOT_END_ISO" >&2

  SLOT_START=$SLOT_END
done

echo "[backfill] Done. Rows written=$ROWS_WRITTEN skipped=$ROWS_SKIPPED"
