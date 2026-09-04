#!/usr/bin/env bash
# dispatcher.sh — stateless cron-bridge dispatcher for scheduled jobs.
#
# Invoked once per minute by:
#   * * * * * <repo-root>/scripts/schedule/dispatcher.sh
#
# Per tick:
#   1. Check control-plane gate (gates.scheduled_jobs). Exit 0 if off.
#   2. Parse and validate jobs.yaml (cached by mtime — no per-minute re-parse).
#   3. Compute which jobs are due this minute.
#   4. For each due job: flock + setsid + timeout, log result.
#
# Sentinel exit codes recorded in run log:
#   124 = timeout (job exceeded timeout_seconds)
#   125 = already_running (concurrent invocation)
#   126 = budget_blocked (token_ceiling exceeded)
#   127 = breaker_open (circuit breaker tripped)
#
# Log scrubbing: GH_TOKEN, ANTHROPIC_API_KEY, Authorization/Bearer tokens
# are stripped from captured stdout/stderr before persistence.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=../lib/platform-compat.sh
source "$REPO_ROOT/scripts/lib/platform-compat.sh"
MANIFEST="$SCRIPT_DIR/jobs.yaml"
REGISTRY="$SCRIPT_DIR/jobs"
PARSE_HELPER="$SCRIPT_DIR/parse_jobs.py"
LOCK_BASE="/tmp/autonomous-scheduled-jobs"
# LOG_BASE/RUN_LOG (but deliberately not AUDIT_LOG -- see D#2283) honor
# AUTONOMOUS_TEAM_STATE_DIR when set, same as the delegated-mode convention
# scripts/start-dashboard.sh already uses: unset in production, this is a
# no-op (default unchanged); a caller (a test, or a delegated per-project
# wrapper) that sets it gets these two run-artifact paths redirected there
# instead of the checked-out .autonomous-team/ tree (D#2267).
LOG_BASE="${AUTONOMOUS_TEAM_STATE_DIR:-$REPO_ROOT/.autonomous-team}/scheduled-jobs/logs"
AUDIT_LOG="$REPO_ROOT/.autonomous-team/audit.jsonl"
RUN_LOG="${AUTONOMOUS_TEAM_STATE_DIR:-$REPO_ROOT/.autonomous-team}/scheduled-jobs/runs.jsonl"

# Mtime cache file — stores last known mtime + cached validated job list
MTIME_CACHE_FILE="/tmp/autonomous-sched-manifest-cache.txt"

# ── Utility: log scrubber ─────────────────────────────────────────────────────
scrub_secrets() {
    # Strip GH_TOKEN, ANTHROPIC_API_KEY, Authorization headers, Bearer tokens
    sed \
        -e 's/GH_TOKEN=[^ \t]*/GH_TOKEN=REDACTED/g' \
        -e 's/ANTHROPIC_API_KEY=[^ \t]*/ANTHROPIC_API_KEY=REDACTED/g' \
        -e 's/Authorization:[[:space:]]*[Bb]earer[[:space:]]*[^ \t]*/Authorization: Bearer REDACTED/gi' \
        -e 's/Authorization:[[:space:]]*[^ \t]*/Authorization: REDACTED/gi' \
        -e 's/Bearer [A-Za-z0-9._~+/=-]\{8,\}/Bearer REDACTED/g'
}

# ── Utility: write audit log line ─────────────────────────────────────────────
audit_line() {
    local role="$1" event="$2" job_name="$3" exit_code="${4:-}"
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    local line
    line="{\"ts\":\"$ts\",\"role\":\"scheduled-job\",\"event\":\"$event\",\"job\":\"$job_name\",\"exit_code\":\"$exit_code\"}"
    mkdir -p "$(dirname "$AUDIT_LOG")"
    echo "$line" >> "$AUDIT_LOG" || true
}

# ── Utility: write run log line ───────────────────────────────────────────────
run_log_line() {
    local job="$1" started_at="$2" ended_at="$3" exit_code="$4" note="${5:-}"
    mkdir -p "$(dirname "$RUN_LOG")"
    local line
    line="{\"job\":\"$job\",\"started_at\":\"$started_at\",\"ended_at\":\"$ended_at\",\"exit_code\":$exit_code,\"note\":\"$note\"}"
    echo "$line" >> "$RUN_LOG" || true
}

# ── Utility: team-log line ────────────────────────────────────────────────────
team_log() {
    local msg="$1"
    # Use rotate-team-log.sh if available; otherwise write to run log only
    if [[ -x "$REPO_ROOT/scripts/rotate-team-log.sh" ]]; then
        "$REPO_ROOT/scripts/rotate-team-log.sh" comment "[scheduler] $msg" 2>/dev/null || true
    fi
}

