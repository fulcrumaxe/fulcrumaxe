#!/usr/bin/env bash
# scripts/append-loop-metrics.sh — append one loop-metrics row to loop-metrics.jsonl.
#
# Used by:
#   - scripts/team-lead-iteration.sh (step 7.5, cron/loop writer)
#   - The interactive /loop skill (end of step 7.5)
#
# Both callers pass counters directly. When counters are omitted this script
# computes agents_spawned and prs_merged via backend/loop_metrics_counters.py.
#
# Usage:
#   bash scripts/append-loop-metrics.sh \
#     --iter-start-iso       2026-05-11T12:00:00Z \
#     --iter-end-iso         2026-05-11T12:05:30Z \
#     --duration-seconds     330 \
#     --agents-spawned       3 \
#     --prs-merged           1 \
#     --discussions-scanned  5 \
#     --prs-scanned          2 \
#     --event-count          42 \
#     --queue-depth          0 \
#     --discussion-count     5 \
#     --pr-count             2 \
#     --needs-review         1 \
#     --needs-merge          1 \
#     --needs-fix            0 \
#     [--allow-test-write]   \
#     [--dry-run true]
#
# Origin field:
#   Set AF_METRICS_ORIGIN=cron|interactive|test before calling.
#   Default: "cron".
#   When AF_MCP_TEST_ORIGIN=1 the script refuses to write unless
#   --allow-test-write is also passed.
#
# All flags are optional. Defaults: 0 for counters, now() for timestamps,
# computed duration from start→end, auto-compute agents_spawned/prs_merged
# when not supplied.
#
# Exit codes:
#   0  — row appended (or printed to stdout on --dry-run), or silently skipped (test-origin guard)
#   1  — fatal error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Allow callers (and tests) to override the output file via environment variable.
METRICS_FILE="${METRICS_FILE:-$REPO_ROOT/.autonomous-team/loop-metrics.jsonl}"

# ── Defaults ──────────────────────────────────────────────────────────────

NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

ITER_START_ISO=""
ITER_END_ISO="$NOW_ISO"
DURATION_SECONDS=""
AGENTS_SPAWNED=""    # empty → compute via loop_metrics_counters.py
PRS_MERGED=""        # empty → compute via loop_metrics_counters.py
DISCUSSIONS_SCANNED=""  # empty → compute via loop_metrics_counters.py
PRS_SCANNED=""          # empty → compute via loop_metrics_counters.py
EVENT_COUNT=0
QUEUE_DEPTH=0
DISCUSSION_COUNT=0
PR_COUNT=0
NEEDS_REVIEW=0
NEEDS_MERGE=0
NEEDS_FIX=0
SCAN_TO_SPAWN_RATIO=""   # empty → not supplied; omitted from row
DRY_RUN=false
ALLOW_TEST_WRITE=0

# ── Arg parser ────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iter-start-iso)       ITER_START_ISO="$2";       shift 2 ;;
    --iter-end-iso)         ITER_END_ISO="$2";         shift 2 ;;
    --duration-seconds)     DURATION_SECONDS="$2";     shift 2 ;;
    --agents-spawned)       AGENTS_SPAWNED="$2";       shift 2 ;;
    --prs-merged)           PRS_MERGED="$2";           shift 2 ;;
    --discussions-scanned)  DISCUSSIONS_SCANNED="$2";  shift 2 ;;
    --prs-scanned)          PRS_SCANNED="$2";          shift 2 ;;
    --event-count)          EVENT_COUNT="$2";          shift 2 ;;
    --queue-depth)          QUEUE_DEPTH="$2";          shift 2 ;;
    --discussion-count)     DISCUSSION_COUNT="$2";     shift 2 ;;
    --pr-count)             PR_COUNT="$2";             shift 2 ;;
    --needs-review)         NEEDS_REVIEW="$2";         shift 2 ;;
    --needs-merge)          NEEDS_MERGE="$2";          shift 2 ;;
    --needs-fix)            NEEDS_FIX="$2";            shift 2 ;;
    --scan-to-spawn-ratio)  SCAN_TO_SPAWN_RATIO="$2"; shift 2 ;;
    --dry-run)              DRY_RUN="$2";              shift 2 ;;
    --allow-test-write)     ALLOW_TEST_WRITE=1;        shift ;;
    *) echo "append-loop-metrics: unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ── Test-origin guard ─────────────────────────────────────────────────────
# When running under Puppeteer / E2E tests, refuse to pollute production metrics
# unless the caller explicitly opts in with --allow-test-write.

if [[ "${AF_MCP_TEST_ORIGIN:-}" == "1" ]] && [[ "$ALLOW_TEST_WRITE" != "1" ]]; then
  echo "append-loop-metrics: refusing test-origin write (AF_MCP_TEST_ORIGIN=1). Pass --allow-test-write to override." >&2
  exit 0
