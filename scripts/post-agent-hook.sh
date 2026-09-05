#!/usr/bin/env bash
# post-agent-hook.sh — run after EVERY agent completion to enforce coordination discipline.
#
# Usage:
#   bash scripts/post-agent-hook.sh \
#     --role <role> \
#     --discussion <N> \
#     --verdict <verdict> \
#     --input-tokens <N> \
#     --output-tokens <N> \
#     [--pr <N>] \
#     [--model <model>] \
#     [--files <comma-separated>] \
#     [--content <lesson text>] \
#     [--event-id <id>] \
#     [--resume]
#
# Idempotent: pass the same --event-id twice and the second call is a no-op.
# Crash-safe: re-run with the same --event-id to resume from where it stopped.
#
# Steps (in order):
#   agent_feed → budget → circuit_breaker → kpi → audit → role_verdict_metric → complete_run → verdict_label → pr_artifacts → memory → training_mine → cost_summary → post_agent_cleanup → worktree_registry → self_observe_check → scope_drift_check → anomaly_check → reap_worktrees → team_log

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# The CODE plane. _REPO's only consumer here is verify_pr_exists (below), which
# REST-checks repos/$_REPO/pulls/$PR — a PR, so a code-plane surface.
#
# Failure direction: verify_pr_exists downgrades verdict=done to fail on a 404.
# Post-cutover a Discussion-plane slug would 404 for every PR that exists, so
# *every* executor PR would be reported as having silently failed to create.
# This site was deferred to "PR-d" in an earlier plan, but PR-d's subject is
# post-merge-hook.sh — a different file that does not use this resolver — so
# nothing actually covered it.
_REPO="$(_require_code_repo "post-agent-hook")" || exit 1
# shellcheck source=scripts/lib/state-dir.sh
source "$SCRIPT_DIR/lib/state-dir.sh" || true

ROLE=""
DISCUSSION=""
VERDICT=""
INPUT_TOKENS=0
OUTPUT_TOKENS=0
CACHE_READ_TOKENS=0
CACHE_WRITE_TOKENS=0
CACHE_CREATION_TOKENS=0
FIRST_WRITE_TURN=""
PR=""
MODEL="claude-sonnet-4-20250514"
FILES=""
CONTENT=""
EVENT_ID_ARG=""
RESUME_FLAG=""
BLOCKED_REASON=""
# self-observe gate: caller passes --self-observed true when envelope contains self_observed:true
AGENT_SELF_OBSERVED="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)          ROLE="$2";          shift 2 ;;
    --discussion)    DISCUSSION="$2";    shift 2 ;;
    --verdict)       VERDICT="$2";       shift 2 ;;
    --input-tokens)  INPUT_TOKENS="$2";  shift 2 ;;
    --output-tokens)          OUTPUT_TOKENS="$2";          shift 2 ;;
    --cache-read-tokens)      CACHE_READ_TOKENS="$2";      shift 2 ;;
    --cache-write-tokens)     CACHE_WRITE_TOKENS="$2";     shift 2 ;;
    --cache-creation-tokens)  CACHE_CREATION_TOKENS="$2";  shift 2 ;;
    --first-write-turn)       FIRST_WRITE_TURN="$2";       shift 2 ;;
    --pr)                 PR="$2";                 shift 2 ;;
    --model)              MODEL="$2";              shift 2 ;;
    --files)              FILES="$2";              shift 2 ;;
    --content)            CONTENT="$2";            shift 2 ;;
    --event-id)           EVENT_ID_ARG="$2";       shift 2 ;;
    --self-observed)      AGENT_SELF_OBSERVED="$2"; shift 2 ;;
    --blocked-reason)     BLOCKED_REASON="$2";     shift 2 ;;
    --resume)             RESUME_FLAG="--resume";  shift ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 --role <role> --discussion <N> --verdict <verdict> --input-tokens <N> --output-tokens <N>" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$ROLE" || -z "$VERDICT" ]]; then
  echo "Error: --role and --verdict are required" >&2
  exit 1
fi

