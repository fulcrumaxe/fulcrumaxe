#!/usr/bin/env bash
set -m   # enable job control for process group cleanup on exit

# --- Resolve REPO_DIR from this script's own location — do not hardcode one
#     machine's checkout path. Cron has no meaningful cwd, so this must not
#     rely on the working directory either. ---
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! git -C "$REPO_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "[$(date +%H:%M:%S)] FATAL: REPO_DIR ($REPO_DIR) is not inside a git checkout" >&2
  exit 1
fi
GIT_TOPLEVEL="$(git -C "$REPO_DIR" rev-parse --show-toplevel)"
if [ "$REPO_DIR" != "$GIT_TOPLEVEL" ]; then
  echo "[$(date +%H:%M:%S)] FATAL: resolved REPO_DIR ($REPO_DIR) is not the git checkout root ($GIT_TOPLEVEL)" >&2
  exit 1
fi

# shellcheck source=scripts/lib/platform-compat.sh
source "$REPO_DIR/scripts/lib/platform-compat.sh" || {
  echo "[$(date +%H:%M:%S)] FATAL: could not source scripts/lib/platform-compat.sh" >&2
  exit 1
}

# --- Resolve CLAUDE_BIN: $CLAUDE_BIN env override, then PATH. There used to
#     be a third, vendored interpreter tier here; nothing in this repo ever
#     installed a binary there, so it could never fire — dropped rather than
#     carried as dead weight. Fail loudly — a silent "no such file" later is
#     worse. ---
if [ -z "${CLAUDE_BIN:-}" ]; then
  if command -v claude &>/dev/null; then
    CLAUDE_BIN="$(command -v claude)"
  else
    echo "[$(date +%H:%M:%S)] FATAL: could not resolve claude — checked \$CLAUDE_BIN and PATH" >&2
    exit 1
  fi
fi

LOG="$REPO_DIR/.autonomous-team/loop.log"
LOCK="$REPO_DIR/.autonomous-team/loop.lock"
HEALTH="$REPO_DIR/.autonomous-team/health.json"

cd "$REPO_DIR"
mkdir -p "$REPO_DIR/.autonomous-team"

# --- Env bootstrap: load API keys and set PATH/GH_CONFIG_DIR/GH_REPO ---
# shellcheck source=scripts/env-bootstrap.sh
source "$REPO_DIR/scripts/env-bootstrap.sh" || {
  echo "[$(date +%H:%M:%S)] FATAL: env-bootstrap failed — cannot start iteration" >> "$LOG"
  exit 1
}

# --- Resolve team-log issue number early (needed by crash recovery) ---
LOG_ISSUE=""
if command -v gh &>/dev/null; then
  LOG_ISSUE=$(gh issue list --label team-log --state open --json number --jq '.[0].number' \
    --repo "$GH_REPO" 2>/dev/null || true)
fi

# --- Helper: consolidated lockfile PID + age check ---
# Returns 0 if it's safe to proceed (no live lock), 1 if a live iteration is running.
_check_and_clean_lockfile() {
  if [ ! -f "$LOCK" ]; then return 0; fi
  LOCK_PID=$(cat "$LOCK" 2>/dev/null | tr -d '[:space:]')
  if [ -z "$LOCK_PID" ]; then rm -f "$LOCK"; return 0; fi
  # Age check: if lockfile older than 30 minutes, treat as stale even if PID alive.
  # If the mtime can't be read at all, treat the lock as brand new (age 0)
  # rather than ancient — a stat failure here must never look like "stale",
  # since that's what would force-kill a live iteration's PID (D#2263).
  if LOCK_MTIME=$(pc_stat_mtime "$LOCK" 2>/dev/null); then
    LOCK_AGE=$(( $(date +%s) - LOCK_MTIME ))
  else
    LOCK_AGE=0
  fi
  if [ "$LOCK_AGE" -gt 1800 ]; then
    echo "[$(date +%H:%M:%S)] Removing aged lockfile (${LOCK_AGE}s old, PID $LOCK_PID)" >> "$LOG"
    kill -TERM "$LOCK_PID" 2>/dev/null || true
    rm -f "$LOCK"
    return 0
  fi
  # PID liveness check
  if kill -0 "$LOCK_PID" 2>/dev/null; then
    echo "[$(date +%H:%M:%S)] Skipping — iteration $LOCK_PID still running" >> "$LOG"
    return 1
  fi
  echo "[$(date +%H:%M:%S)] Removing stale lockfile (PID $LOCK_PID is dead)" >> "$LOG"
  rm -f "$LOCK"
  return 0
}