fi

# Defaults: if start not given, use end
if [[ -z "$ITER_START_ISO" ]]; then
  ITER_START_ISO="$ITER_END_ISO"
fi

# Duration: compute from start/end if not supplied
if [[ -z "$DURATION_SECONDS" ]]; then
  START_EPOCH=$(date -u -d "$ITER_START_ISO" +%s 2>/dev/null \
    || python3 -c "from datetime import datetime; print(int(datetime.fromisoformat('${ITER_START_ISO}'.replace('Z','+00:00')).timestamp()))" 2>/dev/null \
    || echo 0)
  END_EPOCH=$(date -u -d "$ITER_END_ISO" +%s 2>/dev/null \
    || python3 -c "from datetime import datetime; print(int(datetime.fromisoformat('${ITER_END_ISO}'.replace('Z','+00:00')).timestamp()))" 2>/dev/null \
    || echo 0)
  DURATION_SECONDS=$(( END_EPOCH - START_EPOCH ))
  if (( DURATION_SECONDS < 0 )); then DURATION_SECONDS=0; fi
fi

# ── Producer-side sanity check ────────────────────────────────────────────
# A loop iteration longer than 24 hours (86400s) is definitionally bad data.
# Epoch-timestamp values (~1.7 billion) indicate the producer wrote T_END
# instead of (T_END - T_START). Refuse to write rather than corrupt the dataset.
_MAX_SANE_DURATION=86400
if (( DURATION_SECONDS > _MAX_SANE_DURATION )); then
  echo "append-loop-metrics: ERROR — duration_s=${DURATION_SECONDS} exceeds 86400s (looks like an epoch, not a delta). Row rejected." >&2
  bash "${SCRIPT_DIR}/rotate-team-log.sh" comment \
    "[$(date +%H:%M)] append-loop-metrics: WARN — corrupt duration_s=${DURATION_SECONDS} rejected (epoch written instead of delta)" \
    2>/dev/null || true
  exit 2
fi

# ── Compute counters (if not supplied) ───────────────────────────────────

if [[ -z "$AGENTS_SPAWNED" ]] || [[ -z "$PRS_MERGED" ]] || \
   [[ -z "$DISCUSSIONS_SCANNED" ]] || [[ -z "$PRS_SCANNED" ]]; then
  COUNTERS=$(cd "$REPO_ROOT" && python3 -c \
    "from backend.loop_metrics_counters import compute_counters; import json; \
     print(json.dumps(compute_counters('${ITER_START_ISO}', '${ITER_END_ISO}')))" \
    2>/dev/null || echo '{"agents_spawned":0,"prs_merged":0,"discussions_scanned":0,"prs_scanned":0}')
  [[ -z "$AGENTS_SPAWNED" ]] && \
    AGENTS_SPAWNED=$(echo "$COUNTERS" | jq '.agents_spawned // 0' 2>/dev/null || echo 0)
  [[ -z "$PRS_MERGED" ]] && \
    PRS_MERGED=$(echo "$COUNTERS" | jq '.prs_merged // 0' 2>/dev/null || echo 0)
  [[ -z "$DISCUSSIONS_SCANNED" ]] && \
    DISCUSSIONS_SCANNED=$(echo "$COUNTERS" | jq '.discussions_scanned // 0' 2>/dev/null || echo 0)
  [[ -z "$PRS_SCANNED" ]] && \
    PRS_SCANNED=$(echo "$COUNTERS" | jq '.prs_scanned // 0' 2>/dev/null || echo 0)
fi

# ── Best-effort subsystem status reads ───────────────────────────────────

# Budget: summarize to {spent, remaining, ceiling} only — drop agents[] blob (~30KB per row)
BUDGET_FULL=$(cd "$REPO_ROOT" && python3 backend/budget.py status 2>/dev/null || echo '{}')
echo "$BUDGET_FULL" | jq empty 2>/dev/null || BUDGET_FULL='{}'
BUDGET_STATUS=$(echo "$BUDGET_FULL" | jq '{spent: (.spent // 0), remaining: (.remaining // 0), ceiling: (.ceiling // 0)}' 2>/dev/null || echo '{"spent":0,"remaining":0,"ceiling":0}')

COST_STATUS=$(cd "$REPO_ROOT" && python3 backend/cost_tracker.py summary 2>/dev/null \
  || echo '{}')
QUALITY_STATUS=$(cd "$REPO_ROOT" && python3 backend/quality_scorer.py stats 2>/dev/null \
  || echo '{}')

# Validate that status outputs are valid JSON; reset to {} on failure
echo "$COST_STATUS"    | jq empty 2>/dev/null || COST_STATUS='{}'
echo "$QUALITY_STATUS" | jq empty 2>/dev/null || QUALITY_STATUS='{}'