# ── PR existence verification ─────────────────────────────────────────────────
# When verdict=done and --pr is set, REST-verify the PR exists before any state
# mutation.  A 404 means gh pr create silently failed — downgrade to fail.
DOWNGRADE_REASON=""
verify_pr_exists() {
  [ "$VERDICT" = "done" ] || return 0
  [ -n "$PR" ] || return 0
  local http_code
  http_code=$(gh api -i "repos/$_REPO/pulls/$PR" 2>/dev/null \
    | head -1 | awk '{print $2}')
  case "$http_code" in
    200)
      echo "[post-agent-hook] PR #$PR verified (HTTP 200)"
      return 0
      ;;
    404)
      VERDICT="fail"
      DOWNGRADE_REASON="pr_create_failed: PR #$PR not found"
      echo "[post-agent-hook] WARN — downgraded $ROLE verdict done→fail ($DOWNGRADE_REASON)" >&2
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] post-agent-hook: WARN — downgraded $ROLE verdict done→fail ($DOWNGRADE_REASON)" \
        2>/dev/null || true
      ;;
    *)
      echo "[post-agent-hook] WARN — PR verify inconclusive for #$PR (http=$http_code), proceeding with original verdict" >&2
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] post-agent-hook: WARN — PR verify inconclusive for #$PR (http=$http_code), proceeding with original verdict" \
        2>/dev/null || true
      ;;
  esac
}
verify_pr_exists

# Export context for hook-event.sh ID generation
# NOTE: VERDICT may have been downgraded by verify_pr_exists above.
export HOOK_ROLE="$ROLE"
export HOOK_DISCUSSION="$DISCUSSION"
export HOOK_PR="${PR:-}"
export HOOK_VERDICT="$VERDICT"
export HOOK_CALLER="post-agent-hook"

# Source shared idempotency helpers
# shellcheck source=scripts/lib/hook-event.sh
source "$SCRIPT_DIR/lib/hook-event.sh"

# The spawn event ID is shared with pre-spawn-check.sh. If we use it as-is,
# hook_event_init sees the pre-spawn-check done marker and exits immediately as
# a no-op — the real verdict never gets written.
# Fix: use a "-pah" suffixed ID for post-agent-hook idempotency while keeping
# the raw ID for agent_run_tracker complete_run and write_task (so they match
# the rows written at spawn time).
TASK_EVENT_ID="${EVENT_ID_ARG}"  # raw: must match spawn-time task record + start_run row
INIT_ARGS=()
if [[ -n "$EVENT_ID_ARG" ]]; then
  INIT_ARGS+=(--event-id "${EVENT_ID_ARG}-pah")
fi
[[ -n "$RESUME_FLAG" ]]  && INIT_ARGS+=(--resume)

hook_event_init "post-agent-hook" \
  "agent_feed,team_substrate,budget,circuit_breaker,kpi,audit,role_verdict_metric,complete_run,verdict_label,pr_artifacts,memory,training_mine,cost_summary,post_agent_cleanup,worktree_registry,self_observe_check,scope_drift_check,anomaly_check,reap_worktrees,team_log" \
  "${INIT_ARGS[@]:-}"

echo "[post-agent-hook] event_id=${HOOK_EVENT_ID} role=$ROLE verdict=$VERDICT"

# ── 0. Agent feed — JSONL write FIRST (full event, sync, flocked) ─────────────
if ! hook_event_has_step "agent_feed"; then
  if [ "${AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD:-0}" != "1" ]; then
    FEED_MSG="$ROLE: $VERDICT"
    [[ -n "$DISCUSSION" ]] && FEED_MSG="$FEED_MSG D#$DISCUSSION"
    [[ -n "$PR" ]]         && FEED_MSG="$FEED_MSG PR#$PR"
    # Truncate to 280 chars
    FEED_MSG="${FEED_MSG:0:280}"

    FEED_ARGS=(
      --role "$ROLE"
      --event-type "agent_end"
      --message "$FEED_MSG"
      --verdict "$VERDICT"
      --input-tokens "$INPUT_TOKENS"
      --output-tokens "$OUTPUT_TOKENS"
      --model "$MODEL"
    )
    [[ -n "$DISCUSSION" ]]                               && FEED_ARGS+=(--discussion "$DISCUSSION")
    [[ -n "$PR" ]]                                         && FEED_ARGS+=(--pr "$PR")
    [[ -n "$FILES" ]]                                      && FEED_ARGS+=(--files "$FILES")
    [[ "${CACHE_READ_TOKENS:-0}" -gt 0 ]] 2>/dev/null      && FEED_ARGS+=(--cache-read-tokens "$CACHE_READ_TOKENS")
    [[ "${CACHE_WRITE_TOKENS:-0}" -gt 0 ]] 2>/dev/null     && FEED_ARGS+=(--cache-write-tokens "$CACHE_WRITE_TOKENS")

    bash "$SCRIPT_DIR/agent-feed-append.sh" "${FEED_ARGS[@]}" \
      || true  # non-fatal unless disk full (agent-feed-append.sh handles exit codes)
  fi
  hook_event_mark_step "agent_feed"
fi