# ── Control plane gate check ──────────────────────────────────────────────────
GATE_VAL=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.scheduled_jobs 2>/dev/null | tr -d '"' || echo "false")
if [[ "$GATE_VAL" != "true" ]]; then
    TS_NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    run_log_line "dispatcher" "$TS_NOW" "$TS_NOW" 0 "gate_off"
    audit_line "scheduler" "gate_off" "dispatcher" "0"
    exit 0
fi

# ── Manifest mtime-cached parse ───────────────────────────────────────────────
# CURRENT_MTIME is only ever compared for equality against the cached value
# below, so "0" on a genuine stat failure is a safe fallback here — it just
# forces a re-parse (and re-parsing an unreadable manifest fails loudly on
# its own). This used to be a uname==Darwin branch: on any host that isn't
# literally "Darwin" but also isn't GNU (or vice versa), it silently landed
# on "0" every tick, which never changes — so a manifest edit would never
# be picked up until the cache file was deleted by hand (D#2263).
CURRENT_MTIME=$(pc_stat_mtime "$MANIFEST" 2>/dev/null) || CURRENT_MTIME=0

CACHED_MTIME=""
CACHED_JOBS_FILE="/tmp/autonomous-sched-jobs-cache.json"

if [[ -f "$MTIME_CACHE_FILE" ]]; then
    CACHED_MTIME=$(cat "$MTIME_CACHE_FILE" 2>/dev/null || echo "")
fi

if [[ "$CURRENT_MTIME" != "$CACHED_MTIME" ]] || [[ ! -f "$CACHED_JOBS_FILE" ]]; then
    # Validate manifest — exit on schema error
    VALIDATE_OUT=$(python3 "$PARSE_HELPER" \
        --manifest "$MANIFEST" \
        --registry "$REGISTRY" \
        --validate-only 2>&1) || {
        EXIT_CODE=$?
        TS_ERR="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        run_log_line "dispatcher" "$TS_ERR" "$TS_ERR" "$EXIT_CODE" "schema_invalid"
        audit_line "scheduler" "schema_invalid" "dispatcher" "$EXIT_CODE"
        team_log "schema_invalid: manifest failed validation — dispatcher exiting. Error: $VALIDATE_OUT"
        exit "$EXIT_CODE"
    }
    # Cache the validated job list so mtime check is meaningful next tick
    python3 "$PARSE_HELPER" \
        --manifest "$MANIFEST" \
        --registry "$REGISTRY" \
        --all-jobs > "$CACHED_JOBS_FILE" 2>/dev/null || true
    echo "$CURRENT_MTIME" > "$MTIME_CACHE_FILE"
fi

# ── Compute due jobs ──────────────────────────────────────────────────────────
TICK_MINUTE="$(date -u +%Y-%m-%dT%H:%M)"
DUE_JSON=$(python3 "$PARSE_HELPER" \
    --manifest "$MANIFEST" \
    --registry "$REGISTRY" \
    --minute "$TICK_MINUTE" 2>/dev/null) || {
    EXIT_CODE=$?
    TS_ERR="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    run_log_line "dispatcher" "$TS_ERR" "$TS_ERR" "$EXIT_CODE" "parse_error"
    audit_line "scheduler" "parse_error" "dispatcher" "$EXIT_CODE"
    exit "$EXIT_CODE"
}

# ── Check circuit breaker for a job ──────────────────────────────────────────
is_breaker_open() {
    local job_name="$1"
    local breaker_file="/tmp/autonomous-sched-breaker-$job_name.json"
    [[ -f "$breaker_file" ]] || return 1

    local consecutive_failures cooldown_until now_epoch
    consecutive_failures=$(python3 -c "import json; d=json.load(open('$breaker_file')); print(d.get('consecutive_failures', 0))" 2>/dev/null || echo "0")
    cooldown_until=$(python3 -c "import json; d=json.load(open('$breaker_file')); print(d.get('cooldown_until', 0))" 2>/dev/null || echo "0")
    now_epoch=$(date +%s)

    if [[ "$consecutive_failures" -ge 3 ]] && [[ "$now_epoch" -lt "$cooldown_until" ]]; then
        return 0  # breaker is open
    fi
    return 1
}

record_job_result() {
    local job_name="$1" exit_code="$2"
    local breaker_file="/tmp/autonomous-sched-breaker-$job_name.json"
    local now_epoch
    now_epoch=$(date +%s)

    if [[ "$exit_code" -eq 0 ]]; then
        # Success — reset breaker
        echo '{"consecutive_failures":0,"cooldown_until":0}' > "$breaker_file"
    else
        local consecutive_failures=0
        [[ -f "$breaker_file" ]] && consecutive_failures=$(python3 -c \
            "import json; d=json.load(open('$breaker_file')); print(d.get('consecutive_failures', 0))" 2>/dev/null || echo "0")
        consecutive_failures=$((consecutive_failures + 1))
        local cooldown_until=0
        if [[ "$consecutive_failures" -ge 3 ]]; then
            cooldown_until=$((now_epoch + 3600))  # 60 minutes
        fi
        python3 -c "import json; f=open('$breaker_file','w'); json.dump({'consecutive_failures':$consecutive_failures,'cooldown_until':$cooldown_until}, f)" 2>/dev/null || true
    fi
}

