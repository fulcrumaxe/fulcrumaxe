#!/usr/bin/env bash
# pre-spawn-check.sh — run before EVERY agent spawn to enforce coordination discipline.
#
# Usage:
#   bash scripts/pre-spawn-check.sh --role <role> --discussion <N> [--event-id <id>] [--resume] [--dry-run]
#
# Outputs JSON with all context gathered. Exits 1 if spawn should be blocked (circuit
# breaker tripped or budget exceeded). Exits 0 with JSON payload if spawn is allowed.
#
# Circuit breaker and budget are HARD blocks. All other subsystem failures are
# non-fatal warnings included in the JSON output.
#
# --dry-run: compute and print the full JSON output but do NOT write to blackboard,
#   circuit breaker, KPI, budget, agent feed, or team-log. Useful for inspection
#   and testing without side effects.
#
# Idempotent: pass the same --event-id twice and the second call is a no-op.
# Steps: agent_feed → budget_check → circuit_breaker_check → context_load → team_log

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/state-dir.sh
source "$SCRIPT_DIR/lib/state-dir.sh" || true

ROLE=""
DISCUSSION=""
ISOLATION=""
EVENT_ID_ARG=""
RESUME_FLAG=""
DRY_RUN=""
SUBAGENT_TYPE=""
NO_REGISTER=""
OPERATION_CLASS_ARG=""
TOUCHPOINTS_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)              ROLE="$2";              shift 2 ;;
    --discussion)        DISCUSSION="$2";        shift 2 ;;
    --isolation)         ISOLATION="$2";         shift 2 ;;
    --event-id)          EVENT_ID_ARG="$2";      shift 2 ;;
    --resume)            RESUME_FLAG="--resume"; shift ;;
    --dry-run)           DRY_RUN="1";            shift ;;
    --subagent-type)     SUBAGENT_TYPE="$2";     shift 2 ;;
    --no-register|--dry-run-fleet) NO_REGISTER="1"; shift ;;
    --operation-class)   OPERATION_CLASS_ARG="$2"; shift 2 ;;
    --touchpoints)       TOUCHPOINTS_ARG="$2";    shift 2 ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 --role <role> --discussion <N> [--isolation worktree] [--dry-run] [--no-register] [--operation-class <class>] [--touchpoints <comma-separated-paths>]" >&2
      exit 1
      ;;
  esac
done

# Defensive check: parent repo must be on main before any spawn proceeds.
# A leaked executor branch on the parent repo causes worktree contamination downstream.
# Instead of refusing (exit 3), auto-recover to main and log the event so the
# contamination rate is visible without requiring manual operator intervention.
#
# Guard: skip if running from inside a linked worktree. Inside a worktree the branch
# is intentionally non-main; running recovery here would clobber WIP.
# Two signals, either one is sufficient:
#   1. $WORKTREE_ID env var — a dead fallback; nothing in the tree sets it
#      outside tests (verified 2026-08-17: unset inside a live worktree agent)
#   2. git-dir != git-common-dir — canonical linked-worktree test, and the
#      one that actually fires
_GIT_DIR=$(git -C "$REPO_ROOT" rev-parse --git-dir 2>/dev/null || true)
_GIT_COMMON_DIR=$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)
_IN_LINKED_WORKTREE=false
if [[ -n "${WORKTREE_ID:-}" ]] || [[ -n "$_GIT_DIR" && -n "$_GIT_COMMON_DIR" && "$_GIT_DIR" != "$_GIT_COMMON_DIR" ]]; then
  _IN_LINKED_WORKTREE=true
fi

if [[ "$DRY_RUN" != "1" && "$_IN_LINKED_WORKTREE" == "false" ]]; then
  # Detect the project's actual default branch (not assumed "main").
  # 1) remote HEAD symref, 2) project.json default_branch key, 3) fall back to "main".
  _DEFAULT_BRANCH=$(git -C "$REPO_ROOT" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || true)
  if [[ -z "$_DEFAULT_BRANCH" ]]; then
    _PROJECT_JSON="$REPO_ROOT/.autonomous-team/project.json"
    _DEFAULT_BRANCH=$(python3 -c "
import json, sys
try:
    d = json.load(open('$_PROJECT_JSON'))
    print(d.get('default_branch', '') or '')
except Exception:
    print('')
" 2>/dev/null || true)
  fi
  if [[ -z "$_DEFAULT_BRANCH" ]]; then
    _DEFAULT_BRANCH="main"
  fi

  PARENT_BRANCH=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "")
  # Only fire contamination recovery when we are actually on a non-default branch
  # (i.e. drift detected). Firing on every invocation was the BUG — it would reset
  # HEAD even when nothing was contaminated.
  if [[ -n "$PARENT_BRANCH" && "$PARENT_BRANCH" != "$_DEFAULT_BRANCH" ]]; then
    echo "[pre-spawn-check] Parent on '$PARENT_BRANCH', auto-resetting to $_DEFAULT_BRANCH (contamination recovery)" >&2
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] pre-spawn-check: auto-recovered parent from contaminated branch '$PARENT_BRANCH' → $_DEFAULT_BRANCH" \
      2>/dev/null || true
    git -C "$REPO_ROOT" symbolic-ref HEAD "refs/heads/$_DEFAULT_BRANCH" 2>/dev/null || true
    git -C "$REPO_ROOT" fetch origin "$_DEFAULT_BRANCH" --quiet 2>/dev/null || true
    git -C "$REPO_ROOT" reset --hard "origin/$_DEFAULT_BRANCH" 2>/dev/null || true
  fi
fi

# Defensive check: general-purpose subagent_type is forbidden (see CLAUDE.md HARD STOPS).
# This catches the rare case where the spawn parameter is visible at the shell layer.
if [[ "$SUBAGENT_TYPE" == "general-purpose" ]]; then
  echo "ERROR: subagent_type=general-purpose is forbidden — use a named role (see CLAUDE.md HARD STOPS)" >&2
  echo "Allowed types: executor, code-reviewer, security-reviewer, project-manager," >&2
  echo "  acceptance-tester, browser-tester, mission-analyst, technical-architect, product-owner," >&2
  echo "  cost-analyst, performance-expert, security-expert, run-analyst, feedback-scanner," >&2
  echo "  quality-sweep, visual-verifier" >&2
  exit 2
fi

if [[ -z "$ROLE" ]]; then
  echo "Error: --role is required" >&2
  exit 1
fi

# ── Role allowlist gate (D#1622 Batch C1) ────────────────────────────────────
# Consult backend/role_allowlist.py::is_role_active against this project's own
# config.json `active_roles` allowlist. Absent/empty allowlist = allow all
# (backward compatible -- this repo's own config.json has no active_roles key
# today). ROLE_ALLOWLIST_OVERRIDE=1 bypasses the block.
if [[ "${ROLE_ALLOWLIST_OVERRIDE:-}" != "1" ]]; then
  _ROLE_ACTIVE=$(python3 "$REPO_ROOT/backend/role_allowlist.py" check "$ROLE" "$REPO_ROOT/.autonomous-team/config.json" 2>/dev/null || echo "true")
  if [[ "$_ROLE_ACTIVE" == "false" ]]; then
    echo "ERROR: role '$ROLE' is not in this project's active_roles allowlist. Set ROLE_ALLOWLIST_OVERRIDE=1 to bypass." >&2
    if [[ "$DRY_RUN" != "1" ]]; then
      bash "$SCRIPT_DIR/agent-feed-append.sh" \
        --role "$ROLE" \
        --event-type "spawn_blocked" \
        --reason "role_allowlist" \
        --message "blocked $ROLE: not in active_roles allowlist" \
        $([ -n "$DISCUSSION" ] && echo "--discussion $DISCUSSION") \
        2>/dev/null || true
    fi
    exit 1
  fi