# ── 0b. Team substrate — write task record (primary path, completion update) ──
# Updates the task record written at spawn time (status: pending → final verdict).
# Uses TASK_EVENT_ID (raw spawn ID) so it matches the spawn-time task record.
# Non-fatal: agent-feed.jsonl legacy readers are unaffected.
if ! hook_event_has_step "team_substrate"; then
  # Values are passed as argv (never interpolated into the script text) so a
  # single quote in an agent-authored AGENT_OUTPUT field cannot break out of
  # the Python source and execute arbitrary code (CWE-94/CWE-78).
  python3 -c '
import sys, os
sys.path.insert(0, sys.argv[1])
try:
    from backend.agent_teams_substrate import write_task
    disc = sys.argv[3] or None
    pr   = sys.argv[4] or None
    write_task(sys.argv[2], {"status": sys.argv[5], "discussion": disc, "pr": pr})
except Exception as e:
    print(f"[post-agent-hook] WARN: write_task failed: {e}", file=sys.stderr)
' "$REPO_ROOT" "$TASK_EVENT_ID" "$DISCUSSION" "$PR" "$VERDICT"
  hook_event_mark_step "team_substrate"
fi

# ── 1. Budget spend ───────────────────────────────────────────────────────────
if ! hook_event_has_step "budget"; then
  RECORD_ARGS=(--role "$ROLE" --verdict "$VERDICT" --input-tokens "$INPUT_TOKENS" --output-tokens "$OUTPUT_TOKENS" --model "$MODEL")
  [[ -n "$DISCUSSION" ]]                               && RECORD_ARGS+=(--discussion "$DISCUSSION")
  [[ -n "$FILES" ]]                                    && RECORD_ARGS+=(--files "$FILES")
  [[ -n "$CONTENT" ]]                                  && RECORD_ARGS+=(--content "$CONTENT")
  [[ -n "$PR" ]]                                       && RECORD_ARGS+=(--pr "$PR")
  [[ "${CACHE_READ_TOKENS:-0}" -gt 0 ]] 2>/dev/null    && RECORD_ARGS+=(--cache-read-tokens "$CACHE_READ_TOKENS")
  [[ "${CACHE_WRITE_TOKENS:-0}" -gt 0 ]] 2>/dev/null   && RECORD_ARGS+=(--cache-write-tokens "$CACHE_WRITE_TOKENS")

  echo "[post-agent-hook] Recording agent result: role=$ROLE verdict=$VERDICT"
  bash "$SCRIPT_DIR/record-agent-result.sh" "${RECORD_ARGS[@]}" 2>/dev/null \
    || echo "[post-agent-hook] Warning: record-agent-result.sh failed (non-fatal)" >&2
  hook_event_mark_step "budget"
fi

# ── 2. Circuit breaker update ─────────────────────────────────────────────────
if ! hook_event_has_step "circuit_breaker"; then
  if [[ -n "$DISCUSSION" ]]; then
    case "$VERDICT" in
      fail|needs-fix)
        echo "[post-agent-hook] Circuit breaker: recording failure for Discussion #$DISCUSSION"
        python3 "$REPO_ROOT/backend/circuit_breaker.py" record "$DISCUSSION" "$ROLE" "$VERDICT" 2>/dev/null \
          || echo "[post-agent-hook] Warning: circuit_breaker record failed (non-fatal)" >&2
        ;;
      pass|done)
        echo "[post-agent-hook] Circuit breaker: resetting Discussion #$DISCUSSION"
        python3 "$REPO_ROOT/backend/circuit_breaker.py" reset "$DISCUSSION" 2>/dev/null \
          || echo "[post-agent-hook] Warning: circuit_breaker reset failed (non-fatal)" >&2
        ;;
      *)
        echo "[post-agent-hook] Circuit breaker: no update for verdict=$VERDICT"
        ;;
    esac
  fi
  hook_event_mark_step "circuit_breaker"
fi

# ── 3. Quality scorer for code-reviewer pass ─────────────────────────────────
if ! hook_event_has_step "kpi"; then
  if [[ "$ROLE" == "code-reviewer" && "$VERDICT" == "pass" && -n "$PR" ]]; then
    echo "[post-agent-hook] Running quality scorer for PR #$PR"
    SCORER_ARGS=(--pr "$PR")
    [[ -n "$DISCUSSION" ]] && SCORER_ARGS+=(--discussion "$DISCUSSION")
    python3 "$REPO_ROOT/backend/quality_scorer.py" score "${SCORER_ARGS[@]}" 2>/dev/null \
      || echo "[post-agent-hook] Warning: quality_scorer failed (non-fatal)" >&2
  fi

  # KPI update
  echo "[post-agent-hook] Refreshing KPI snapshot"
  python3 "$REPO_ROOT/backend/kpi_engine.py" compute 2>/dev/null \
    || echo "[post-agent-hook] Warning: kpi_engine compute failed (non-fatal)" >&2
  hook_event_mark_step "kpi"
