#!/usr/bin/env bash
# spawn-browser-tester.sh — drain the browser-tour queue and spawn browser-tester agents.
#
# Called from /loop step 7.5. Reads .autonomous-team/browser-tour-queue.jsonl,
# processes entries older than 30min, newest-last (oldest-first), capped at
# 2 spawns per /loop iteration.
#
# Nightly trigger: if gates.browser_tester_periodic is true AND no tour has run
# in the last 24h, enqueues a full-dashboard nightly entry before draining.
#
# Spawns go through pre-spawn-check.sh (which calls claude_spawn_tracker.record()).
# The global breaker can trip browser-tester just like any other spawn role.
#
# Queue file format (one JSON object per line):
#   {"trigger":"post-merge","pr":42,"affected_pages":["/ideas"],"queued_at":"ISO","status":"pending"}
#   {"trigger":"nightly","pr":null,"affected_pages":["/","/ideas","/prs",...],
#    "queued_at":"ISO","status":"pending"}
#
# Done file: .autonomous-team/browser-tour-queue.done.jsonl
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop.
#            Mock spawner via stub scripts in tests. See Discussion #439.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Allow tests to override REPO_ROOT via env var; default to parent of script dir.
# When REPO_ROOT is overridden, also re-point SCRIPT_DIR so helper scripts
# (pre-spawn-check.sh, rotate-team-log.sh) are found under the test root.
if [[ -n "${REPO_ROOT:-}" ]]; then
  SCRIPT_DIR="${REPO_ROOT}/scripts"
else
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

QUEUE_FILE="${REPO_ROOT}/.autonomous-team/browser-tour-queue.jsonl"
DONE_FILE="${REPO_ROOT}/.autonomous-team/browser-tour-queue.done.jsonl"
TOURS_DIR="${REPO_ROOT}/.autonomous-team/browser-tours"
MAX_SPAWNS_PER_ITER=2
MIN_AGE_SECONDS=1800  # 30 minutes

# Allow test stubs to override the spawner
SPAWNER_SCRIPT="${BROWSER_TESTER_SPAWNER:-}"

spawned=0
errors=0

# ── Ensure dirs exist ─────────────────────────────────────────────────────────
mkdir -p "$TOURS_DIR"
touch "$QUEUE_FILE" "$DONE_FILE"

# ── Helper: ISO timestamp to epoch seconds ────────────────────────────────────
iso_to_epoch() {
  python3 -c "
import sys, datetime
ts = sys.argv[1]
try:
    dt = datetime.datetime.fromisoformat(ts.replace('Z','+00:00'))
    print(int(dt.timestamp()))
except Exception:
    print(0)
" "$1" 2>/dev/null || echo 0
}

now_epoch=$(date +%s)

# ── Nightly trigger ───────────────────────────────────────────────────────────
PERIODIC_GATE=$(python3 "${REPO_ROOT}/backend/control_plane.py" get gates.browser_tester_periodic 2>/dev/null || echo "false")