fi

# ── Hourly JSONL sweep (background, non-blocking) ─────────────────────────
# Run at most once per hour. Tracks last-run via mtime of .last-jsonl-sweep.
# Skipped in dry-run mode and when running inside a worktree (only the parent
# pre-spawn-check fires this, not executor-side pre-spawn-checks).
if [[ "$DRY_RUN" != "1" && "$_IN_LINKED_WORKTREE" == "false" ]]; then
  _SWEEP_STAMP="$REPO_ROOT/.autonomous-team/.last-jsonl-sweep"
  _SWEEP_INTERVAL=3600  # 1 hour in seconds
  _RUN_SWEEP=false

  if [[ ! -f "$_SWEEP_STAMP" ]]; then
    _RUN_SWEEP=true
  else
    _STAMP_AGE=$(python3 -c "
import os, time
try:
    print(int(time.time() - os.path.getmtime('$_SWEEP_STAMP')))
except Exception:
    print(99999)
" 2>/dev/null || echo "99999")
    if [[ "$_STAMP_AGE" -ge "$_SWEEP_INTERVAL" ]] 2>/dev/null; then
      _RUN_SWEEP=true
    fi
  fi

  if [[ "$_RUN_SWEEP" == "true" ]]; then
    echo "[pre-spawn-check] running hourly JSONL sweep in background..." >&2
    bash "$SCRIPT_DIR/sweep-jsonl.sh" >> "$REPO_ROOT/.autonomous-team/.last-jsonl-sweep.log" 2>&1 &
    disown
    # Update mtime immediately so concurrent spawns don't double-fire
    touch "$_SWEEP_STAMP" 2>/dev/null || true
  fi
fi

# Export context for hook-event.sh ID generation
export HOOK_ROLE="$ROLE"
export HOOK_DISCUSSION="${DISCUSSION:-}"
export HOOK_PR=""
export HOOK_VERDICT="spawn-check"
export HOOK_CALLER="pre-spawn-check"

# Source shared idempotency helpers
# shellcheck source=scripts/lib/hook-event.sh
source "$SCRIPT_DIR/lib/hook-event.sh"

# In dry-run mode skip all idempotency/event tracking (no side effects)
if [[ "$DRY_RUN" != "1" ]]; then
  # Initialize event (read-only steps still recorded for replay determinism)
  INIT_ARGS=()
  [[ -n "$EVENT_ID_ARG" ]] && INIT_ARGS+=(--event-id "$EVENT_ID_ARG")
  [[ -n "$RESUME_FLAG" ]]  && INIT_ARGS+=(--resume)

  hook_event_init "pre-spawn-check" \
    "agent_feed,budget_check,circuit_breaker_check,context_load,team_log" \
    "${INIT_ARGS[@]:-}" \
    --query-mode
fi

WARNINGS=()
PROJECT_CONTEXT=""
AGENT_MEMORY=""
GATE_CONTEXT=""
CIRCUIT_FAILURES=0
BUDGET_REMAINING=0
IN_FLIGHT=0

# ── Helper: emit spawn_blocked event to agent feed ────────────────────────────
# Call before every hard-block exit. Emission failure is non-fatal.
# Args: reason message [details_json]
emit_spawn_block() {
  local reason="$1"
  local message="$2"
  local details_json="$3"
  [[ -z "$details_json" ]] && details_json="{}"
  [[ "$DRY_RUN" == "1" ]] && return 0
  local disc_args=()
  [[ -n "$DISCUSSION" ]] && disc_args=(--discussion "$DISCUSSION")
  bash "$SCRIPT_DIR/agent-feed-append.sh" \
    --role "$ROLE" \
    --event-type "spawn_blocked" \
    --reason "$reason" \
    --message "blocked $ROLE: $message" \
    "${disc_args[@]}" \
    --details "$details_json" \
    2>/dev/null || true
}

# ── 0a. PM dedup check — must run BEFORE logging the spawn_attempt ────────────
# If a spawn_attempt for the same role+discussion was recorded in the last 120s, block.
# We check BEFORE writing to the feed so the just-written entry doesn't count against
# itself, which was the original self-blocking bug (D#844).
if [[ "$ROLE" == "project-manager" && -n "$DISCUSSION" && "$DRY_RUN" != "1" ]]; then
  PM_DEDUP_WINDOW=120
  FEED_FILE="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
  RECENT_PM=$(python3 - <<PYEOF 2>/dev/null
import json, time, pathlib
feed = pathlib.Path("$FEED_FILE")
cutoff = time.time() - $PM_DEDUP_WINDOW
count = 0
if feed.exists():
    for line in feed.read_text().splitlines():
        try:
            d = json.loads(line)
            if (d.get("event_type") == "spawn_attempt"
                    and d.get("role") == "project-manager"
                    and str(d.get("discussion", "")) == "$DISCUSSION"):
                import datetime
                ts = d.get("ts", "")
                t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if t >= cutoff:
                    count += 1
        except Exception:
            pass
print(count)
PYEOF
)
  RECENT_PM="${RECENT_PM:-0}"
  if [[ "$RECENT_PM" -ge 1 ]]; then
    echo "ERROR: pm_dedup — project-manager spawn for D#$DISCUSSION blocked (duplicate within ${PM_DEDUP_WINDOW}s)." >&2
    exit 1
  fi
fi

# ── 0. Agent feed — spawn_attempt event (JSONL only, no team-log — too noisy) ─
if [[ "$DRY_RUN" != "1" ]] && ! hook_event_has_step "agent_feed"; then
  FEED_MSG="spawn_attempt: $ROLE"
  [[ -n "$DISCUSSION" ]] && FEED_MSG="$FEED_MSG D#$DISCUSSION"
  FEED_MSG="${FEED_MSG:0:280}"
  bash "$SCRIPT_DIR/agent-feed-append.sh" \
    --role "$ROLE" \
    --event-type "spawn_attempt" \
    --message "$FEED_MSG" \
    $([ -n "$DISCUSSION" ] && echo "--discussion $DISCUSSION") \
    2>/dev/null || true  # completely non-fatal
  hook_event_mark_step "agent_feed"
fi

# ── 1. Budget check (hard block if exceeded) ─────────────────────────────────
# In dry-run mode: read budget for JSON output but never block or write
#
# D#2063: `budget.py check` PRINTS its JSON verdict to stdout AND separately
# signals exhaustion via exit 1 -- exit 1 means "the read succeeded and the
# budget is exhausted", not "the read failed". The old `$(cmd || echo
# <fallback>)` form treated that exit 1 as a command-substitution failure, so
# on a real exhausted budget the fallback JSON got appended *after* the real
# JSON that had already printed. Two concatenated JSON objects don't parse,
# so the next line's own `|| echo "true"` fired and silently approved the
# spawn at the exact moment the budget said no.
#
# Capture stdout and the exit code separately so two different situations
# are told apart instead of both collapsing to "healthy":
#   1. budget.py ran and produced a parseable answer (allowed: true or
#      false) -- a real signal. Honor it, including fail-closed on false.
#   2. budget.py produced nothing parseable at all (crash, LockTimeout,
#      missing/bad interpreter) -- an unknown, not an exhausted budget.
#      Failing closed here would block *every* spawn on the host the
#      instant budget.py breaks, with no way to spawn the agent that would
#      fix it -- the same shape of mistake PR #2093 made with the
#      worktree-cap counter (a correct, reviewed fix that would have halted
#      every worktree spawn on this host). So this stays open, but LOUD:
#      a WARNING that shows up, not a silent fallback like the bug this
#      fixes.
_budget_err=$(mktemp)
BUDGET_JSON=$(python3 "$REPO_ROOT/backend/budget.py" check 2>"$_budget_err")
_budget_rc=$?
BUDGET_ALLOWED=$(echo "$BUDGET_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('allowed','true')).lower())" 2>/dev/null)
_budget_parse_rc=$?
if [[ $_budget_parse_rc -ne 0 || -z "$BUDGET_JSON" ]]; then
  WARNINGS+=("budget.py check could not be read (exit $_budget_rc): $(tail -1 "$_budget_err")")
  echo "WARNING: budget.py check unreadable (exit $_budget_rc) — spawn allowed through, budget state unknown." >&2
  BUDGET_ALLOWED="true"
  BUDGET_REMAINING=0
else
  BUDGET_REMAINING=$(echo "$BUDGET_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('remaining',0))" 2>/dev/null || echo "0")
fi
rm -f "$_budget_err"

if [[ "$DRY_RUN" != "1" ]]; then
  if ! hook_event_has_step "budget_check"; then
    if [[ "$BUDGET_ALLOWED" == "false" ]]; then
      emit_spawn_block "budget_exceeded" "budget exhausted" "{\"budget_remaining\":${BUDGET_REMAINING}}"
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] team-lead: BUDGET EXCEEDED — skipping spawn of $ROLE" \
        2>/dev/null || true
      echo "ERROR: budget exceeded. Spawn of $ROLE blocked." >&2
      exit 1
    fi
    hook_event_mark_step "budget_check"
  fi