# ── Origin field ──────────────────────────────────────────────────────────
# Default "cron"; callers set AF_METRICS_ORIGIN=interactive or AF_METRICS_ORIGIN=test
ORIGIN="${AF_METRICS_ORIGIN:-cron}"

# ── Collect Team Lead token usage for this iteration ─────────────────────
# Read ITER_TS_PREV from the last loop-metrics.jsonl row, then call
# subscription_usage.team_lead_usage(ITER_TS_PREV, ITER_TS_NOW).
# On any failure: write zeros and emit a single warning (never block /loop).

TL_INPUT=0
TL_OUTPUT=0
TL_CACHE_READ=0
TL_CACHE_WRITE=0

# Current iteration end timestamp as Unix float
ITER_TS_NOW=$(date +%s)

# Previous iteration timestamp: last row's timestamp field parsed to Unix epoch
ITER_TS_PREV=""
if [[ -f "$METRICS_FILE" ]]; then
  LAST_ROW=$(tail -1 "$METRICS_FILE" 2>/dev/null || true)
  if [[ -n "$LAST_ROW" ]]; then
    LAST_TS=$(echo "$LAST_ROW" | jq -r '.timestamp // empty' 2>/dev/null || true)
    if [[ -n "$LAST_TS" ]]; then
      ITER_TS_PREV=$(python3 -c \
        "from datetime import datetime, timezone; \
         t='${LAST_TS}'.replace('Z','+00:00'); \
         print(datetime.fromisoformat(t).timestamp())" \
        2>/dev/null || true)
    fi
  fi
fi

# If no previous row, default to (now - 600s) so the first run captures recent activity
if [[ -z "$ITER_TS_PREV" ]]; then
  ITER_TS_PREV=$(( ITER_TS_NOW - 600 ))
fi

TL_USAGE=$(cd "$REPO_ROOT" && python3 -c \
  "import json, sys; \
   from backend.subscription_usage import team_lead_usage; \
   result = team_lead_usage(since_ts=float('${ITER_TS_PREV}'), until_ts=float('${ITER_TS_NOW}')); \
   print(json.dumps(result))" \
  2>/dev/null || echo '')

if [[ -z "$TL_USAGE" ]]; then
  bash "${SCRIPT_DIR}/rotate-team-log.sh" comment \
    "[$(date +%H:%M)] append-loop-metrics: WARNING — team_lead_usage() failed; writing zeros for this iteration" \
    2>/dev/null || true
else
  echo "$TL_USAGE" | jq empty 2>/dev/null && {
    TL_INPUT=$(echo "$TL_USAGE"   | jq '.input   // 0' 2>/dev/null || echo 0)
    TL_OUTPUT=$(echo "$TL_USAGE"  | jq '.output  // 0' 2>/dev/null || echo 0)
    TL_CACHE_READ=$(echo "$TL_USAGE"  | jq '.cache_read  // 0' 2>/dev/null || echo 0)
    TL_CACHE_WRITE=$(echo "$TL_USAGE" | jq '.cache_write // 0' 2>/dev/null || echo 0)
  } || {
    bash "${SCRIPT_DIR}/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] append-loop-metrics: WARNING — team_lead_usage() returned invalid JSON; writing zeros" \
      2>/dev/null || true
  }
fi

# ── Build JSON row ────────────────────────────────────────────────────────