if [[ "$PERIODIC_GATE" == "true" ]]; then
  # Check last tour timestamp from done file or tours dir
  last_tour_epoch=0
  if [[ -f "$DONE_FILE" ]]; then
    last_done_ts=$(grep '"trigger":"nightly"' "$DONE_FILE" 2>/dev/null | tail -1 \
      | python3 -c "import sys,json; e=json.loads(sys.stdin.read()); print(e.get('queued_at',''))" 2>/dev/null || echo "")
    if [[ -n "$last_done_ts" ]]; then
      last_tour_epoch=$(iso_to_epoch "$last_done_ts")
    fi
  fi

  twenty_four_h=86400
  if (( now_epoch - last_tour_epoch > twenty_four_h )); then
    echo "[spawn-browser-tester] Nightly gate enabled and last tour >24h ago — enqueuing nightly entry"
    queued_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    nightly_entry=$(python3 -c "
import json
entry = {
  'trigger': 'nightly',
  'pr': None,
  'affected_pages': ['/', '/ideas', '/discussions', '/prs', '/kpi', '/loop-timeline', '/loop-controller'],
  'tour_goal': 'Full nightly regression sweep. Visit each top-level dashboard page. Report any rendering errors, broken charts, missing data, console errors, or network failures.',
  'queued_at': '${queued_at}',
  'status': 'pending'
}
print(json.dumps(entry))
")
    echo "$nightly_entry" >> "$QUEUE_FILE"
  else
    echo "[spawn-browser-tester] Nightly gate enabled but last tour was recent — skipping nightly enqueue"
  fi
else
  echo "[spawn-browser-tester] gates.browser_tester_periodic=false — skipping nightly trigger"
fi

# ── Drain queue ───────────────────────────────────────────────────────────────
if [[ ! -s "$QUEUE_FILE" ]]; then
  echo "[spawn-browser-tester] Queue is empty — nothing to drain"
  exit 0
fi

# Read pending entries, oldest first (preserve line order from file)
# Filter: status=pending AND age >= MIN_AGE_SECONDS
PENDING_ENTRIES=$(python3 - "$QUEUE_FILE" "$now_epoch" "$MIN_AGE_SECONDS" <<'PYEOF'
import sys, json, datetime

queue_file = sys.argv[1]
now_epoch = int(sys.argv[2])
min_age = int(sys.argv[3])

entries = []
with open(queue_file) as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("status") != "pending":
            continue
        ts = e.get("queued_at", "")
        try:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = now_epoch - int(dt.timestamp())
        except Exception:
            age = min_age  # treat unknown age as eligible
        if age >= min_age:
            entries.append((i, e))

# Print as JSON array with original line indices
print(json.dumps(entries))
PYEOF
)

if [[ -z "$PENDING_ENTRIES" || "$PENDING_ENTRIES" == "[]" ]]; then
  echo "[spawn-browser-tester] No pending entries older than ${MIN_AGE_SECONDS}s"
  exit 0
fi

entry_count=$(echo "$PENDING_ENTRIES" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
echo "[spawn-browser-tester] $entry_count eligible queue entries — processing up to $MAX_SPAWNS_PER_ITER"

# Process entries up to cap
for idx in $(seq 0 $((MAX_SPAWNS_PER_ITER - 1))); do
  entry_json=$(echo "$PENDING_ENTRIES" | python3 -c "
import sys, json
data = json.load(sys.stdin)
idx = int(sys.argv[1])
if idx < len(data):
    line_idx, entry = data[idx]
    entry['_line_idx'] = line_idx
    print(json.dumps(entry))
" "$idx" 2>/dev/null || echo "")

  if [[ -z "$entry_json" ]]; then
    break
  fi

  trigger=$(echo "$entry_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('trigger','unknown'))")
  pr=$(echo "$entry_json" | python3 -c "import sys,json; v=json.load(sys.stdin).get('pr'); print(v if v else '')")
  affected_pages=$(echo "$entry_json" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('affected_pages',[])))")
  tour_goal=$(echo "$entry_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tour_goal',''))")
  line_idx=$(echo "$entry_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('_line_idx',0))")

  echo "[spawn-browser-tester] Entry $((idx+1)): trigger=$trigger pr=${pr:-none}"

  # Pre-spawn check (cost guard / circuit breaker)
  DISC_ARG=""
  [[ -n "$pr" ]] && DISC_ARG="--discussion $pr"
  BT_EVENT_ID="browser-tester-${pr:-${trigger}}-$(date +%s)"

  SPAWN_CTX=$(bash "${SCRIPT_DIR}/pre-spawn-check.sh" --role browser-tester $DISC_ARG --event-id "$BT_EVENT_ID" 2>&1) && SPAWN_RC=0 || SPAWN_RC=$?
  if [[ $SPAWN_RC -ne 0 ]]; then
    echo "[spawn-browser-tester] Spawn blocked by pre-spawn-check — entry stays queued: $SPAWN_CTX"
    errors=$((errors + 1))
    continue
  fi

  # Record in claude_spawn_tracker
  SOURCE_TAG="browser-tester-${trigger}"
  python3 -c "
import sys
sys.path.insert(0, '${REPO_ROOT}')
from backend.claude_spawn_tracker import record
record('${SOURCE_TAG}')
print('[spawn-browser-tester] claude_spawn_tracker.record() ok')
" 2>/dev/null || echo "[spawn-browser-tester] Warning: claude_spawn_tracker.record() failed (non-fatal)"

  # Resolve workflow
  WORKFLOW_ARGS=(
    "--input" "tour_goal=${tour_goal:-Tour all dashboard pages}"
    "--input" "affected_pages=${affected_pages}"
    "--input" "trigger=${trigger}"
    "--input" "report_to=team-lead"
  )
  [[ -n "$pr" ]] && WORKFLOW_ARGS+=("--input" "pr_number=${pr}")

  RESOLVED=$(python3 "${REPO_ROOT}/backend/workflow_runner.py" resolve browser-tester \
    "${WORKFLOW_ARGS[@]}" 2>&1) && WF_RC=0 || WF_RC=$?

  if [[ $WF_RC -ne 0 ]]; then
    echo "[spawn-browser-tester] Warning: workflow_runner failed — using direct role: $RESOLVED"
    ROLE="browser-tester"
  else
    ROLE=$(echo "$RESOLVED" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('steps',[{}])[0].get('agent','browser-tester'))" 2>/dev/null || echo "browser-tester")
  fi

  # Build tour output filename
  NOW_ISO=$(date -u +"%Y-%m-%dT%H-%M")
  if [[ -n "$pr" ]]; then
    TOUR_OUT="${TOURS_DIR}/post-merge-pr${pr}-${NOW_ISO}.json"
  else
    TOUR_OUT="${TOURS_DIR}/nightly-${NOW_ISO}.json"
  fi

  echo "[spawn-browser-tester] Spawning $ROLE (trigger=$trigger, output=$TOUR_OUT)"

  # Spawn the agent — use stub if BROWSER_TESTER_SPAWNER is set (for tests)
  SPAWN_SUCCESS=false
  if [[ -n "$SPAWNER_SCRIPT" ]]; then
    # Test stub path
    "$SPAWNER_SCRIPT" \
      --role "$ROLE" \
      --trigger "$trigger" \
      --pr "${pr:-}" \
      --affected-pages "$affected_pages" \
      --tour-goal "$tour_goal" \
      --output "$TOUR_OUT" \
      && SPAWN_SUCCESS=true || true
  else
    # Real spawn: log intention (actual Agent() spawn happens via Team Lead)
    bash "${SCRIPT_DIR}/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] spawn-browser-tester: queuing $ROLE spawn trigger=$trigger pr=${pr:-none} pages=${affected_pages}" \
      2>/dev/null || true
    SPAWN_SUCCESS=true
  fi

  if $SPAWN_SUCCESS; then
    # Mark entry as drained: update status in queue file and append to done file
    python3 - "$QUEUE_FILE" "$line_idx" <<'MARK_DONE'
import sys, json, pathlib

queue_file = pathlib.Path(sys.argv[1])
target_idx = int(sys.argv[2])

lines = queue_file.read_text().splitlines(keepends=True)
if target_idx < len(lines):
    try:
        entry = json.loads(lines[target_idx])
        entry["status"] = "drained"
        lines[target_idx] = json.dumps(entry) + "\n"
        queue_file.write_text("".join(lines))
    except Exception as e:
        print(f"Warning: could not mark line {target_idx} as drained: {e}", file=sys.stderr)
MARK_DONE

    # Append to done file
    done_entry=$(echo "$entry_json" | python3 -c "
import sys,json
e=json.load(sys.stdin)
e.pop('_line_idx', None)
e['status']='drained'
import datetime
e['drained_at']=datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
print(json.dumps(e))
")
    echo "$done_entry" >> "$DONE_FILE"

    spawned=$((spawned + 1))
    echo "[spawn-browser-tester] Entry drained (spawned=$spawned)"
  else
    echo "[spawn-browser-tester] Spawn failed — entry stays queued"
    errors=$((errors + 1))
  fi
done

echo "[spawn-browser-tester] Done: spawned=$spawned errors=$errors"