fi

# ── 1.2. Per-role token_cap check (hard block if remaining < per-role cap) ────
# policies.<role>.token_cap sets a tighter per-role ceiling than the global default.
# If the budget remaining is less than this cap, reject the spawn to prevent the
# role from starting a run it cannot finish.
# Dry-run: reads cap but does not block or write team-log.
_ROLE_SLUG="${ROLE//-/_}"
ROLE_TOKEN_CAP=$(python3 "$REPO_ROOT/backend/control_plane.py" get "policies.${ROLE}.token_cap" 2>/dev/null | tr -d '"' || echo "")
# Normalize: treat "null" / empty / non-numeric as "no cap set"
if [[ -n "$ROLE_TOKEN_CAP" && "$ROLE_TOKEN_CAP" != "null" ]] && python3 -c "int('$ROLE_TOKEN_CAP')" 2>/dev/null; then
  if [[ "$DRY_RUN" != "1" ]]; then
    BUDGET_REMAINING_INT=$(python3 -c "print(int(float('${BUDGET_REMAINING}')))" 2>/dev/null || echo "$BUDGET_REMAINING")
    ROLE_CAP_EXCEEDED=$(python3 -c "print('true' if int('${BUDGET_REMAINING_INT}') < int('${ROLE_TOKEN_CAP}') else 'false')" 2>/dev/null || echo "false")
    if [[ "$ROLE_CAP_EXCEEDED" == "true" ]]; then
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] team-lead: per-role token_cap block — $ROLE needs ${ROLE_TOKEN_CAP} tokens but only ${BUDGET_REMAINING_INT} remaining" \
        2>/dev/null || true
      echo "ERROR: per-role token_cap ($ROLE_TOKEN_CAP) for $ROLE exceeds budget remaining ($BUDGET_REMAINING_INT). Spawn blocked." >&2
      exit 1
    fi
  fi
fi

# ── 1.5. Subscription throttle check (soft block — new spawns deferred) ─────
# Only active when gates.subscription_throttle == true. When off, skipped entirely
# so API-mode users see no behavior change. Dry-run skips the block and team-log write.
SUB_GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.subscription_throttle 2>/dev/null | tr -d '"' || echo "false")
if [[ "$SUB_GATE" == "true" && "$DRY_RUN" != "1" ]]; then
  SUB_JSON=$(python3 "$REPO_ROOT/backend/subscription_usage.py" --json 2>/dev/null || echo '{"percent":0,"plan":"unknown"}')
  SUB_PCT=$(echo "$SUB_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('percent',0))" 2>/dev/null || echo "0")
  TARGET_PCT=$(python3 "$REPO_ROOT/backend/control_plane.py" get policies.subscription.target_percent 2>/dev/null | tr -d '"' || echo "80")
  TARGET_PCT="${TARGET_PCT:-80}"
  # Compare as floats
  OVER_QUOTA=$(python3 -c "import sys; print('true' if float('${SUB_PCT}') >= float('${TARGET_PCT}') else 'false')" 2>/dev/null || echo "false")
  if [[ "$OVER_QUOTA" == "true" ]]; then
    emit_spawn_block "subscription_throttled" "subscription usage ${SUB_PCT}% >= target ${TARGET_PCT}%" "{\"percent\":${SUB_PCT},\"target_percent\":${TARGET_PCT}}"
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] team-lead: SUBSCRIPTION THROTTLED — usage ${SUB_PCT}% >= target ${TARGET_PCT}%, deferring spawn of $ROLE" \
      2>/dev/null || true
    python3 -c "
import json, sys
print(json.dumps({
    'allowed': False,
    'reason': 'subscription_throttled',
    'percent': float(sys.argv[1]),
    'target_percent': float(sys.argv[2]),
    'role': sys.argv[3],
}))
" "$SUB_PCT" "$TARGET_PCT" "$ROLE"
    exit 1
  fi
fi

# ── 2. Circuit breaker check (hard block if >= 3 failures) ──────────────────
# In dry-run mode: read status but do not block or write team-log
if [[ -n "$DISCUSSION" ]]; then
  CIRCUIT_FAILURES=$(python3 "$REPO_ROOT/backend/circuit_breaker.py" status "$DISCUSSION" 2>/dev/null || echo "0")
else
  CIRCUIT_FAILURES=0
fi
if [[ "$DRY_RUN" != "1" ]]; then
  if ! hook_event_has_step "circuit_breaker_check"; then
    if [[ -n "$DISCUSSION" && "$CIRCUIT_FAILURES" -ge 3 ]] 2>/dev/null; then
      emit_spawn_block "circuit_breaker_open" "circuit-breaker open ($CIRCUIT_FAILURES failures)" "{\"circuit_failures\":${CIRCUIT_FAILURES}}"
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] team-lead: Discussion #$DISCUSSION circuit-breaker open ($CIRCUIT_FAILURES failures) -- skipping spawn of $ROLE, needs manual review" \
        2>/dev/null || true
      echo "ERROR: circuit-breaker open for Discussion #$DISCUSSION ($CIRCUIT_FAILURES failures >= 3 threshold). Spawn blocked." >&2
      exit 1
    fi
    hook_event_mark_step "circuit_breaker_check"
  fi
fi

