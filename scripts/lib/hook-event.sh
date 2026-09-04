#!/usr/bin/env bash
# scripts/lib/hook-event.sh — shared idempotency + crash-safety helpers for hook scripts.
#
# API:
#   hook_event_init <hook_name> <step_list_csv> [--event-id <id>] [--resume] [--query-mode]
#     Generates (or reads) event_id, creates marker file, acquires flock, checks
#     for full completion (exits 0 immediately if already done, unless --query-mode).
#
#   hook_event_has_step <step_name>
#     Returns 0 if the step is already in steps_completed, 1 if not.
#
#   hook_event_mark_step <step_name>
#     Atomically appends step to steps_completed in the marker file.
#
#   hook_event_finish
#     Moves marker to done/ directory, releases flock.
#
# After sourcing this file, call hook_event_init at the top of your hook.
# Wrap each logical step with:
#   if ! hook_event_has_step "step_name"; then
#     ... do work ...
#     hook_event_mark_step "step_name"
#   fi
# Call hook_event_finish at the very end (a trap is also installed for safety).
#
# Environment exported by hook_event_init:
#   HOOK_EVENT_ID         — the resolved event id (16-char hex or UUID4)
#   HOOK_EVENT_DIR        — .autonomous-team/hook-events
#   HOOK_EVENT_MARKER     — path to the active marker JSON
#   HOOK_EVENT_LOCK       — path to the flock target file
#   HOOK_EVENT_FD         — file descriptor holding the flock
#   HOOK_EVENT_QUERY_MODE — set to 1 when --query-mode is active (skips no-op exit on dupe)

set -uo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────

_hook_event_resolve_dir() {
  # Walk up from here to find repo root (contains .autonomous-team/)
  local dir
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "$dir/.autonomous-team" ]]; then
      echo "$dir/.autonomous-team/hook-events"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  # Fallback: use current working directory
  echo "$(pwd)/.autonomous-team/hook-events"
}

# ── ID generation ─────────────────────────────────────────────────────────────

_hook_event_generate_id() {
  local role="${HOOK_ROLE:-}"
  local discussion="${HOOK_DISCUSSION:-}"
  local pr="${HOOK_PR:-}"
  local verdict="${HOOK_VERDICT:-}"
  local minute
  minute="$(date -u +%Y%m%dT%H%M)"

  if [[ -n "$role" && -n "$verdict" ]]; then
    # Deterministic: sha256 of key fields truncated to 16 hex chars
    local raw="${role}|${discussion}|${pr}|${verdict}|${minute}"
    echo -n "$raw" | sha256sum 2>/dev/null | cut -c1-16 \
      || python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])" "$raw"
  else
    # Fallback to UUID4 and warn
    local uuid
    uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))" 2>/dev/null \
           || cat /proc/sys/kernel/random/uuid 2>/dev/null \
           || date +%s%N | sha256sum | cut -c1-16)
    echo "$uuid"
    # Warning goes to stderr; callers decide whether to route it
    echo "[hook-event] WARNING: generating UUID4 event_id — missing role/verdict context (caller=${HOOK_CALLER:-unknown}, hook=${HOOK_NAME:-unknown})" >&2
  fi
}

# ── Marker read/write ─────────────────────────────────────────────────────────

_hook_event_read_marker() {
  local path="$1"
  if [[ -f "$path" ]]; then
    cat "$path"
  else
    echo "{}"
  fi
}

_hook_event_write_marker() {
  # Atomic write: write to .tmp then mv (rename is atomic on same filesystem)
  local path="$1"
  local content="$2"
  local tmp="${path}.tmp"
  printf '%s\n' "$content" > "$tmp"
  mv "$tmp" "$path"
}

# ── Public API ────────────────────────────────────────────────────────────────