# --- Helper: detect and recover from a previous crash ---
_recover_from_crash() {
  MARKER=$(python3 backend/blackboard.py read loop/crash_marker 2>/dev/null || true)
  if [ -z "$MARKER" ] || [ "$MARKER" = "null" ]; then return; fi
  MARKER_PID=$(echo "$MARKER" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('pid',''))" 2>/dev/null || true)
  if [ -n "$MARKER_PID" ] && ! kill -0 "$MARKER_PID" 2>/dev/null; then
    echo "[$(date +%H:%M:%S)] Crash recovery: found crash marker from dead PID $MARKER_PID" >> "$LOG"
    if [ -n "$LOG_ISSUE" ]; then
      gh issue comment "$LOG_ISSUE" --body \
        "[$(date +%H:%M)] crash-recovery: previous iteration (PID $MARKER_PID) crashed — cleaning up stale state" \
        --repo "$GH_REPO" 2>/dev/null || true
    fi
    python3 backend/blackboard.py delete loop/crash_marker 2>/dev/null || true
  fi
}

# --- Helper: record a crash entry to loop-metrics.jsonl ---
_record_crash_metric() {
  local exit_code=$1 duration=$2 stderr_file=$3 timed_out=$4
  local error_line
  error_line=$(head -1 "$stderr_file" 2>/dev/null | head -c 200 | tr '"' "'" || echo "unknown")
  printf '{"timestamp":"%s","duration_seconds":%d,"status":"crash","exit_code":%d,"timed_out":%s,"error":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$duration" "$exit_code" "$timed_out" "$error_line" \
    >> "$REPO_DIR/.autonomous-team/loop-metrics.jsonl"
}

# --- Process watchdog: kill stale autonomous-team processes before lock check ---
timeout 15 bash "$REPO_DIR/scripts/process-watchdog.sh" || true

SESSION_FILE="$REPO_DIR/.autonomous-team/session.json"
SESSION_ARGS=()

# Read session_id from session.json if it exists and is valid.
if [ -f "$SESSION_FILE" ]; then
  SESSION_ID=$(python3 -c "
import json, sys, os
from datetime import datetime, timezone
try:
    data = json.loads(open('$SESSION_FILE').read())
    sid = data.get('session_id', '')
    count = data.get('iteration_count', 0)
    created = datetime.fromisoformat(data.get('created_at', ''))
    max_iter = int(os.environ.get('AF_SESSION_MAX_ITERATIONS', '20'))
    max_age = int(os.environ.get('AF_SESSION_MAX_AGE_MINUTES', '120'))
    age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
    if sid and count < max_iter and age_min < max_age:
        print(sid)
    else:
        print('')
except Exception:
    print('')
" 2>/dev/null)
  # Note: the claude CLI doesn't support --session yet.
  # Session persistence works through the FIFO/server path (session_id in JSON).
  # For direct CLI mode, we skip session args — each run starts fresh.
  # if [ -n "$SESSION_ID" ]; then
  #   SESSION_ARGS=(--session "$SESSION_ID")
  # fi
  :
fi

# --- Crash recovery: detect and clean up previous crash before acquiring lock ---
_recover_from_crash

# --- Lock check (consolidated PID + age function) ---
_check_and_clean_lockfile || exit 0

echo $$ > "$LOCK"
STDERR_LOG=$(mktemp)
START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --- Record loop-run start (loop_runs.py) ---
LOOP_RUN_FILE=$(python3 backend/loop_runs.py start 2>/dev/null || true)

# --- Set crash marker in blackboard (cleared by EXIT trap on any exit) ---
python3 backend/blackboard.py write loop/crash_marker \
  "{\"pid\": $$, \"started_at\": \"$START_TS\"}" 2>/dev/null || true

# EXIT trap: kill process group, remove lockfile and temp files, clear crash marker
trap 'kill -- -$$ 2>/dev/null; rm -f "$LOCK" "$STDERR_LOG"; python3 backend/blackboard.py delete loop/crash_marker 2>/dev/null || true' EXIT

echo "[$(date +%H:%M:%S)] ════ Loop iteration start ════" >> "$LOG"

# --- Helper: update health.json ---
_update_health() {
  local success=$1   # "true" or "false"
  local exit_code=$2
  local stderr_excerpt=$3

  python3 -c "
import json, os, tempfile
from datetime import datetime, timezone

path = '$HEALTH'
now = datetime.now(timezone.utc).isoformat()

try:
    data = json.loads(open(path).read()) if os.path.exists(path) else {}
except Exception:
    data = {}

data.setdefault('consecutive_failures', 0)
data.setdefault('last_error', '')
data.setdefault('last_failure_at', '')
data.setdefault('last_success_at', '')

if '$success' == 'true':
    data['consecutive_failures'] = 0
    data['last_success_at'] = now
else:
    data['consecutive_failures'] = data['consecutive_failures'] + 1
    data['last_error'] = 'exit $exit_code: $(echo "$stderr_excerpt" | head -1 | sed "s/'/\\\\\'/g")'
    data['last_failure_at'] = now

tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path) or '.')
with os.fdopen(tmp_fd, 'w') as f:
    json.dump(data, f, indent=2)