# ── 2.5. Base-branch health check (optional, per project.json) ───────────────
# Only runs for roles that compile or run tests. Skipped when project.json is
# absent (this repo's own use) or lacks the health_check field.
#
# Fields read from .autonomous-team/project.json:
#   health_check        — shell command to validate the base branch
#   health_check_blocks — bool (default false); when true, fail → exit 1
#
# Cache: blackboard key health-check/<sha256(cmd+baseSHA)>, TTL 300s.
# Cache hit  → log cached result, skip re-run.
# Cache miss → run command, store result.

_HEALTH_CHECK_ROLES=("executor" "acceptance-tester")
_ROLE_NEEDS_HEALTH_CHECK=false
for _r in "${_HEALTH_CHECK_ROLES[@]}"; do
  if [[ "$ROLE" == "$_r" ]]; then
    _ROLE_NEEDS_HEALTH_CHECK=true
    break
  fi
done

if [[ "$_ROLE_NEEDS_HEALTH_CHECK" == "true" ]]; then
  _PROJECT_JSON_PATH="$REPO_ROOT/.autonomous-team/project.json"
  _HEALTH_CMD=""
  _HEALTH_BLOCKS="false"

  if [[ -f "$_PROJECT_JSON_PATH" ]]; then
    _HEALTH_CMD=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('health_check') or '')
except Exception:
    print('')
" "$_PROJECT_JSON_PATH" 2>/dev/null || echo "")

    _HEALTH_BLOCKS=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print('true' if d.get('health_check_blocks', False) else 'false')
except Exception:
    print('false')
" "$_PROJECT_JSON_PATH" 2>/dev/null || echo "false")
  fi

  if [[ -n "$_HEALTH_CMD" ]]; then
    # Build cache key: SHA256 of command + current base branch HEAD SHA
    _BASE_SHA=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "unknown")
    _CACHE_KEY=$(printf '%s\n%s' "$_HEALTH_CMD" "$_BASE_SHA" \
      | python3 -c "import sys, hashlib; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])" 2>/dev/null || echo "nocache")
    _BB_KEY="health-check/$_CACHE_KEY"
    _HEALTH_TTL=300

    # Check cache via blackboard
    _CACHED_RESULT=$(python3 -c "
import json, sys, time
sys.path.insert(0, sys.argv[1])
try:
    from backend.blackboard import get_blackboard
    bb = get_blackboard()
    entry = bb.read_entry(sys.argv[2])
    if entry is None:
        print('miss')
    else:
        import datetime
        ts = entry.get('updated_at', '')
        if ts:
            t = datetime.datetime.fromisoformat(ts).timestamp()
            age = time.time() - t
            ttl = int(sys.argv[3])
            if age < ttl:
                v = entry.get('value', {})
                remaining = ttl - int(age)
                print(json.dumps({'hit': True, 'remaining': remaining,
                                   'exit_code': v.get('exit_code', 0),
                                   'result': v.get('result', 'pass')}))
            else:
                print('miss')
        else:
            print('miss')
except Exception as e:
    print('miss')
" "$REPO_ROOT" "$_BB_KEY" "$_HEALTH_TTL" 2>/dev/null || echo "miss")

    _RUN_HEALTH_CHECK=true
    _HEALTH_CACHED_EXIT=0
    _HEALTH_CACHED_RESULT="pass"

    if [[ "$_CACHED_RESULT" != "miss" ]]; then
      _HIT=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print('true' if d.get('hit') else 'false')" "$_CACHED_RESULT" 2>/dev/null || echo "false")
      if [[ "$_HIT" == "true" ]]; then
        _HEALTH_CACHED_EXIT=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('exit_code',0))" "$_CACHED_RESULT" 2>/dev/null || echo "0")
        _HEALTH_CACHED_RESULT=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('result','pass'))" "$_CACHED_RESULT" 2>/dev/null || echo "pass")
        _REMAINING=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('remaining',0))" "$_CACHED_RESULT" 2>/dev/null || echo "0")
        echo "[health-check] cached $_HEALTH_CACHED_RESULT (TTL ${_REMAINING}s)" >&2
        _RUN_HEALTH_CHECK=false

        if [[ "$_HEALTH_CACHED_EXIT" -ne 0 ]]; then
          echo "[HEALTH-FAIL] $_HEALTH_CMD exited $_HEALTH_CACHED_EXIT (cached)" >&2
          if [[ "$_HEALTH_BLOCKS" == "true" ]]; then
            echo "blocked_reason=health_check_failed" >&2
            exit 1
          fi
        fi
      fi
    fi

    if [[ "$_RUN_HEALTH_CHECK" == "true" ]]; then
      # Run from repo root; embed exit code in output stream to survive the pipe
      _HEALTH_OUTPUT=$(cd "$REPO_ROOT" && { eval "$_HEALTH_CMD" 2>&1; echo "EXIT:$?"; } | tail -21)
      _HEALTH_EXIT=$(printf '%s' "$_HEALTH_OUTPUT" | grep '^EXIT:' | sed 's/EXIT://' || echo "0")
      _HEALTH_OUTPUT=$(printf '%s' "$_HEALTH_OUTPUT" | grep -v '^EXIT:' | tail -20)

      _HEALTH_RESULT="pass"
      [[ "$_HEALTH_EXIT" -ne 0 ]] && _HEALTH_RESULT="fail"

      # Write to cache (best-effort; DRY_RUN skips)
      if [[ "$DRY_RUN" != "1" ]]; then
        python3 -c "
import json, sys
sys.path.insert(0, sys.argv[1])
try:
    from backend.blackboard import get_blackboard
    bb = get_blackboard()
    bb.write(sys.argv[2], {'exit_code': int(sys.argv[3]), 'result': sys.argv[4],
                            'command': sys.argv[5]}, updated_by='pre-spawn-check')
except Exception:
    pass
" "$REPO_ROOT" "$_BB_KEY" "$_HEALTH_EXIT" "$_HEALTH_RESULT" "$_HEALTH_CMD" 2>/dev/null || true
      fi

      if [[ "$_HEALTH_EXIT" -ne 0 ]]; then
        echo "[HEALTH-FAIL] $_HEALTH_CMD exited $_HEALTH_EXIT" >&2
        printf '%s\n' "$_HEALTH_OUTPUT" >&2
        if [[ "$_HEALTH_BLOCKS" == "true" ]]; then
          echo "blocked_reason=health_check_failed" >&2
          exit 1
        fi
      fi
    fi
  fi
fi

# ── 2.6. Dial check — refuse spawn if operation class dial is too low ─────────
# Derives the operation class from what the spawn will touch (unless
# --operation-class override was given, which always wins — an honest caller
# should not be second-guessed). See backend/dial_operation_class.py for the
# derivation rule and its rationale.
#
# Every class except sandbox.modify is checked at requested_level=2
# (propose-confirm, the minimum for autonomous agent spawning). A deny exits
# non-zero so that spawn-agent.sh can surface a clean "dial_denied" block to
# Team Lead. This part is unchanged.
#
# sandbox.modify is a deliberate exception (D#1805 round 2, security review).
# Its ceiling is hardcoded to 1 (backend/dial_registry.py _CEILINGS) and can
# never be raised — no operator, allowlisted source, or config edit reaches
# it. Checking it at requested_level=2 like every other class would make the
# deny permanent for the one caller who is honest about touching hooks/ via
# --touchpoints, while every spawn that omits --touchpoints (nothing in this
# repo auto-populates it today) sails through unaffected. That refuses only
# routine, already-reviewed hooks/ work (see PR #2057) and creates an
# incentive to stop declaring --touchpoints precisely where the file-scope
# conflict gate that flag also drives needs them most — over-blocking, which
# CLAUDE.md ranks worse than the gap this dial is meant to close. A hard
# deny here needs auto-derived touchpoints plus a real approval mechanism
# first; that is a separate, larger Discussion, not this PR.
#
# So sandbox.modify is checked at requested_level=1 instead (satisfied by
# today's registry state), and when it fires the spawn PROCEEDS but the
# firing is recorded: a loud stderr warning plus an audit row
# (kind: dial_sandbox_modify_fired) in <STATE_DIR>/audit.jsonl. That is what
# makes this dial actually fire, and be seen firing, for the first time —
# without making hooks/ maintenance require a human.
#
# Non-fatal when dial_registry.py / dial_operation_class.py is unavailable
# (import error / state dir missing) — the spawn is allowed with a warning so
# existing deployments without the dial file are not broken.
_DIAL_CLASS=""
if [[ -n "$OPERATION_CLASS_ARG" ]]; then
  _DIAL_CLASS="$OPERATION_CLASS_ARG"