fi

# ── 4. Audit trail ────────────────────────────────────────────────────────────
if ! hook_event_has_step "audit"; then
  # Cost tracker update note: cost_tracker.py reads from blackboard budget/agents/* keys
  # (populated by record-agent-result.sh above via budget.py spend). No separate record call needed.
  AGENT_ID="${ROLE}-${DISCUSSION:-nodisc}-$(date +%s)"
  echo "[post-agent-hook] Cost tracker: spend already recorded via record-agent-result.sh (id=$AGENT_ID)"
  hook_event_mark_step "audit"
fi

# ── 4b. Role verdict metric (Discussion #540) ─────────────────────────────────
# Emit a raw role_verdict event so role_success_rate_24h() can aggregate at
# read time. Non-fatal: a missing DuckDB or import error must not block the hook.
if ! hook_event_has_step "role_verdict_metric"; then
  # Validate: refuse to write garbage role/verdict values.
  # "unknown" values come from the EXIT trap in spawn-agent.sh (fires before the
  # subagent even runs) and should never pollute the metric store.
  _RVM_REJECT=0
  if [ -z "$ROLE" ] || [ "$ROLE" = "unknown" ] || [ -z "$VERDICT" ] || [ "$VERDICT" = "unknown" ]; then
    _RVM_REJECT=1
    _REJECT_LOG="$REPO_ROOT/.autonomous-team/dashboard-logs/role-verdict-rejects.log"
    mkdir -p "$(dirname "$_REJECT_LOG")" 2>/dev/null || true
    echo "$(date -Iseconds) reject role=$ROLE verdict=$VERDICT pid=$$ caller=$0" \
      >> "$_REJECT_LOG" 2>/dev/null || true
    echo "[post-agent-hook] role_verdict_metric: skipped — role='$ROLE' or verdict='$VERDICT' is unknown/empty (logged to role-verdict-rejects.log)" >&2
  fi
  if [ "$_RVM_REJECT" -eq 0 ]; then
    python3 "$REPO_ROOT/backend/stats_writer.py" emit-verdict \
      --role "$ROLE" --verdict "$VERDICT" 2>/dev/null || true
  fi
  unset _RVM_REJECT _REJECT_LOG
  hook_event_mark_step "role_verdict_metric"
fi

# ── 4c. agent_run_tracker complete_run (Discussion #635 PR-c) ─────────────────
# Update the agent_run row written by spawn-agent.sh (start_run, PR-b) with
# the final verdict, token counts, and end_ts / duration_s.
# Non-fatal: any failure is logged to stderr; the hook exit code is unaffected.
if ! hook_event_has_step "complete_run"; then
  # Use the raw spawn event_id (TASK_EVENT_ID) that spawn-agent.sh used in start_run.
  # HOOK_EVENT_ID is the "-pah" suffixed idempotency ID; TASK_EVENT_ID is the original.
  # When TASK_EVENT_ID is empty, HOOK_EVENT_ID is a hash-derived id (see
  # hook-event.sh:_hook_event_generate_id) that is guaranteed NOT to match the
  # id start_run() wrote — the fallback creates an orphan row rather than
  # updating the started one. Keep the fallback (losing the row entirely is
  # worse) but say so loudly instead of silently substituting (D#1812).
  if [[ -z "${TASK_EVENT_ID:-}" ]]; then
    echo "[post-agent-hook] WARN: TASK_EVENT_ID is empty — falling back to HOOK_EVENT_ID='${HOOK_EVENT_ID:-<unset>}' for complete_run. This id will NOT match the start_run row (if any); expect an orphan row." >&2
  fi
  _CR_AGENT_ID="${TASK_EVENT_ID:-${HOOK_EVENT_ID}}"
  _CR_ARGS=(
    --agent-id "$_CR_AGENT_ID"
    --verdict  "$VERDICT"
    --input-tokens  "$INPUT_TOKENS"
    --output-tokens "$OUTPUT_TOKENS"
  )
  # D#2316 PR-b: pass the role/discussion already resolved by this point in the
  # hook (ROLE/DISCUSSION are in scope from step 0's arg parsing) so a run
  # spawned via Agent() — which never calls start_run() — lands with its real
  # role instead of complete_run() unconditionally stamping orphan-unmatched.
  # complete_run() only uses these on the INSERT branch (no start_run() row);
  # an existing row's role/discussion is never touched by this (no-clobber,
  # item 11). An unrecognised ROLE still resolves to orphan-unmatched inside
  # complete_run() itself (no-guessing, item 10) — nothing is validated here.
  [[ -n "${ROLE:-}"       && "$ROLE" != "unknown" ]]        && _CR_ARGS+=(--role "$ROLE")
  [[ -n "${DISCUSSION:-}" ]]                                && _CR_ARGS+=(--discussion "$DISCUSSION")
  [[ "${CACHE_READ_TOKENS:-0}"       -gt 0 ]] 2>/dev/null && _CR_ARGS+=(--cache-read  "$CACHE_READ_TOKENS")
  [[ "${CACHE_WRITE_TOKENS:-0}"      -gt 0 ]] 2>/dev/null && _CR_ARGS+=(--cache-write "$CACHE_WRITE_TOKENS")
  [[ "${CACHE_CREATION_TOKENS:-0}"   -gt 0 ]] 2>/dev/null && _CR_ARGS+=(--cache-creation-tokens "$CACHE_CREATION_TOKENS")
  [[ -n "${BLOCKED_REASON:-}"  ]]                          && _CR_ARGS+=(--blocked-reason "$BLOCKED_REASON")
  [[ -n "${MODEL:-}"           ]]                          && _CR_ARGS+=(--model "$MODEL")
  [[ -n "${FIRST_WRITE_TURN:-}" ]]                         && _CR_ARGS+=(--first-write-turn "$FIRST_WRITE_TURN")

  # No longer discarding stderr here: complete_run's own orphan warning (D#1812)
  # and any other diagnostic it logs need to actually reach the hook's stderr
  # instead of a stream nobody reads.
  python3 "$REPO_ROOT/backend/agent_run_tracker.py" complete "${_CR_ARGS[@]}" \
    || echo "[post-agent-hook] WARN: complete_run failed for $_CR_AGENT_ID (non-fatal)" >&2
  hook_event_mark_step "complete_run"