hook_event_init() {
  # Usage: hook_event_init <hook_name> <step_list_csv> [--event-id <id>] [--resume] [--query-mode]
  #
  # --query-mode: skip the "already complete → no-op exit" short-circuit.
  #   Use for read-only query hooks (e.g. pre-spawn-check.sh) that must always
  #   return output even when called multiple times within the same minute.
  #   Marker files are still written for observability.
  local hook_name="$1"
  local step_csv="$2"
  shift 2

  local supplied_event_id=""
  local resume=false
  export HOOK_EVENT_QUERY_MODE=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --event-id)   supplied_event_id="$2"; shift 2 ;;
      --resume)     resume=true; shift ;;
      --query-mode) HOOK_EVENT_QUERY_MODE=1; shift ;;
      *) shift ;;
    esac
  done

  export HOOK_NAME="$hook_name"

  # Resolve event dir — respect externally-set HOOK_EVENT_DIR (e.g. in tests)
  if [[ -z "${HOOK_EVENT_DIR:-}" ]]; then
    export HOOK_EVENT_DIR
    HOOK_EVENT_DIR="$(_hook_event_resolve_dir)"
  fi
  mkdir -p "$HOOK_EVENT_DIR/done"

  # Resolve event_id
  if [[ -n "$supplied_event_id" ]]; then
    export HOOK_EVENT_ID="$supplied_event_id"
  else
    export HOOK_EVENT_ID
    HOOK_EVENT_ID="$(_hook_event_generate_id)"
  fi

  # Print event_id as first stdout line (callers can capture it)
  echo "hook_event_id=${HOOK_EVENT_ID}"

  export HOOK_EVENT_MARKER="${HOOK_EVENT_DIR}/${HOOK_EVENT_ID}.json"
  export HOOK_EVENT_LOCK="${HOOK_EVENT_DIR}/${HOOK_EVENT_ID}.lock"

  # Grammar-agnostic containment guard: a hostile event_id (e.g. containing
  # "../") must not resolve outside HOOK_EVENT_DIR. realpath -m (not normpath)
  # so symlinks in existing path components are resolved (D#1792).
  local _hed_real
  _hed_real="$(realpath -m "$HOOK_EVENT_DIR")"
  if [[ "$(realpath -m "$HOOK_EVENT_MARKER")" != "$_hed_real"/* || "$(realpath -m "$HOOK_EVENT_LOCK")" != "$_hed_real"/* ]]; then
    echo "[hook-event] REJECTED: event id '${HOOK_EVENT_ID}' resolves outside ${HOOK_EVENT_DIR}" >&2
    exit 1
  fi

  # Length guard: an id long enough to blow NAME_MAX (255 on ext4/xfs/most
  # Linux filesystems) on a basename built from it must be rejected before
  # touch/exec/mv ever see it. Without this, ENAMETOOLONG on every one of
  # those commands still let the old code fall through to the end of the
  # function (D#2105) — 200 leaves headroom for the longest suffix this file
  # appends (".json.tmp", 9 chars) well under the 255 limit.
  local _hed_max_id_len="${HOOK_EVENT_ID_MAX_LEN:-200}"
  if (( ${#HOOK_EVENT_ID} > _hed_max_id_len )); then
    echo "[hook-event] REJECTED: event id '${HOOK_EVENT_ID}' exceeds max length ${_hed_max_id_len} (len=${#HOOK_EVENT_ID})" >&2
    exit 1
  fi

  # Acquire exclusive flock on the lock file (BEFORE checking done marker,
  # so a concurrent call that just finished moving to done is visible to us).
  # Each stage's exit status is checked explicitly and reported loudly on
  # failure — mirroring the containment guard above. Before this check, a
  # mode-000 lock file let `exec 200>` fail silently while `hook_event_init`
  # still exported HOOK_EVENT_FD=200 and returned 0, advertising a lock it
  # never held (D#2105).
  mkdir -p "$(dirname "$HOOK_EVENT_LOCK")"
  local _hed_lock_failed=""
  if ! touch "$HOOK_EVENT_LOCK"; then
    _hed_lock_failed="create lock file"
  fi
  if [[ -z "$_hed_lock_failed" ]] && ! exec 200>"$HOOK_EVENT_LOCK"; then
    _hed_lock_failed="open lock fd"
  fi
  if [[ -z "$_hed_lock_failed" ]] && ! flock -x 200; then
    _hed_lock_failed="acquire flock"
  fi
  if [[ -n "$_hed_lock_failed" ]]; then
    echo "[hook-event] INIT_FAILED: failed to ${_hed_lock_failed} for '${HOOK_EVENT_LOCK}'" >&2
    exec 200>&- 2>/dev/null || true
    exit 1
  fi
  export HOOK_EVENT_FD=200

  # Install trap to release lock + move marker to done on unexpected exit
  trap '_hook_event_trap_cleanup' EXIT INT TERM

  # Check if already completed (marker in done/) — check AFTER acquiring flock
  # so we see the state left by a concurrent invocation that just finished.
  # In --query-mode we skip the early exit: query hooks must always produce output.
  local done_marker="${HOOK_EVENT_DIR}/done/${HOOK_EVENT_ID}.json"
  if [[ -f "$done_marker" && -z "${HOOK_EVENT_QUERY_MODE:-}" ]]; then
    echo "[hook-event] Event ${HOOK_EVENT_ID} already complete — no-op exit."
    # Release flock and exit cleanly
    flock -u 200 2>/dev/null || true
    exec 200>&- 2>/dev/null || true
    trap - EXIT INT TERM 2>/dev/null || true
    exit 0
  fi

  # Parse steps
  IFS=',' read -ra _HOOK_STEPS <<< "$step_csv"
  export HOOK_STEPS_CSV="$step_csv"

  # Check active marker — if it already has all steps completed, move to done.
  # In --query-mode we skip this early exit for the same reason as above.
  if [[ -f "$HOOK_EVENT_MARKER" && -z "${HOOK_EVENT_QUERY_MODE:-}" ]]; then
    local completed_count
    completed_count=$(python3 -c "
import json,sys
try:
  d=json.load(open(sys.argv[1]))
  total=d.get('steps_total',[])
  done=set(d.get('steps_completed',[]))
  print(len([s for s in total if s in done]))
except:
  print(0)
" "$HOOK_EVENT_MARKER" 2>/dev/null || echo "0")
    local total_count="${#_HOOK_STEPS[@]}"
    if [[ "$completed_count" -ge "$total_count" && "$total_count" -gt 0 ]]; then
      echo "[hook-event] Event ${HOOK_EVENT_ID} marker shows all steps done — no-op exit."
      hook_event_finish
      exit 0
    fi
  fi

  # Build steps_completed from existing marker (for resume) or start fresh
  local existing_completed="[]"
  if [[ "$resume" == "true" && -f "$HOOK_EVENT_MARKER" ]]; then
    existing_completed=$(python3 -c "
import json,sys
try:
  d=json.load(open(sys.argv[1]))
  print(json.dumps(d.get('steps_completed',[])))
except:
  print('[]')
" "$HOOK_EVENT_MARKER" 2>/dev/null || echo "[]")
  fi

  # Write initial marker (atomic)
  local steps_total_json
  steps_total_json=$(python3 -c "
import json,sys
steps=[s.strip() for s in sys.argv[1].split(',') if s.strip()]
print(json.dumps(steps))
" "$step_csv")

  # Gather inputs from environment variables set by the hook caller
  local inputs_json
  inputs_json=$(python3 -c "
import json,os
d={}
for k in ['role','discussion','pr','verdict']:
  v=os.environ.get('HOOK_'+k.upper(),'')
  if v: d[k]=int(v) if k in ('discussion','pr') and v.isdigit() else v
print(json.dumps(d))
" 2>/dev/null || echo "{}")

  local now
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  # Preserve started_at if resuming
  local started_at="$now"
  if [[ "$resume" == "true" && -f "$HOOK_EVENT_MARKER" ]]; then
    started_at=$(python3 -c "
import json,sys
try:
  d=json.load(open(sys.argv[1]))
  print(d.get('started_at',sys.argv[2]))
except:
  print(sys.argv[2])
" "$HOOK_EVENT_MARKER" "$now" 2>/dev/null || echo "$now")
  fi

  local marker_json
  marker_json=$(python3 -c "
import json,sys
d={
  'event_id': sys.argv[1],
  'hook': sys.argv[2],
  'started_at': sys.argv[3],
  'inputs': json.loads(sys.argv[4]),
  'steps_total': json.loads(sys.argv[5]),
  'steps_completed': json.loads(sys.argv[6]),
  'last_update': sys.argv[3],
}
print(json.dumps(d, indent=2))
" "$HOOK_EVENT_ID" "$hook_name" "$started_at" "$inputs_json" "$steps_total_json" "$existing_completed")

  _hook_event_write_marker "$HOOK_EVENT_MARKER" "$marker_json"
}

hook_event_has_step() {
  local step="$1"
  if [[ ! -f "$HOOK_EVENT_MARKER" ]]; then
    return 1
  fi
  # Use a subshell to capture the check result without stderr suppression
  # (2>/dev/null can interfere with python3 when fd 200 is held for flock)
  local _result
  _result=$(python3 -c "
import json,sys
try:
  d=json.load(open(sys.argv[1]))
  done=d.get('steps_completed',[])
  print(0 if sys.argv[2] in done else 1)
except:
  print(1)
" "$HOOK_EVENT_MARKER" "$step" 2>/dev/null)
  return "${_result:-1}"
}

hook_event_mark_step() {
  local step="$1"
  if [[ ! -f "$HOOK_EVENT_MARKER" ]]; then
    echo "[hook-event] WARNING: marker not found when marking step '$step'" >&2
    return 0
  fi
  local now
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local updated
  updated=$(python3 -c "
import json,sys
path,step,now=sys.argv[1],sys.argv[2],sys.argv[3]
d=json.load(open(path))
done=d.get('steps_completed',[])
if step not in done:
  done.append(step)
d['steps_completed']=done
d['last_update']=now
print(json.dumps(d, indent=2))
" "$HOOK_EVENT_MARKER" "$step" "$now")
  _hook_event_write_marker "$HOOK_EVENT_MARKER" "$updated"
}

hook_event_finish() {
  if [[ -z "${HOOK_EVENT_MARKER:-}" ]]; then
    return 0
  fi
  # Move marker to done/
  local done_dir="${HOOK_EVENT_DIR}/done"
  mkdir -p "$done_dir"
  if [[ -f "$HOOK_EVENT_MARKER" ]]; then
    mv "$HOOK_EVENT_MARKER" "${done_dir}/${HOOK_EVENT_ID}.json" 2>/dev/null || true
  fi
  # Remove lock file
  rm -f "$HOOK_EVENT_LOCK" 2>/dev/null || true
  # Release flock
  if [[ -n "${HOOK_EVENT_FD:-}" ]]; then
    flock -u "${HOOK_EVENT_FD}" 2>/dev/null || true
    eval "exec ${HOOK_EVENT_FD}>&-" 2>/dev/null || true
    unset HOOK_EVENT_FD
  fi
  # Disable trap to avoid double-cleanup
  trap - EXIT INT TERM 2>/dev/null || true
  echo "[hook-event] Event ${HOOK_EVENT_ID} complete — marker moved to done/."
}

_hook_event_trap_cleanup() {
  # Called on unexpected exit — don't move to done/ since we may be partial;
  # just release the flock so the next invocation can proceed.
  if [[ -n "${HOOK_EVENT_FD:-}" ]]; then
    flock -u "${HOOK_EVENT_FD}" 2>/dev/null || true
    eval "exec ${HOOK_EVENT_FD}>&-" 2>/dev/null || true
    unset HOOK_EVENT_FD
  fi
}