else
  _DIAL_CLASS=$(python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
try:
    from backend.dial_operation_class import derive_class
    print(derive_class(sys.argv[2], sys.argv[3]))
except Exception:
    print('agent.spawn')
" "$REPO_ROOT" "$ROLE" "$TOUCHPOINTS_ARG" 2>/dev/null || echo "agent.spawn")
fi

_DIAL_ALLOWED="true"
_DIAL_REASON=""
# Populated only when a class actually fires and is allowed under the
# warn-and-audit exception below (currently just sandbox.modify). Left as
# the JSON literal 'null' otherwise, and the final JSON builder omits the
# 'dial_fired' key entirely in that case — a firing indicator present on
# every spawn is noise that would get filtered, which defeats the point.
_DIAL_FIRED_JSON='null'

if [[ -n "$_DIAL_CLASS" ]]; then
  _DIAL_REQUESTED_LEVEL=2
  [[ "$_DIAL_CLASS" == "sandbox.modify" ]] && _DIAL_REQUESTED_LEVEL=1

  _DIAL_RESULT=$(python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
try:
    from backend.dial_registry import check
    allowed, reason = check(sys.argv[2], requested_level=int(sys.argv[3]))
    import json
    print(json.dumps({'allowed': allowed, 'reason': reason}))
except Exception as e:
    import json
    print(json.dumps({'allowed': True, 'reason': 'dial_registry unavailable: ' + str(e)}))
" "$REPO_ROOT" "$_DIAL_CLASS" "$_DIAL_REQUESTED_LEVEL" 2>/dev/null || echo '{"allowed":true,"reason":"dial_registry import failed"}')

  _DIAL_ALLOWED=$(python3 -c "import sys,json,os; d=json.loads(sys.argv[1]); print(str(d.get('allowed',True)).lower())" "$_DIAL_RESULT" 2>/dev/null || echo "true")
  _DIAL_REASON=$(python3 -c "import sys,json,os; d=json.loads(sys.argv[1]); print(d.get('reason',''))" "$_DIAL_RESULT" 2>/dev/null || echo "")

  if [[ "$_DIAL_ALLOWED" == "false" ]]; then
    echo "blocked_reason=dial_denied ${_DIAL_CLASS} ${_DIAL_REASON}" >&2
    echo "ERROR: dial check denied spawn of $ROLE (class=${_DIAL_CLASS}): ${_DIAL_REASON}" >&2
    if [[ "$DRY_RUN" != "1" ]]; then
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] team-lead: dial_denied — $ROLE (class=${_DIAL_CLASS}): ${_DIAL_REASON}" \
        2>/dev/null || true
    fi
    exit 1
  elif [[ "$_DIAL_CLASS" == "sandbox.modify" ]]; then
    echo "WARNING: sandbox.modify dial fired for spawn of $ROLE (touchpoints=${TOUCHPOINTS_ARG:-none}) — allowed at requested_level=1 because ceiling=1 makes level=2 unraisable (D#1805). Recording audit row kind=dial_sandbox_modify_fired." >&2

    # Audit write: no bare `except: pass` here on purpose (round 3, security
    # review) — stderr is the ONLY channel that reaches a human when the JSON
    # below can't be inspected, and the real production caller
    # (spawn-agent.sh) discards this script's stderr entirely. A failure that
    # produces neither a row nor a message is functionally the same as never
    # firing at all. `|| true` still lets the spawn proceed regardless —
    # failing to log an already-allowed action must not turn into a block —
    # but silence and success no longer look identical.
    #
    # _AUDIT_WRITTEN and _DIAL_IS_DRY_RUN are kept as two separate booleans
    # (round 4, security review) rather than a single string with three
    # values ("true"/"false"/"dry_run") — a JSON consumer doing the natural
    # `if payload["dial_fired"]["audit_written"]:` must see False when the
    # row was not actually written, in either case. Overloading a third
    # string value into that field made a lost-row case read as truthy.
    _AUDIT_WRITTEN="false"
    _DIAL_IS_DRY_RUN="false"
    if [[ "$DRY_RUN" != "1" ]]; then
      _AUDIT_ERR=$(python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
from backend.dial_registry import _append_audit, _read_last_audit_hash, _now_iso
_append_audit({
    'kind': 'dial_sandbox_modify_fired',
    'prev_hash': _read_last_audit_hash(),
    'role': sys.argv[2],
    'touchpoints': sys.argv[3],
    'reason': sys.argv[4],
    'timestamp': _now_iso(),
})
" "$REPO_ROOT" "$ROLE" "${TOUCHPOINTS_ARG:-}" "$_DIAL_REASON" 2>&1)
      _AUDIT_RC=$?
      if [[ $_AUDIT_RC -eq 0 ]]; then
        _AUDIT_WRITTEN="true"
      else
        echo "ERROR: dial_sandbox_modify_fired audit write failed (spawn proceeds regardless): ${_AUDIT_ERR}" >&2
      fi
    else
      _DIAL_IS_DRY_RUN="true"
    fi

    # Surface the firing on stdout too — spawn-agent.sh:500 discards this
    # script's stderr on the real production call path, so the warning above
    # is never seen via an actual spawn. The JSON payload is the channel that
    # actually gets kept and parsed; this is what makes the firing routable
    # by Team Lead instead of dependent on a stream nobody reads.
    _DIAL_FIRED_JSON=$(python3 -c "
import json, sys
print(json.dumps({
    'class': sys.argv[1],
    'role': sys.argv[2],
    'touchpoints': sys.argv[3],
    'reason': sys.argv[4],
    'audit_written': sys.argv[5] == 'true',
    'dry_run': sys.argv[6] == 'true',
}))
" "$_DIAL_CLASS" "$ROLE" "${TOUCHPOINTS_ARG:-}" "$_DIAL_REASON" "$_AUDIT_WRITTEN" "$_DIAL_IS_DRY_RUN" 2>/dev/null || echo 'null')
  fi
fi

# ── 2.7. External-intake gate (D#1588 Batch A) — hard secondary layer ────────
# When --discussion N is present, block the spawn if that Discussion is
# provenance:external and has not been intake-approved by a human. Fail-closed:
# any error resolving the author/allowlist classifies external, so an
# unresolvable Discussion is blocked rather than silently allowed through.
# Skipped in dry-run mode (inspection only, no side effects/blocking).
if [[ -n "$DISCUSSION" && "$DRY_RUN" != "1" ]]; then
  _INTAKE_GATE_JSON=$(python3 "$SCRIPT_DIR/lib/external_intake_gate.py" check-discussion "$DISCUSSION" 2>/dev/null || echo '{"blocked":true,"reason":"gate_check_failed"}')
  _INTAKE_BLOCKED=$(echo "$_INTAKE_GATE_JSON" | python3 -c "import sys,json; print(str(json.load(sys.stdin).get('blocked',True)).lower())" 2>/dev/null || echo "true")
  _INTAKE_REASON=$(echo "$_INTAKE_GATE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason',''))" 2>/dev/null || echo "gate_check_failed")
  if [[ "$_INTAKE_BLOCKED" == "true" ]]; then
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] team-lead: external-intake gate blocked spawn of $ROLE for D#$DISCUSSION (${_INTAKE_REASON})" \
      2>/dev/null || true
    echo "blocked_reason=external_intake_gate ${_INTAKE_REASON}" >&2
    # D#1672 (AC-22): the operator message is reason-driven — a Discussion
    # whose approval was auto-dismissed by a post-approval edit has already
    # had intake-approved removed by the bot, so telling the operator to
    # "apply" a label that's already gone (and would just get dismissed
    # again for the same stale content) is actively misleading.
    case "$_INTAKE_REASON" in
      external_edited_after_approval)
        _INTAKE_MSG="the Discussion's content changed after intake-approved was applied, so the bot automatically dismissed the approval (the label has been removed). A maintainer must review the CURRENT Discussion body and re-apply intake-approved."
        ;;
      external_baseline_unreadable)
        _INTAKE_MSG="the approval-baseline store could not be read, so approval cannot be confirmed (fail-closed). This is a system-health issue, not a missing label — check the external-intake-baselines store before retrying."
        ;;
      external_awaiting_intake_approval)
        _INTAKE_MSG="provenance:external without intake-approved. A human maintainer must apply the intake-approved label before this Discussion can be spawned against."
        ;;
      *)
        _INTAKE_MSG="(${_INTAKE_REASON}). A human maintainer must apply the intake-approved label before this Discussion can be spawned against."
        ;;
    esac
    echo "ERROR: external-intake gate blocked spawn of $ROLE for Discussion #$DISCUSSION — ${_INTAKE_MSG}" >&2
    exit 1
  fi