fi

# ── 4c2. Verdict-overturn detection (Discussion #1397) ───────────────────────
if ! hook_event_has_step "verdict_overturn"; then
  if [[ -f "$SCRIPT_DIR/hooks/post-agent.d/verdict-overturn.sh" ]]; then
    export _REPO REPO_ROOT PR ROLE VERDICT
    source "$SCRIPT_DIR/hooks/post-agent.d/verdict-overturn.sh" 2>/dev/null || true
  fi
  hook_event_mark_step "verdict_overturn"
fi

# ── 4c3. Verdict → label relay (D#2031) ──────────────────────────────────────
if ! hook_event_has_step "verdict_label"; then
  [ -f "$SCRIPT_DIR/hooks/post-agent.d/verdict-label.sh" ] && { export REPO_ROOT PR ROLE VERDICT; source "$SCRIPT_DIR/hooks/post-agent.d/verdict-label.sh" 2>/dev/null || true; }
  hook_event_mark_step "verdict_label"
fi

# ── 4d. PR test-artifact persistence (Discussion #964) ───────────────────────
# When the agent's envelope contains a tests_run array, persist each entry to
# .autonomous-team/pr-artifacts/<pr>/<sha>.jsonl for downstream agents to reuse.
# Non-fatal: any failure is logged to stderr; the hook exit code is unaffected.
if ! hook_event_has_step "pr_artifacts"; then
  if [[ -f "$SCRIPT_DIR/hooks/post-agent.d/pr-artifacts.sh" ]]; then
    # Export vars needed by the module
    export _REPO REPO_ROOT PR ROLE CONTENT
    source "$SCRIPT_DIR/hooks/post-agent.d/pr-artifacts.sh" 2>/dev/null || true
  fi
  hook_event_mark_step "pr_artifacts"
fi

# ── 5. Agent memory ───────────────────────────────────────────────────────────
if ! hook_event_has_step "memory"; then
  # Memory is recorded via record-agent-result.sh in the budget step above.
  # This step is a no-op marker for replay determinism.
  echo "[post-agent-hook] Memory: recorded via record-agent-result.sh"
  hook_event_mark_step "memory"
fi