# ── Run a single job ──────────────────────────────────────────────────────────
run_job() {
    local job_name="$1"
    local job_key="$2"
    local timeout_seconds="$3"
    local token_ceiling="${4:-0}"

    local registry_file="$REGISTRY/$job_key.sh"
    local lock_dir="$LOCK_BASE"
    local lock_file="$lock_dir/$job_name.lock"
    local log_dir="$LOG_BASE/$job_name"
    local ts_start
    ts_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    local ts_start_epoch
    ts_start_epoch=$(date +%s)

    mkdir -p "$lock_dir" "$log_dir"

    local log_file="$log_dir/$(date -u +%Y%m%dT%H%M%S).log"

    # Check token_ceiling budget before forking
    if [[ "$token_ceiling" -gt 0 ]]; then
        local budget_spent
        budget_spent=$(python3 "$REPO_ROOT/backend/budget.py" status 2>/dev/null \
            | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('spent',0))" 2>/dev/null || echo "0")
        if [[ "$budget_spent" -ge "$token_ceiling" ]]; then
            local ts_end
            ts_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            run_log_line "$job_name" "$ts_start" "$ts_end" 126 "budget_blocked"
            audit_line "scheduler" "budget_blocked" "$job_name" "126"
            team_log "job $job_name blocked — token budget $budget_spent >= ceiling $token_ceiling (exit 126)"
            return 126
        fi
    fi

    # Check circuit breaker
    if is_breaker_open "$job_name"; then
        local ts_end
        ts_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        run_log_line "$job_name" "$ts_start" "$ts_end" 127 "breaker_open"
        audit_line "scheduler" "breaker_open" "$job_name" "127"
        return 127
    fi

    # Try to acquire lock (non-blocking)
    exec 9>"$lock_file"
    if ! flock -n 9; then
        # Already running
        local ts_end
        ts_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        run_log_line "$job_name" "$ts_start" "$ts_end" 125 "already_running"
        audit_line "scheduler" "already_running" "$job_name" "125"
        team_log "job $job_name skipped — already running (exit 125)"
        exec 9>&-
        return 125
    fi

    # Run job with setsid + timeout + process-group kill on timeout
    local job_exit=0
    # Capture output (capped at 1MB: head 512KB + tail 512KB via post-processing)
    (
        setsid timeout \
            --kill-after=5s \
            --signal=TERM \
            "${timeout_seconds}s" \
            "$registry_file"
    ) > "$log_file.raw" 2>&1 || job_exit=$?

    # Scrub secrets and cap at 1MB
    local raw_size
    raw_size=$(wc -c < "$log_file.raw" 2>/dev/null || echo "0")
    if [[ "$raw_size" -gt 1048576 ]]; then
        # head 512KB + tail 512KB
        {
            head -c 524288 "$log_file.raw"
            echo "... [truncated] ..."
            tail -c 524288 "$log_file.raw"
        } | scrub_secrets > "$log_file"
    else
        scrub_secrets < "$log_file.raw" > "$log_file"
    fi
    rm -f "$log_file.raw"

    local ts_end
    ts_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # Record result
    run_log_line "$job_name" "$ts_start" "$ts_end" "$job_exit" ""
    audit_line "scheduler" "job_complete" "$job_name" "$job_exit"
    record_job_result "$job_name" "$job_exit"

    if [[ "$job_exit" -ne 0 ]]; then
        team_log "job $job_name exited $job_exit (log: $log_file)"
    fi

    # Release lock
    exec 9>&-
    return "$job_exit"
}

# ── Dispatch due jobs ─────────────────────────────────────────────────────────
# Parse due jobs from JSON array
JOB_COUNT=$(echo "$DUE_JSON" | python3 -c "import json,sys; jobs=json.load(sys.stdin); print(len(jobs))" 2>/dev/null || echo "0")

if [[ "$JOB_COUNT" -eq 0 ]]; then
    exit 0
fi

# Extract and run each due job
echo "$DUE_JSON" | python3 -c "
import json, sys
jobs = json.load(sys.stdin)
for j in jobs:
    print(j['name'], j['job'], j['timeout_seconds'], j.get('token_ceiling', 0))
" 2>/dev/null | while IFS=' ' read -r job_name job_key timeout_seconds token_ceiling; do
    run_job "$job_name" "$job_key" "$timeout_seconds" "$token_ceiling" &
done

# Wait for all forked jobs to complete
wait

exit 0