fi

# ── 3. Context load (project context, agent memory, gate context) ─────────────
# Always load context (needed for JSON output); side effects gated on DRY_RUN
_load_context() {
  # D#2100: each of the four commands below used to swallow stderr and
  # collapse "ran fine, printed nothing" and "could not run at all" into
  # the same empty string, then warn as if only the first case happened.
  # Capture stderr and the exit status separately so a crash (e.g. the
  # interpreter on PATH lacking a dependency) is reported as a check that
  # could not run, not as the specific zero-exit condition it never reached.
  local _ctx_err _ctx_rc

  _ctx_err=$(mktemp)
  PROJECT_CONTEXT=$(python3 "$REPO_ROOT/backend/context_manager.py" prompt 2>"$_ctx_err")
  _ctx_rc=$?
  if [[ $_ctx_rc -ne 0 ]]; then
    WARNINGS+=("context_manager.py prompt could not run (exit $_ctx_rc): $(tail -1 "$_ctx_err")")
  elif [[ -z "$PROJECT_CONTEXT" ]]; then
    WARNINGS+=("context_manager.py prompt returned empty")
  fi
  rm -f "$_ctx_err"

  _ctx_err=$(mktemp)
  AGENT_MEMORY=$(python3 "$REPO_ROOT/backend/agent_memory.py" query --role "$ROLE" --limit 5 2>"$_ctx_err")
  _ctx_rc=$?
  if [[ $_ctx_rc -ne 0 ]]; then
    WARNINGS+=("agent_memory.py query could not run for role $ROLE (exit $_ctx_rc): $(tail -1 "$_ctx_err")")
  elif [[ -z "$AGENT_MEMORY" ]]; then
    WARNINGS+=("agent_memory.py returned no lessons for role: $ROLE")
  fi
  rm -f "$_ctx_err"

  _ctx_err=$(mktemp)
  GATE_CONTEXT=$(python3 "$REPO_ROOT/backend/control_plane.py" show 2>"$_ctx_err")
  _ctx_rc=$?
  if [[ $_ctx_rc -ne 0 ]]; then
    WARNINGS+=("control_plane.py show could not run (exit $_ctx_rc): $(tail -1 "$_ctx_err")")
    GATE_CONTEXT="{}"
  elif [[ "$GATE_CONTEXT" == "{}" ]]; then
    WARNINGS+=("control_plane.py show returned empty config")
  fi
  rm -f "$_ctx_err"

  _ctx_err=$(mktemp)
  CARD_CHECK=$(python3 "$REPO_ROOT/backend/agent_cards.py" show "$ROLE" 2>"$_ctx_err")
  _ctx_rc=$?
  if [[ $_ctx_rc -ne 0 ]]; then
    WARNINGS+=("agent card check could not run for $ROLE (exit $_ctx_rc): $(tail -1 "$_ctx_err")")
  elif [[ -z "$CARD_CHECK" ]]; then
    WARNINGS+=("no agent card for $ROLE")
    if [[ "$DRY_RUN" != "1" ]]; then
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] team-lead: WARNING — no agent card for $ROLE" \
        2>/dev/null || true
    fi
  fi
  rm -f "$_ctx_err"


  # Worktree cap check — only applies to worktree-isolated spawns.
  # Roles that don't use isolation:worktree share the main checkout and are never capped here.
  if [[ "$ISOLATION" == "worktree" && -f "$SCRIPT_DIR/lib/worktree-registry.sh" && "$DRY_RUN" != "1" ]]; then
    # shellcheck source=scripts/lib/worktree-registry.sh
    source "$SCRIPT_DIR/lib/worktree-registry.sh" 2>/dev/null || true
    ACTIVE_WORKTREES=$(worktree_registry count-disk 2>/dev/null || echo "0")
    WORKTREE_CAP_VAL="${WORKTREE_CAP:-8}"
    if [[ "$ACTIVE_WORKTREES" -ge "$WORKTREE_CAP_VAL" ]] 2>/dev/null; then
      # D#2059 amendment: this used to `exit 1` here. Enforcement is deferred
      # until (1) D#2001 lands a working reaper -- there is no in-band way
      # back under a cumulative, monotonically-growing directory count today
      # -- and (2) D#2097 settles what this threshold should even be counting
      # (it was chosen for a *live* count, not this cumulative one). Until
      # then this is a warning: emit the audit row, log it, and let the
      # spawn proceed. The `exit 1` this replaced would have blocked every
      # worktree-isolated spawn on hosts where cleanup hasn't run.
      emit_spawn_block "worktree_cap_reached" "worktree cap (${WORKTREE_CAP_VAL}) reached" "{\"active_worktrees\":${ACTIVE_WORKTREES},\"cap\":${WORKTREE_CAP_VAL}}"
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] team-lead: WARNING — worktree cap (${WORKTREE_CAP_VAL}) reached, deferring spawn for ${ROLE}" \
        2>/dev/null || true
      echo "WARNING: worktree cap ($WORKTREE_CAP_VAL) reached — $ACTIVE_WORKTREES active. Allowing spawn of $ROLE; enforcement deferred (see D#2059)." >&2
    fi
  fi

  # Per-project concurrency cap — reads policies.executor.max_concurrent from control_plane.
  # This is the inner bound set via the Settings page slider. The fleet-wide cap (8) is
  # the outer bound checked below. Both must pass for the spawn to proceed.
  # Skipped in dry-run and no-register modes (same guards as fleet cap check).
  if [[ "$DRY_RUN" != "1" && "${NO_REGISTER:-}" != "1" ]]; then
    # D#2314 D1: resolve the fleet.db project-name key through the one shared
    # resolver backend/api.py's read side also calls (backend/fleet/project_name.py).
    # A silent default fallback here is exactly what caused every real spawn to
    # register under a name the dashboard never queried. A loud failure is
    # acceptable; a silent mis-key is not.
    # PYTHONPATH="$REPO_ROOT" is required here (D#2314): this script never
    # `cd`s, and `-m` resolves the `backend` package off cwd/PYTHONPATH, not
    # off this script's own location — invoked from outside $REPO_ROOT this
    # raised ModuleNotFoundError, turning a resolvable project into a
    # hard-blocked spawn. Every `python3 -m backend...` call in this file has
    # the same cwd dependency and now carries this same prefix — see the
    # `register` call further down, whose failure branch does
    # blocked_reason=fleet_cap_exceeded on every real spawn, which is what
    # made this worth fixing everywhere rather than just at the two sites
    # first flagged.
    if ! _PROJECT_NAME_EARLY=$(PYTHONPATH="$REPO_ROOT" python3 -m backend.fleet.project_name "$REPO_ROOT" 2>&1); then
      echo "ERROR: pre-spawn-check: could not resolve fleet project name: $_PROJECT_NAME_EARLY" >&2
      exit 1
    fi
    _PER_PROJECT_CAP=$(python3 "$REPO_ROOT/backend/control_plane.py" get "policies.executor.max_concurrent" 2>/dev/null | tr -d '"' || echo "")
    # Normalize: treat empty / null / non-numeric as "no per-project cap configured"
    if [[ -n "$_PER_PROJECT_CAP" && "$_PER_PROJECT_CAP" != "null" ]] && python3 -c "int('$_PER_PROJECT_CAP')" 2>/dev/null; then
      # count_project_capped, not count_project (D#2314 S2): agent-tool- rows
      # (Agent()-tool registration coverage) must never consume this cap — a
      # busy consensus panel alone can register up to 7 of them.
      # PYTHONPATH="$REPO_ROOT" (D#2314 S3 remnant): this call is new to this
      # PR and had the identical cwd-dependency gap the project_name calls
      # above were fixed for — measured from /tmp it silently returned "0"
      # via the `|| echo "0"` fallback, disabling the per-project cap.
      _ACTIVE_PROJECT=$(PYTHONPATH="$REPO_ROOT" python3 -m backend.fleet.concurrency count_project_capped "$_PROJECT_NAME_EARLY" 2>/dev/null || echo "0")
      _OVER_PER_PROJECT=$(python3 -c "print('true' if int('${_ACTIVE_PROJECT}') >= int('${_PER_PROJECT_CAP}') else 'false')" 2>/dev/null || echo "false")
      if [[ "$_OVER_PER_PROJECT" == "true" ]]; then
        bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
          "[$(date +%H:%M)] team-lead: per-project concurrency cap (${_PER_PROJECT_CAP}) reached for ${_PROJECT_NAME_EARLY} — ${_ACTIVE_PROJECT} active. Spawn of $ROLE blocked." \
          2>/dev/null || true
        echo "ERROR: blocked_reason=per_project_cap_exceeded — per-project agent cap (${_PER_PROJECT_CAP}) reached (${_ACTIVE_PROJECT} active). Spawn of $ROLE blocked." >&2
        exit 1
      fi
    fi
  fi

  # Fleet cap check — cross-project hard block.
  # Calls backend.fleet.concurrency.register(); exits 1 with blocked_reason fleet_cap_exceeded on deny.
  # Guard on DRY_RUN or NO_REGISTER: neither should register slots (they would never be freed).
  # --no-register (alias --dry-run-fleet) is for smoke-test / prompt-assembly-inspection only.
  if [[ "$DRY_RUN" != "1" && "${NO_REGISTER:-}" != "1" ]]; then
    # Same resolver as the per-project cap check above — see D#2314 D1/S3 notes there.
    if ! _PROJECT_NAME=$(PYTHONPATH="$REPO_ROOT" python3 -m backend.fleet.project_name "$REPO_ROOT" 2>&1); then
      echo "ERROR: pre-spawn-check: could not resolve fleet project name: $_PROJECT_NAME" >&2
      exit 1
    fi
    # Use EVENT_ID_ARG as the stable shared identifier — it is passed unchanged to post-agent-hook.sh
    # via --event-id, so register() and unregister() always operate on the same key.
    # Fall back to WORKTREE_ID — a dead fallback, nothing in the tree sets it
    # outside tests — then warn if neither is set.
    if [[ -n "$EVENT_ID_ARG" ]]; then
      _AGENT_ID="$EVENT_ID_ARG"
    elif [[ -n "${WORKTREE_ID:-}" ]]; then
      _AGENT_ID="$WORKTREE_ID"
    else
      echo "[pre-spawn-check] WARN: EVENT_ID_ARG and WORKTREE_ID both unset; fleet slot may leak" >&2
      _AGENT_ID="spawn-$$"
    fi
    # PYTHONPATH="$REPO_ROOT" (D#2314, code review round 2): this call is
    # unconditional on every real spawn and its failure branch exits 1 with
    # blocked_reason=fleet_cap_exceeded — a ModuleNotFoundError here used to
    # hard-block every spawn mislabelled as a cap problem, which is close to
    # undiagnosable since the operator sees a cap error while the cap is
    # fine. spawn-agent.sh already calls this script without cd-ing to the
    # repo root, so this was reachable on the real path, not theoretical.
    if ! PYTHONPATH="$REPO_ROOT" python3 -m backend.fleet.concurrency register "$_PROJECT_NAME" "$_AGENT_ID" "$ROLE" "$$" 2>/dev/null; then
      echo "ERROR: blocked_reason=fleet_cap_exceeded — fleet agent cap reached, spawn of $ROLE blocked." >&2
      exit 1
    fi
  fi
}