METRICS_ROW=$(jq -nc \
  --arg     timestamp         "$ITER_END_ISO" \
  --arg     origin            "$ORIGIN" \
  --argjson dur               "$DURATION_SECONDS" \
  --argjson open_prs          "$PR_COUNT" \
  --argjson needs_review      "$NEEDS_REVIEW" \
  --argjson needs_merge       "$NEEDS_MERGE" \
  --argjson needs_fix         "$NEEDS_FIX" \
  --argjson event_count       "$EVENT_COUNT" \
  --argjson disc_count        "$DISCUSSION_COUNT" \
  --argjson queue_depth       "$QUEUE_DEPTH" \
  --argjson agents_sp         "$AGENTS_SPAWNED" \
  --argjson prs_merged        "$PRS_MERGED" \
  --argjson disc_scanned      "$DISCUSSIONS_SCANNED" \
  --argjson prs_scanned       "$PRS_SCANNED" \
  --argjson budget            "$BUDGET_STATUS" \
  --argjson cost              "$COST_STATUS" \
  --argjson quality           "$QUALITY_STATUS" \
  --argjson tl_input          "$TL_INPUT" \
  --argjson tl_output         "$TL_OUTPUT" \
  --argjson tl_cache_read     "$TL_CACHE_READ" \
  --argjson tl_cache_write    "$TL_CACHE_WRITE" \
  '{
    timestamp:                  $timestamp,
    origin:                     $origin,
    duration_s:                 $dur,
    open_prs:                   $open_prs,
    needs_review:               $needs_review,
    needs_merge:                $needs_merge,
    needs_fix:                  $needs_fix,
    event_count:                $event_count,
    discussion_count:           $disc_count,
    queue_depth:                $queue_depth,
    agents_spawned:             $agents_sp,
    prs_merged:                 $prs_merged,
    discussions_scanned:        $disc_scanned,
    prs_scanned:                $prs_scanned,
    budget:                     $budget,
    cost:                       $cost,
    quality:                    $quality,
    team_lead_input_tokens:     $tl_input,
    team_lead_output_tokens:    $tl_output,
    team_lead_cache_read:       $tl_cache_read,
    team_lead_cache_write:      $tl_cache_write
  }' 2>/dev/null) || {
  # Fallback: plain printf if jq fails
  METRICS_ROW=$(printf \
    '{"timestamp":"%s","origin":"%s","duration_s":%s,"open_prs":%s,"needs_review":%s,"needs_merge":%s,"needs_fix":%s,"event_count":%s,"discussion_count":%s,"queue_depth":%s,"agents_spawned":%s,"prs_merged":%s,"discussions_scanned":%s,"prs_scanned":%s,"budget":{},"cost":{},"quality":{},"team_lead_input_tokens":0,"team_lead_output_tokens":0,"team_lead_cache_read":0,"team_lead_cache_write":0}' \
    "$ITER_END_ISO" "$ORIGIN" "$DURATION_SECONDS" "$PR_COUNT" \
    "$NEEDS_REVIEW" "$NEEDS_MERGE" "$NEEDS_FIX" \
    "$EVENT_COUNT" "$DISCUSSION_COUNT" "$QUEUE_DEPTH" \
    "$AGENTS_SPAWNED" "$PRS_MERGED" "$DISCUSSIONS_SCANNED" "$PRS_SCANNED")
}

# Inject scan_to_spawn_ratio if supplied (non-empty)
if [[ -n "$SCAN_TO_SPAWN_RATIO" ]]; then
  METRICS_ROW=$(echo "$METRICS_ROW" | jq -c --argjson r "$SCAN_TO_SPAWN_RATIO" '. + {scan_to_spawn_ratio: $r}' 2>/dev/null || echo "$METRICS_ROW")
fi

# ── Emit to stats.duckdb ─────────────────────────────────────────────────
# 1. Record loop_iteration_duration_seconds (legacy metric row)
# 2. Record loop_metrics row with Team Lead token fields
# Skipped on dry-run and on test-origin (guard already checked above).

if [[ "$DRY_RUN" != "true" ]]; then
  (cd "$REPO_ROOT" && python3 -c \
    "from backend.stats_writer import record; record('loop_iteration_duration_seconds', ${DURATION_SECONDS}, 'seconds', source='loop')" \
    2>/dev/null || true)

  (cd "$REPO_ROOT" && python3 -c \
    "from backend.stats_writer import record_loop_iter; \
     record_loop_iter( \
       duration_s=${DURATION_SECONDS}, \
       team_lead_input_tokens=${TL_INPUT}, \
       team_lead_output_tokens=${TL_OUTPUT}, \
       team_lead_cache_read=${TL_CACHE_READ}, \
       team_lead_cache_write=${TL_CACHE_WRITE})" \
    2>/dev/null || true)
fi

# ── Validate row is single-line JSON before writing ───────────────────────
# Reject if:
#   1. jq -e . fails (malformed / empty JSON)
#   2. Compacted output differs from input (contains embedded newlines)
# Exit code 2 on validation failure — callers can distinguish from fatal (1).

VALIDATED_ROW=$(printf '%s' "$METRICS_ROW" | jq -ec . 2>/dev/null) || {
  echo "append-loop-metrics: ERROR — row is not valid JSON; refusing to write" >&2
  exit 2
}

# Check for embedded newlines: jq -ec should produce exactly one line.
# We count lines in the compacted output — valid single-line JSON = 1 line.
NEWLINE_COUNT=$(printf '%s' "$VALIDATED_ROW" | wc -l)
if [[ "$NEWLINE_COUNT" -gt 1 ]]; then
  echo "append-loop-metrics: ERROR — row contains embedded newlines (multi-line JSON); refusing to write" >&2
  exit 2
fi

# ── Write or dry-run ──────────────────────────────────────────────────────

if [[ "$DRY_RUN" == "true" ]]; then
  echo "$VALIDATED_ROW"
else
  mkdir -p "$(dirname "$METRICS_FILE")"
  printf '%s\n' "$VALIDATED_ROW" >> "$METRICS_FILE"
fi