# ── 6. Training data miner ───────────────────────────────────────────────────
# Every script this step runs lives under scripts/training/, which moved to the
# private internal repo (D#2348 phase 1). The calls were already non-fatal, but
# "non-fatal" meant printing `incremental-miner failed (non-fatal)` to stderr on
# EVERY agent run — a permanent warning about an absence that is now the normal
# state. Skip the step when the directory isn't here, the same way the
# cost-summary step below guards on its own file existing.
if ! hook_event_has_step "training_mine"; then
 if [[ ! -d "$REPO_ROOT/scripts/training" ]]; then
  echo "[post-agent-hook] Training miner: scripts/training/ not in this tree — skipping"
 else
  echo "[post-agent-hook] Mining new training examples"
  python3 "$REPO_ROOT/scripts/training/incremental-miner.py" 2>/dev/null \
    || echo "[post-agent-hook] Warning: incremental-miner failed (non-fatal)" >&2

  # Training threshold trigger — gated behind gates.training_triggers (default false).
  # Set true to re-enable team-log emission. Mining above runs regardless of gate.
  TRAIN_GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.training_triggers 2>/dev/null | tr -d '"' || echo "false")
  if [ "$TRAIN_GATE" = "true" ]; then
    if python3 "$REPO_ROOT/scripts/training/training-trigger.py" --check 2>/dev/null; then
      : # below threshold — no-op
    else
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] training-trigger: threshold reached — run \`bash scripts/training/vast-bringup.sh --confirm-cost\` to start a training pass" \
        2>/dev/null || true
    fi
  fi

  # Auto-fire training on existing box (opt-in)
  if [ "${AUTO_TRAIN_EXISTING:-0}" = "1" ] && [ -f "$REPO_ROOT/.autonomous-team/vast-training.json" ]; then
    echo "[post-agent-hook] Checking training threshold (existing-box mode)"
    python3 "$REPO_ROOT/scripts/training/training-trigger.py" --mode existing-box --quiet || true
  fi
 fi

  hook_event_mark_step "training_mine"
fi

# ── 6a-b. Fleet cost summary ─────────────────────────────────────────────────
# Note: fleet.unregister (remove project from fleet registry on agent teardown)
# arrives via PR #947 (FleetConcurrencyTile WAL reader) — it inserts after this block.
if ! hook_event_has_step "cost_summary"; then
  if [[ -f "$SCRIPT_DIR/hooks/post-agent.d/cost-summary.sh" ]]; then
    source "$SCRIPT_DIR/hooks/post-agent.d/cost-summary.sh" 2>/dev/null || true
  fi
  hook_event_mark_step "cost_summary"
fi

# ── 6b0. post_agent_cleanup — prune per-worktree build artifacts ─────────────
# Reads post_agent_cleanup from .autonomous-team/project.json (if present).
# Each entry is a shell command string; the literal token $WORKTREE is replaced
# with the agent's worktree path before execution.  Failures are logged to
# stderr but do NOT abort the hook.  Absent field or missing file → no-op.
if ! hook_event_has_step "post_agent_cleanup"; then
  _PAC_WORKTREE=""
  if [[ -n "${WORKTREE_ID:-}" ]]; then
    _PAC_WORKTREE="$REPO_ROOT/.claude/worktrees/$WORKTREE_ID"
  fi

  if [[ -n "$_PAC_WORKTREE" ]]; then
    _PAC_CMDS=$(python3 -c "
import json, pathlib, sys
p = pathlib.Path('$REPO_ROOT/.autonomous-team/project.json')
if not p.exists():
    sys.exit(0)
try:
    d = json.loads(p.read_text())
except Exception:
    sys.exit(0)
cmds = d.get('post_agent_cleanup', [])
if not isinstance(cmds, list):
    sys.exit(0)
for c in cmds:
    if isinstance(c, str):
        print(c)
" 2>/dev/null || true)

    if [[ -n "$_PAC_CMDS" ]]; then
      echo "[post-agent-hook] Running post_agent_cleanup for worktree: $_PAC_WORKTREE"
      while IFS= read -r _PAC_CMD; do
        [[ -z "$_PAC_CMD" ]] && continue
        _PAC_EXPANDED="${_PAC_CMD//\$WORKTREE/$_PAC_WORKTREE}"
        echo "[post-agent-hook] post_agent_cleanup: ${WORKTREE_ID:-unknown} — $_PAC_CMD"
        if ! bash -c "$_PAC_EXPANDED"; then
          echo "[post_agent_cleanup] WARN: command failed: $_PAC_EXPANDED" >&2
        fi
      done <<< "$_PAC_CMDS"
    fi
  fi
  hook_event_mark_step "post_agent_cleanup"
fi

# ── 6b. Worktree registry status transition ──────────────────────────────────
# If the agent ran in a worktree, update its registry status on graceful exit.
if ! hook_event_has_step "worktree_registry"; then
  WORKTREE_ID="${WORKTREE_ID:-}"
  if [[ -n "$WORKTREE_ID" && -f "$SCRIPT_DIR/lib/worktree-registry.sh" ]]; then
    # shellcheck source=scripts/lib/worktree-registry.sh
    source "$SCRIPT_DIR/lib/worktree-registry.sh" 2>/dev/null || true
    case "$VERDICT" in
      done)
        # Executor finished. If a PR was opened, leave the registry status as-is
        # (the executor should have called mark-status pushed after git push).
        # post-merge-hook.sh will mark it merged, and the reaper will then clean it up.
        # Only mark discarded when there is no PR (e.g. executor bailed before pushing).
        if [[ -z "$PR" ]]; then
          worktree_registry mark-status "$WORKTREE_ID" discarded 2>/dev/null || true
        fi
        ;;
      pass|fail|needs-fix|skip)
        # Reviewer or other non-executor role completed — always safe to discard the
        # worktree because reviewers never leave work that post-merge-hook needs to find.
        worktree_registry mark-status "$WORKTREE_ID" discarded 2>/dev/null || true
        ;;
    esac
  fi
  hook_event_mark_step "worktree_registry"
fi

# ── 6b2. Fleet concurrency unregister ────────────────────────────────────────
# Release the fleet slot acquired by pre-spawn-check.sh on agent completion.
# Use TASK_EVENT_ID (the raw spawn event_id) as the stable key — it matches what
# pre-spawn-check.sh used as EVENT_ID_ARG when registering.  Fall back to WORKTREE_ID,
# then warn if neither is available (the slot will leak in that degenerate case).
# Same resolver pre-spawn-check.sh registers with (backend/fleet/project_name.py,
# D#2314 D1) — unregister must use the identical key or the row leaks. Best-effort:
# this is teardown cleanup, not a spawn gate, so a resolution failure here is a
# leaked fleet-cap slot (unregister below becomes a no-op), not a blocked agent.
# PYTHONPATH="$REPO_ROOT" — see D#2314 S3 note in pre-spawn-check.sh; this
# script never cds either, so -m needs it set explicitly.
_FC_PROJECT=$(PYTHONPATH="$REPO_ROOT" python3 -m backend.fleet.project_name "$REPO_ROOT" 2>/dev/null || true)
if [[ -n "${TASK_EVENT_ID:-}" ]]; then
  _FC_AGENT_ID="$TASK_EVENT_ID"
elif [[ -n "${WORKTREE_ID:-}" ]]; then
  _FC_AGENT_ID="$WORKTREE_ID"
else
  echo "[post-agent-hook] WARN: TASK_EVENT_ID and WORKTREE_ID both unset; fleet slot may not be released" >&2
  _FC_AGENT_ID="spawn-$$"
fi
PYTHONPATH="$REPO_ROOT" python3 -m backend.fleet.concurrency unregister "$_FC_PROJECT" "$_FC_AGENT_ID" 2>/dev/null || true

# ── 6c. Self-observe gate enforcement ────────────────────────────────────────
# When gates.self_observe_enforcement = "advisory" (or "enforced"), check whether
# the agent included self_observed:true in its AGENT_OUTPUT envelope.
# Shadow mode (default) = no check, no warning.
# Advisory mode = emit a team-log WARN when a done/pass verdict skips the gate.
# Enforced mode = same warning (verdict downgrade deferred to a future phase).
if ! hook_event_has_step "self_observe_check"; then
  SO_ENFORCEMENT=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.self_observe_enforcement 2>/dev/null | tr -d '"' || echo "shadow")
  # Only act in advisory or enforced mode
  if [[ "$SO_ENFORCEMENT" == "advisory" || "$SO_ENFORCEMENT" == "enforced" ]]; then
    # Only check verdicts where self-observe matters: done or pass
    if [[ "$VERDICT" == "done" || "$VERDICT" == "pass" ]]; then
      # AGENT_ENVELOPE_SELF_OBSERVED may be set by caller; otherwise default missing/false
      # Callers that parse the agent output can pass: --self-observed true
      if [[ "${AGENT_SELF_OBSERVED:-false}" != "true" ]]; then
        AGENT_ID="${ROLE}-${DISCUSSION:-nodisc}-${PR:-nopr}"
        bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
          "[$(date +%H:%M)] team-lead: WARN — agent=${AGENT_ID} role=${ROLE} skipped self-observe gate (${SO_ENFORCEMENT} mode)" \
          2>/dev/null || true
        echo "[post-agent-hook] self-observe gate: WARN agent=${AGENT_ID} role=${ROLE} verdict=${VERDICT} mode=${SO_ENFORCEMENT}" >&2
      fi
    fi
  fi
  hook_event_mark_step "self_observe_check"
fi

# ── 6d. Scope-drift check — warn when executor commits files outside Spec list ──
if ! hook_event_has_step "scope_drift_check"; then
  if [[ -f "$SCRIPT_DIR/hooks/post-agent.d/scope-drift-check.sh" ]]; then
    source "$SCRIPT_DIR/hooks/post-agent.d/scope-drift-check.sh" 2>/dev/null || true
  fi
  hook_event_mark_step "scope_drift_check"
fi

# ── 6e. Anomaly check — flag metrics that swung >10x since last iteration ────
if ! hook_event_has_step "anomaly_check"; then
  if [[ -f "$SCRIPT_DIR/hooks/post-agent.d/anomaly-check.sh" ]]; then
    source "$SCRIPT_DIR/hooks/post-agent.d/anomaly-check.sh" 2>/dev/null || true
  fi
  hook_event_mark_step "anomaly_check"
fi

# ── 6f. Worktree reaper — throttled to at most once per hour off this hot path
# Measured 2026-08-23 (N=198 spawns): running unconditionally after every
# spawn cost 16,460ms per call — ~165x the "< 100ms" this comment used to
# claim, and worst under exactly the "nothing to reap" condition it named
# (0 of 198 reaped). Throttled, not removed — see
# scripts/hooks/post-agent.d/reap-worktrees-throttle.sh (D#2155) for why a
# throttle instead of dropping the call, and why not a cron job instead.
# Non-fatal: failures must not block the hook exit code.
if ! hook_event_has_step "reap_worktrees"; then
  if [[ -f "$SCRIPT_DIR/hooks/post-agent.d/reap-worktrees-throttle.sh" ]]; then
    source "$SCRIPT_DIR/hooks/post-agent.d/reap-worktrees-throttle.sh" 2>/dev/null || true
  fi
  hook_event_mark_step "reap_worktrees"
fi

# ── 7. Log to team-log — TERSE one-liner (no tokens, no files) ───────────────
if ! hook_event_has_step "team_log"; then
  # Terse format: [HH:MM] role: verdict D#N PR#M
  # Full payload (tokens, files, model) is in agent-feed.jsonl
  MSG="[$(date +%H:%M)] $ROLE: $VERDICT"
  [[ -n "$DISCUSSION" ]] && MSG="$MSG D#$DISCUSSION"
  [[ -n "$PR" ]]         && MSG="$MSG PR#$PR"
  bash "$SCRIPT_DIR/rotate-team-log.sh" comment "$MSG" \
    || echo "[post-agent-hook] Warning: team-log comment failed (non-fatal)" >&2
  hook_event_mark_step "team_log"
fi

# ── 7b. Parent branch contamination recovery ─────────────────────────────────
# After any agent completes, check whether the parent repo's HEAD was contaminated
# (e.g. by an executor that ran git checkout inside a worktree). If so, auto-recover
# to main and log — same self-healing logic as pre-spawn-check.sh.
#
# Guard: skip if running from inside a linked worktree (non-main branch is intentional).
# Two signals, either one is sufficient:
#   1. $WORKTREE_ID env var — a dead fallback; nothing in the tree sets it
#      outside tests (verified 2026-08-17: unset inside a live worktree agent)
#   2. git-dir != git-common-dir — canonical linked-worktree test, and the
#      one that actually fires
_PAH_GIT_DIR=$(git -C "$REPO_ROOT" rev-parse --git-dir 2>/dev/null || true)
_PAH_GIT_COMMON_DIR=$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)
_PAH_IN_LINKED_WORKTREE=false
if [[ -n "${WORKTREE_ID:-}" ]] || [[ -n "$_PAH_GIT_DIR" && -n "$_PAH_GIT_COMMON_DIR" && "$_PAH_GIT_DIR" != "$_PAH_GIT_COMMON_DIR" ]]; then
  _PAH_IN_LINKED_WORKTREE=true
fi

if [[ "$_PAH_IN_LINKED_WORKTREE" == "false" ]]; then
  PARENT_BRANCH_HOOK=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "")
  if [[ -n "$PARENT_BRANCH_HOOK" && "$PARENT_BRANCH_HOOK" != "main" ]]; then
    echo "[post-agent-hook] Parent on '$PARENT_BRANCH_HOOK', auto-resetting to main (contamination recovery)" >&2
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] post-agent-hook: auto-recovered parent from contaminated branch '$PARENT_BRANCH_HOOK' → main (after $ROLE $VERDICT)" \
      2>/dev/null || true
    git -C "$REPO_ROOT" symbolic-ref HEAD refs/heads/main 2>/dev/null || true
    git -C "$REPO_ROOT" fetch origin main --quiet 2>/dev/null || true
    git -C "$REPO_ROOT" reset --hard origin/main 2>/dev/null || true
  fi
fi

hook_event_finish
echo "[post-agent-hook] Done."