os.replace(tmp_path, path)
print(data['consecutive_failures'])
" 2>/dev/null
}

# --- Helper: post failure comment to team-log ---
_post_failure_to_teamlog() {
  local exit_code=$1
  local stderr_log=$2

  if ! command -v gh &>/dev/null; then
    return
  fi

  if [ -z "$LOG_ISSUE" ]; then
    return
  fi

  local excerpt
  excerpt=$(tail -5 "$stderr_log" 2>/dev/null | head -5)

  gh issue comment "$LOG_ISSUE" --body "$(printf '[%s] Loop iteration failed — exit code %s\n\nStderr (last 5 lines):\n```\n%s\n```' \
    "$(date +%H:%M)" "$exit_code" "$excerpt")" \
    --repo "$GH_REPO" 2>/dev/null || true
}

# --- Helper: escalate to needs-boss if consecutive failures >= 3 ---
_maybe_escalate() {
  local consecutive=$1
  local stderr_log=$2

  if [ "$consecutive" -lt 3 ]; then
    return
  fi

  # Check if open needs-boss issue already exists for this streak.
  local existing
  existing=$(gh issue list --label needs-boss --state open --json number --jq '.[0].number' \
    --repo "$GH_REPO" 2>/dev/null)
  if [ -n "$existing" ]; then
    return
  fi

  local health_contents
  health_contents=$(python3 -c "import json; print(json.dumps(json.load(open('$HEALTH')), indent=2))" 2>/dev/null || echo "(unreadable)")

  local stderr_tail
  stderr_tail=$(tail -20 "$stderr_log" 2>/dev/null)

  local last_error
  last_error=$(python3 -c "import json; d=json.load(open('$HEALTH')); print(d.get('last_error',''))" 2>/dev/null || echo "unknown")

  gh issue create \
    --label needs-boss \
    --title "Loop health alert: $consecutive consecutive cron failures" \
    --body "$(printf 'The cron loop has failed %s times in a row.\n\n**Last error:** %s\n\n**Stderr (last 20 lines):**\n```\n%s\n```\n\n**health.json:**\n```json\n%s\n```' \
      "$consecutive" "$last_error" "$stderr_tail" "$health_contents")" \
    --repo "$GH_REPO" 2>/dev/null || true
}