if [[ "$DRY_RUN" == "1" ]]; then
  _load_context
elif ! hook_event_has_step "context_load"; then
  _load_context
  hook_event_mark_step "context_load"
else
  # Resume: re-load context for JSON output
  PROJECT_CONTEXT=$(python3 "$REPO_ROOT/backend/context_manager.py" prompt 2>/dev/null || echo "")
  AGENT_MEMORY=$(python3 "$REPO_ROOT/backend/agent_memory.py" query --role "$ROLE" --limit 5 2>/dev/null || echo "")
  GATE_CONTEXT=$(python3 "$REPO_ROOT/backend/control_plane.py" show 2>/dev/null || echo "{}")
fi

# ── 4. Require --event-id (hard error if missing) ─────────────────────────────
if [[ "$DRY_RUN" != "1" ]]; then
  if [[ -z "$EVENT_ID_ARG" ]]; then
    echo "pre-spawn-check: --event-id is required (role=$ROLE)" >&2
    exit 2
  fi
  if ! hook_event_has_step "team_log"; then
    hook_event_mark_step "team_log"
  fi
fi

# ── Build JSON output ──────────────────────────────────────────────────────────
WARNINGS_JSON=$(python3 -c "
import json, sys
w = sys.argv[1:]
print(json.dumps(w))
" "${WARNINGS[@]+"${WARNINGS[@]}"}" 2>/dev/null || echo '[]')

# ── Lessons injection (executor role only) ─────────────────────────────────────
# Scan Discussion spec body for likely file paths and inject up to 3 matching
# lessons from the quality-score lessons store.
LESSONS_JSON='[]'
if [[ "$ROLE" == "executor" ]]; then
  # Extract file globs from Discussion body if available
  SPEC_FILES=""
  if [[ -n "$DISCUSSION" ]]; then
    # --fresh: this pulls a raw filename regex out of the body to feed the
    # lessons store — a stale read here would compute the glob (and the hints
    # that follow from it) against the previous version of the Spec, silently
    # (D#1778). Split into a separate read + rc check because "cmd | ... ||
    # echo ''" under `pipefail` swallows exit code 3 along with any other
    # pipeline failure — there is no way to bolt an rc check onto that idiom.
    _LESSONS_DISC_BODY=$(python3 "$REPO_ROOT/backend/discussion_cache.py" get-body "$DISCUSSION" --fresh 2>/dev/null)
    _LESSONS_DISC_BODY_RC=$?
    if [[ $_LESSONS_DISC_BODY_RC -eq 3 ]]; then
      echo "WARN: could not get a live read of Discussion #$DISCUSSION for lessons injection (GraphQL fetch failed) — using a stale cached body; lessons hints may reference the previous Spec revision." >&2
    fi
    SPEC_FILES=$(printf '%s' "$_LESSONS_DISC_BODY" \
      | grep -oE '[a-z][a-z0-9_/]*\.[a-z]+' \
      | grep -E '\.(ts|tsx|py|css|json|sh|md)$' \
      | head -20 \
      | sort -u \
      | tr '\n' ',' \
      | sed 's/,$//' || echo "")
    unset _LESSONS_DISC_BODY _LESSONS_DISC_BODY_RC
  fi

  LESSONS_JSON=$(python3 -c "
import sys, json
sys.path.insert(0, '${REPO_ROOT}')
try:
    from backend.lessons import LessonsStore
    store = LessonsStore()
    files_raw = sys.argv[1] if len(sys.argv) > 1 else ''
    globs = [f.strip() for f in files_raw.split(',') if f.strip()]
    # Expand bare filenames to directory globs for broader matching
    expanded = []
    for g in globs:
        parts = g.split('/')
        if len(parts) > 1:
            expanded.append(parts[0] + '/**')
        expanded.append(g)
    expanded = list(dict.fromkeys(expanded))  # deduplicate, preserve order
    lessons = store.pick_for_prompt(role='executor', files_globs=expanded or ['*'], max_lessons=3)
    print(json.dumps(lessons))
except Exception:
    print('[]')
" "${SPEC_FILES}" 2>/dev/null || echo '[]')
fi

# ── Persona voice injection ────────────────────────────────────────────────────
# Source persona.sh and build the ## Voice block for this role (empty string if
# no persona file exists — mechanical roles like reapers have no persona).
PERSONA_VOICE=""
if [[ -f "$SCRIPT_DIR/lib/persona.sh" ]]; then
  # shellcheck source=scripts/lib/persona.sh
  source "$SCRIPT_DIR/lib/persona.sh"
  PERSONA_VOICE=$(persona_voice_block "$ROLE" 2>/dev/null || true)
fi

# ── Working Principles injection ───────────────────────────────────────────────
# Source working-principles.sh and capture the ## Working Principles block.
# Unlike persona, this block is role-agnostic — every agent role gets it.
WORKING_PRINCIPLES=""
if [[ -f "$SCRIPT_DIR/lib/working-principles.sh" ]]; then
  # shellcheck source=scripts/lib/working-principles.sh
  source "$SCRIPT_DIR/lib/working-principles.sh"
  WORKING_PRINCIPLES=$(working_principles_block 2>/dev/null || true)
fi

# ── Self-Observe Gate injection ────────────────────────────────────────────────
# Inject the self-observe gate block for executor roles.
# Gate defaults to false (shadow mode). When true, non-corrected findings flip verdict.
SELF_OBSERVE_GATE=""
if [[ -f "$SCRIPT_DIR/lib/self-observe-gate.sh" ]]; then
  # shellcheck source=scripts/lib/self-observe-gate.sh
  source "$SCRIPT_DIR/lib/self-observe-gate.sh"
  if [[ "$ROLE" == "executor" ]]; then
    SO_GATE_ENABLED=$(python3 -c "
import json, sys
try:
    d = json.load(open('.autonomous-team/config.json'))
    print('true' if d.get('gates', {}).get('self_observe_executor', False) else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")
    if [[ "$SO_GATE_ENABLED" == "true" ]]; then
      SELF_OBSERVE_GATE=$(self_observe_gate_block "$REPO_ROOT" 2>/dev/null || true)
    else
      SELF_OBSERVE_GATE=$(self_observe_gate_block --shadow "$REPO_ROOT" 2>/dev/null || true)
    fi
  fi
fi

python3 -c "
import json, sys

role = sys.argv[1]
discussion = sys.argv[2] or None
project_context = sys.argv[3]
agent_memory = sys.argv[4]
gate_context_raw = sys.argv[5]
circuit_failures = int(sys.argv[6])
budget_remaining = int(sys.argv[7])
warnings_raw = sys.argv[8]
in_flight = int(sys.argv[9])
lessons_raw = sys.argv[10]
persona_voice = sys.argv[11]
working_principles = sys.argv[12]
self_observe_gate = sys.argv[13]
dial_fired_raw = sys.argv[14]

try:
    gate_context = json.loads(gate_context_raw)
except Exception:
    gate_context = gate_context_raw

try:
    warnings = json.loads(warnings_raw)
except Exception:
    warnings = []

try:
    lessons = json.loads(lessons_raw)
except Exception:
    lessons = []

try:
    dial_fired = json.loads(dial_fired_raw)
except Exception:
    dial_fired = None

out = {
    'allowed': True,
    'role': role,
    'project_context': project_context,
    'agent_memory': agent_memory,
    'gate_context': gate_context,
    'circuit_breaker_failures': circuit_failures,
    'budget_remaining': budget_remaining,
    'warnings': warnings,
    'in_flight_impl': in_flight,
    'lessons': lessons,
    'persona_voice': persona_voice,
    'working_principles': working_principles,
    'self_observe_gate': self_observe_gate,
}
if discussion:
    out['discussion'] = int(discussion)
# Only present when a dial actually fired this spawn (D#1805 round 3) — a key
# that shows up on every spawn is noise that gets filtered, which is exactly
# how an unobserved firing becomes invisible again.
if dial_fired:
    out['dial_fired'] = dial_fired

print(json.dumps(out, indent=2))
" \
  "$ROLE" \
  "$DISCUSSION" \
  "$PROJECT_CONTEXT" \
  "$AGENT_MEMORY" \
  "$GATE_CONTEXT" \
  "$CIRCUIT_FAILURES" \
  "$BUDGET_REMAINING" \
  "$WARNINGS_JSON" \
  "$IN_FLIGHT" \
  "$LESSONS_JSON" \
  "$PERSONA_VOICE" \
  "$WORKING_PRINCIPLES" \
  "$SELF_OBSERVE_GATE" \
  "$_DIAL_FIRED_JSON"

if [[ "$DRY_RUN" != "1" ]]; then
  hook_event_finish
fi