# --- Run claude with retry for transient errors (exit 2 or 124) ---
_run_claude() {
  local prompt
  prompt=$(cat <<PROMPT
You are the Team Lead for $GH_REPO.
Run ONE /loop iteration per CLAUDE.md.

REPO SCOPE INVARIANT: every gh CLI call must include --repo $GH_REPO.

Steps (in order):
1. cat .autonomous-team/now.md
2. gh repo view --repo $GH_REPO --json nameWithOwner,defaultBranchRef
3. Ensure team-log issue exists (label: team-log)
4. Scan open Discussions (GraphQL) — parse STATUS lines in each body
5. Check open PRs: gh pr list --repo $GH_REPO --state open --json number,title,labels
6. ACT ON WORK (CLAUDE.md /loop step 5):
   a. Any Discussion with STATUS:SPEC_READY and no active impl-coordinator → spawn impl-coordinator via Team Lead
   b. Any open PR missing review labels → spawn code-reviewer
   c. Nothing actionable → notify project-manager to run idea generation
7. Auto-merge check (CLAUDE.md /loop step 6): for each open PR, check gate labels; if all required gates are met, merge via: gh pr merge {number} --squash --repo $GH_REPO
8. Update .autonomous-team/now.md with what you found and what you acted on
PROMPT
)
    timeout 300 \
      "$CLAUDE_BIN" -p "$prompt" \
      >> "$LOG" 2>>"$STDERR_LOG"
}

_run_claude
EXIT_CODE=$?

# Single retry on transient errors (claude startup failure=2, timeout=124).
if [ "$EXIT_CODE" -eq 2 ] || [ "$EXIT_CODE" -eq 124 ]; then
  echo "[$(date +%H:%M:%S)] Transient exit $EXIT_CODE — retrying once after 10s" >> "$LOG"
  sleep 10
  _run_claude
  EXIT_CODE=$?
fi

# --- Update iteration count in session.json after successful claude exit ---
if [ "$EXIT_CODE" -ne 124 ] && [ -f "$SESSION_FILE" ]; then
  python3 -c "
import json, sys, os, tempfile
try:
    path = '$SESSION_FILE'
    data = json.loads(open(path).read())
    data['iteration_count'] = data.get('iteration_count', 0) + 1
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(tmp_fd, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)
except Exception as e:
    print(f'[run-loop] warning: could not update session count: {e}', file=sys.stderr)
" 2>>"$LOG"
fi

END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ "$EXIT_CODE" -eq 124 ]; then
  TIMED_OUT=true
  echo "[$(date +%H:%M:%S)] TIMEOUT — iteration killed after 300s" >> "$LOG"
else
  TIMED_OUT=false
fi

# Compute duration in seconds (GNU date; macOS fallback included).
START_EPOCH=$(date -u -d "$START_TS" +%s 2>/dev/null \
  || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$START_TS" +%s 2>/dev/null \
  || echo 0)
END_EPOCH=$(date -u -d "$END_TS" +%s 2>/dev/null \
  || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$END_TS" +%s 2>/dev/null \
  || echo 0)
DURATION_S=$((END_EPOCH - START_EPOCH))

# --- On failure: append stderr to log, post to team-log, update health, record crash metric ---
if [ "$EXIT_CODE" -ne 0 ]; then
  echo "[$(date +%H:%M:%S)] ERROR — exit code $EXIT_CODE — stderr follows:" >> "$LOG"
  tail -20 "$STDERR_LOG" | sed 's/^/  [stderr] /' >> "$LOG"

  _post_failure_to_teamlog "$EXIT_CODE" "$STDERR_LOG"

  CONSECUTIVE=$(_update_health "false" "$EXIT_CODE" "$(head -1 "$STDERR_LOG" 2>/dev/null)")
  _maybe_escalate "${CONSECUTIVE:-1}" "$STDERR_LOG"

  # Record crash entry to loop-metrics.jsonl (agent is dead, so shell writes it directly)
  _record_crash_metric "$EXIT_CODE" "$DURATION_S" "$STDERR_LOG" "$TIMED_OUT"
else
  _update_health "true" 0 ""
fi

echo "[$(date +%H:%M:%S)] ════ Loop iteration end ════" >> "$LOG"

# --- Finalise loop-run record (exit code, duration, stderr) ---
if [ -n "${LOOP_RUN_FILE:-}" ]; then
  python3 backend/loop_runs.py finish \
    --file "$LOOP_RUN_FILE" \
    --exit "$EXIT_CODE" \
    --stderr "$STDERR_LOG" 2>/dev/null || true
fi

echo "[$(date +%H:%M:%S)] SUMMARY {\"start\":\"$START_TS\",\"end\":\"$END_TS\",\"duration_s\":$DURATION_S,\"exit_code\":$EXIT_CODE,\"timed_out\":$TIMED_OUT}" >> "$LOG"
