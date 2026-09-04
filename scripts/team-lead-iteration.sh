#!/usr/bin/env bash
# team-lead-iteration.sh — canonical /loop wrapper for the Team Lead.
#
# Runs all CLAUDE.md /loop steps in order. Integrates every team subsystem:
# blackboard, circuit_breaker, event_bus, workflow_runner, quality_scorer,
# audit_trail, agent_cards, spawn queue, and GitHub Discussions.
#
# Usage:
#   bash scripts/team-lead-iteration.sh           # normal run
#   bash scripts/team-lead-iteration.sh --dry-run # print actions, skip merges/label changes/writes
#
# Exit codes:
#   0  — iteration complete, no outstanding work
#   5  — spawn recommendations printed; caller must spawn then re-run
#   1  — fatal error (repo access failed, etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Put the project venv first on PATH so every bare `python3` call below (and in
# scripts this launches, e.g. loop-subsystem-snapshot.py) resolves to the
# interpreter with the project's dependencies installed, not whatever python3
# happens to be first for this process's inherited environment. No-op, not a
# failure, when .venv/ doesn't exist yet.
if [ -d "$REPO_ROOT/.venv/bin" ]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi

source "$SCRIPT_DIR/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"

DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    *) echo "Unknown argument: $arg" >&2; echo "Usage: $0 [--dry-run]" >&2; exit 1 ;;
  esac
done

log() { echo "[$(date +%H:%M:%S)] $*"; }
dry() {
  if [ "$DRY_RUN" = "true" ]; then
    echo "[DRY-RUN] $*"
  else
    eval "$*"
  fi
}

# ─────────────────────────────────────────────────────────────────
# Step 0.5 — Hook-event cleanup (remove done markers older than 7 days)
# ─────────────────────────────────────────────────────────────────
log "=== Step 0.5: Hook-event cleanup ==="
HOOK_DONE_DIR="$REPO_ROOT/.autonomous-team/hook-events/done"
if [[ -d "$HOOK_DONE_DIR" ]]; then
  REMOVED=$(find "$HOOK_DONE_DIR" -name "*.json" -mtime +7 -delete -print 2>/dev/null | wc -l || echo 0)
  log "Hook-event cleanup: removed $REMOVED stale done-markers (>7 days)"
else
  mkdir -p "$HOOK_DONE_DIR" 2>/dev/null || true
  log "Hook-event cleanup: done/ directory created"
fi

# ─────────────────────────────────────────────────────────────────
# Step 0.6 — Budget init (idempotent)
# ─────────────────────────────────────────────────────────────────
log "=== Step 0.6: Budget init ==="
if ! python3 "$REPO_ROOT/backend/budget.py" status > /dev/null 2>&1; then
  log "No session found — initializing budget"
  python3 "$REPO_ROOT/backend/budget.py" init
else
  log "Budget session already active"
fi

T_START=$(date +%s)
ITER_START_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ─────────────────────────────────────────────────────────────────
# Step 0.7 — Subsystem snapshot (single read of all subsystems)
# ─────────────────────────────────────────────────────────────────
log "=== Step 0.7: Subsystem snapshot ==="
# Write where the readers read. This used to be a PID-suffixed /tmp file that
# nobody opened and that got deleted at the end of the iteration, which is why
# the snapshot on disk was five days stale. No --no-drain here: the loop
# iteration is the one caller that *should* consume the event cursor.
SNAPSHOT_FILE="$(python3 "$REPO_ROOT/backend/snapshot_path.py")"
SNAPSHOT_TMP="${SNAPSHOT_FILE}.tmp.$$"
mkdir -p "$(dirname "$SNAPSHOT_FILE")"
if python3 "$SCRIPT_DIR/loop-subsystem-snapshot.py" --output "$SNAPSHOT_TMP" 2>/tmp/snapshot-stderr-$$ \
   && mv -f "$SNAPSHOT_TMP" "$SNAPSHOT_FILE"; then
  log "Snapshot written to $SNAPSHOT_FILE"
  SNAPSHOT_WARNINGS=$(jq -r '(.warnings // []) | join(" | ")' "$SNAPSHOT_FILE" 2>/dev/null || echo "")
  if [ -n "$SNAPSHOT_WARNINGS" ]; then
    log "Snapshot warnings: $SNAPSHOT_WARNINGS"
  fi
  EVENT_COUNT=$(jq '.events_drained | length' "$SNAPSHOT_FILE" 2>/dev/null || echo 0)
  DISC_COUNT=$(jq '.discussions | length' "$SNAPSHOT_FILE" 2>/dev/null || echo 0)
  QUEUE_DEPTH=$(jq '(.blackboard.queue_pending // []) | length' "$SNAPSHOT_FILE" 2>/dev/null || echo 0)
  log "Snapshot: events=$EVENT_COUNT discussions=$DISC_COUNT queue=$QUEUE_DEPTH"
else
  log "WARNING: loop-subsystem-snapshot.py failed — $(cat /tmp/snapshot-stderr-$$ 2>/dev/null | head -3)"
  # The stub goes to a scratch file, NOT to the canonical path. Overwriting the
  # canonical snapshot with an empty stub would destroy the fresh copy the
  # refresh timer just wrote and hand every other reader an all-zeros view of
  # the world. This iteration degrades; the readers do not.
  rm -f "$SNAPSHOT_TMP"
  SNAPSHOT_FILE="/tmp/loop-snapshot-stub-$$.json"
  printf '{"blackboard":{"memory_recent":[],"queue_pending":[],"queue_active":[],"budget":{}},"events_drained":[],"circuit_breaker":{"tripped_roles":[]},"discussions":[],"workflows_available":null,"agent_cards":null,"audit_recent_failures":[],"snapshot_at":""}\n' > "$SNAPSHOT_FILE"
  EVENT_COUNT=0; DISC_COUNT=0; QUEUE_DEPTH=0
fi
rm -f /tmp/snapshot-stderr-$$

# Load tripped roles for circuit-breaker checks later
TRIPPED_ROLES=$(jq -r '(.circuit_breaker.tripped_roles // []) | join(",")' "$SNAPSHOT_FILE" 2>/dev/null || echo "")

# ─────────────────────────────────────────────────────────────────
# Step 1 — Repo check
# ─────────────────────────────────────────────────────────────────
log "=== Step 1: Repo check ==="
REPO_INFO=$(gh repo view "$REPO" --json nameWithOwner,defaultBranchRef 2>/dev/null)
REPO_NAME=$(echo "$REPO_INFO" | jq -r '.nameWithOwner' 2>/dev/null || echo "unknown")
DEFAULT_BRANCH=$(echo "$REPO_INFO" | jq -r '.defaultBranchRef.name' 2>/dev/null || echo "main")
log "Repo: $REPO_NAME  default branch: $DEFAULT_BRANCH"

# ─────────────────────────────────────────────────────────────────
# Step 2 — Ensure team-log issue exists
# ─────────────────────────────────────────────────────────────────
log "=== Step 2: Team-log check ==="
LOG=$(gh issue list --label team-log --state open --json number --jq '.[0].number' --repo "$REPO" 2>/dev/null || echo "")
if [ -z "$LOG" ]; then
  log "WARNING: No team-log issue found. Create one with label 'team-log'."
else
  log "Team-log issue: #$LOG"
fi

# ─────────────────────────────────────────────────────────────────
# Step 3 — Discussion scan via subsystem snapshot
# Reads .discussions from the snapshot (GraphQL already done in 0.7)
# ─────────────────────────────────────────────────────────────────
log "=== Step 3: Discussion scan ==="
DISCUSSIONS_JSON=$(jq '.discussions // []' "$SNAPSHOT_FILE" 2>/dev/null || echo "[]")
DISC_TOTAL=$(echo "$DISCUSSIONS_JSON" | jq 'length' 2>/dev/null || echo 0)
log "Discussions in snapshot: $DISC_TOTAL"

NEW_DISC_SPEC_READY=()
NEW_DISC_DISCUSSING=()

# ── External-intake gate (D#1588 Batch A) ──────────────────────────────────
# Primary enforcement chokepoint: classify_and_label() re-derives provenance
# from LIVE author identity every iteration (labels are audit-only — this
# closes the scan-lag fail-open window, D#1588 panel Risk 3), applies exactly
# one of provenance:internal/provenance:external via GraphQL
# addLabelsToLabelable when the Discussion doesn't already carry one (REST
# issues/N/labels silently no-ops on Discussions — confirmed by the panel),
# and returns whether the Discussion is blocked (external + no intake-approved
# label). Blocked Discussions are excluded from NEW_DISC_SPEC_READY /
# NEW_DISC_DISCUSSING — inert to automation but still visible on GitHub
# (no censorship, just no compute spend until a human applies intake-approved).
_gate_check_discussion() {
  local num="$1"
  if [ "$DRY_RUN" = "true" ]; then
    echo "[DRY-RUN] would classify+label+gate-check D#$num" >&2
    echo '{"blocked":false,"reason":"dry_run"}'
    return 0
  fi
  python3 "$REPO_ROOT/scripts/lib/external_intake_gate.py" classify-and-label "$num" 2>/dev/null \
    || echo '{"blocked":true,"reason":"gate_check_failed"}'
}

if [ "$DISC_TOTAL" -gt 0 ]; then
  while IFS=$'\t' read -r num title status is_new; do
    log "  #$num [$status]: $title (new=$is_new)"

    GATE_JSON=$(_gate_check_discussion "$num")
    GATE_BLOCKED=$(echo "$GATE_JSON" | python3 -c "import sys,json; print(str(json.load(sys.stdin).get('blocked',True)).lower())" 2>/dev/null || echo "true")
    GATE_REASON=$(echo "$GATE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason',''))" 2>/dev/null || echo "gate_check_failed")

    if [ "$GATE_BLOCKED" = "true" ]; then
      log "    -> gated: awaiting intake-approved ($GATE_REASON) — skipping automation for D#$num"
      continue
    fi

    if [ "$is_new" = "true" ]; then
      case "$status" in
        SPEC_READY)
          NEW_DISC_SPEC_READY+=("$num:$title")
          log "    -> SPEC_READY: recommend executor"
          ;;
        DISCUSSING|UNKNOWN)
          NEW_DISC_DISCUSSING+=("$num:$title")
          log "    -> needs spec: recommend project-manager"
          ;;
        DONE|CLOSED)
          log "    -> already closed/done, skip"
          ;;
        *)
          log "    -> status '$status', no immediate action"
          ;;
      esac
    fi
  done < <(echo "$DISCUSSIONS_JSON" | jq -r '.[] | [(.number|tostring), .title, .status, (.is_new_since_last_run|tostring)] | @tsv' 2>/dev/null)
fi

# ─────────────────────────────────────────────────────────────────
# Step 4 — Open PR analysis
# ─────────────────────────────────────────────────────────────────
log "=== Step 4: Open PR analysis ==="
OPEN_PRS=$(gh pr list --state open --json number,title,labels --repo "$REPO" 2>/dev/null || echo "[]")
PR_COUNT=$(echo "$OPEN_PRS" | jq 'length' 2>/dev/null || echo 0)
log "Open PRs: $PR_COUNT"

NEEDS_REVIEW=()
NEEDS_MERGE=()
NEEDS_FIX=()
NEEDS_SECURITY_REVIEW=()

if [ "$PR_COUNT" -gt 0 ]; then
  while IFS=$'\t' read -r pr_num pr_title labels_json; do
    has_code_review=$(echo "$labels_json" | jq -r 'map(select(.name == "code-review-passed")) | length' 2>/dev/null || echo 0)
    has_needs_fix=$(echo "$labels_json" | jq -r 'map(select(.name | test("needs-fix|code-review-needs-fix"))) | length' 2>/dev/null || echo 0)
    has_security_triggered=$(echo "$labels_json" | jq -r 'map(select(.name == "security-review-triggered")) | length' 2>/dev/null || echo 0)
    has_security_passed=$(echo "$labels_json" | jq -r 'map(select(.name == "security-review-passed")) | length' 2>/dev/null || echo 0)

    if [ "$has_needs_fix" -gt 0 ]; then
      NEEDS_FIX+=("$pr_num:$pr_title")
      log "  PR #$pr_num needs-fix: $pr_title"
    elif [ "$has_code_review" -gt 0 ]; then
      if [ "$has_security_triggered" -gt 0 ] && [ "$has_security_passed" -eq 0 ]; then
        NEEDS_SECURITY_REVIEW+=("$pr_num:$pr_title")
        log "  PR #$pr_num awaiting security review: $pr_title"
      else
        NEEDS_MERGE+=("$pr_num:$pr_title")
        log "  PR #$pr_num ready to merge: $pr_title"
      fi
    else
      NEEDS_REVIEW+=("$pr_num:$pr_title")
      log "  PR #$pr_num needs code review: $pr_title"
    fi
  done < <(echo "$OPEN_PRS" | jq -r '.[] | "\(.number)\t\(.title)\t\(.labels|tojson)"' 2>/dev/null)
fi

# ─────────────────────────────────────────────────────────────────
# Step 4b — Stuck PR sweeper
# Detects PRs with code-review-needs-fix age >30min and re-routes them.
# Non-fatal: failure is logged but does not abort the iteration.
# ─────────────────────────────────────────────────────────────────
log "=== Step 4b: Stuck PR sweep ==="
bash "$SCRIPT_DIR/sweep-stuck-prs.sh" 2>&1 | while IFS= read -r line; do log "  $line"; done || true

# ─────────────────────────────────────────────────────────────────
# Step 5.0 — Orphan worktree scan
# ─────────────────────────────────────────────────────────────────
log "=== Step 5.0: Orphan worktree scan ==="
bash "$SCRIPT_DIR/scan-orphan-worktrees.sh" || true

# ─────────────────────────────────────────────────────────────────
# Step 5.1 — Spawn queue drain
# Read .autonomous-team/spawn-queue.json and recommend spawns,
# skipping any role that circuit_breaker has tripped.
# ─────────────────────────────────────────────────────────────────
log "=== Step 5.1: Spawn queue drain ==="
QUEUE_ITEMS=$(jq '.blackboard.queue_pending // []' "$SNAPSHOT_FILE" 2>/dev/null || echo "[]")
QUEUE_LEN=$(echo "$QUEUE_ITEMS" | jq 'length' 2>/dev/null || echo 0)
log "Pending spawn queue items: $QUEUE_LEN"

QUEUE_SPAWN_RECOMMENDATIONS=()

if [ "$QUEUE_LEN" -gt 0 ]; then
  while IFS=$'\t' read -r qid qrole qdisc qpr; do
    role_tripped=false
    if [ -n "$TRIPPED_ROLES" ] && echo "$TRIPPED_ROLES" | grep -qF "$qrole"; then
      log "  SKIP $qid — role '$qrole' is circuit-breaker tripped"
      role_tripped=true
    fi
    if [ "$role_tripped" = "false" ]; then
      log "  Queue item $qid: role=$qrole disc=$qdisc pr=$qpr"
      QUEUE_SPAWN_RECOMMENDATIONS+=("$qrole:$qdisc:$qpr")
    fi
  done < <(echo "$QUEUE_ITEMS" | jq -r '.[] | [.id, .role, (.discussion // "null"|tostring), (.pr // "null"|tostring)] | @tsv' 2>/dev/null)
fi

# ─────────────────────────────────────────────────────────────────
# Step 5.2 — Workflow resolution
# Call workflow_runner.resolve before every impl or review spawn.
# workflow_runner is the single source of truth for spawn dispatch.
# ─────────────────────────────────────────────────────────────────
log "=== Step 5.2: Workflow resolution ==="

_resolve_workflow() {
  local kind="$1"; shift
  local wf_out
  if wf_out=$(python3 "$REPO_ROOT/backend/workflow_runner.py" resolve "$kind" "$@" 2>&1); then
    echo "$wf_out"
    return 0
  else
    log "  WARNING: workflow_runner resolve $kind failed: $wf_out"
    return 1
  fi
}

# Resolve implement-discussion for SPEC_READY discussions
for disc_entry in "${NEW_DISC_SPEC_READY[@]+"${NEW_DISC_SPEC_READY[@]}"}"; do
  disc_num="${disc_entry%%:*}"
  disc_title="${disc_entry#*:}"
  log "  Resolving implement-discussion for #$disc_num"
  disc_url="https://github.com/$REPO/discussions/$disc_num"
  _resolve_workflow implement-discussion \
    --input "discussion_number=$disc_num" \
    --input "discussion_title=$disc_title" \
    --input "discussion_url=$disc_url" \
    --input "spec_body=<read from discussion>" 2>/dev/null || \
    log "  workflow_runner unavailable for #$disc_num — defaulting to executor"
done

# Resolve review-pr for PRs needing code review
for pr_entry in "${NEEDS_REVIEW[@]+"${NEEDS_REVIEW[@]}"}"; do
  pr_num="${pr_entry%%:*}"
  log "  Resolving review-pr for PR #$pr_num"
  # Try to extract the real discussion number from the PR body (e.g. "Discussion #42" or "Closes #42").
  # If none is found, omit --input discussion_number entirely — workflow_runner will WARN and fall back.
  disc_num=$(gh pr view "$pr_num" --repo "$REPO" --json body --jq '.body // ""' 2>/dev/null \
    | grep -oE 'Discussion #[0-9]+' | head -1 | grep -oE '[0-9]+' || true)
  if [[ -n "$disc_num" ]]; then
    _resolve_workflow review-pr \
      --input "pr_number=$pr_num" \
      --input "discussion_number=$disc_num" 2>/dev/null || true
  else
    # No linked Discussion — omit the argument; workflow_runner will WARN and fall back gracefully.
    _resolve_workflow review-pr --input "pr_number=$pr_num" 2>/dev/null || true
  fi
done

# ─────────────────────────────────────────────────────────────────
# Step 5.3 — Quality gate
# PRs with code-review-passed are re-scored. Score < 60 blocks merge.
# (Skipped in --dry-run: no label changes)
# ─────────────────────────────────────────────────────────────────
log "=== Step 5.3: Quality gate ==="

FILTERED_MERGE=()
for pr_entry in "${NEEDS_MERGE[@]+"${NEEDS_MERGE[@]}"}"; do
  pr_num="${pr_entry%%:*}"
  pr_title="${pr_entry#*:}"
  [ -z "$pr_num" ] && continue

  log "  Scoring PR #$pr_num: $pr_title"
  SCORE_JSON=$(python3 "$REPO_ROOT/backend/quality_scorer.py" score --pr "$pr_num" --cache-ttl-sec 600 2>/dev/null || echo '{}')
  SCORE=$(echo "$SCORE_JSON" | jq -r '.total_score // 0' 2>/dev/null || echo 0)
  GRADE=$(echo "$SCORE_JSON" | jq -r '.grade // "?"' 2>/dev/null || echo "?")
  IS_APPLICABLE=$(echo "$SCORE_JSON" | jq -r '.applicable // true' 2>/dev/null || echo "true")
  log "  PR #$pr_num quality score: $SCORE ($GRADE) applicable=$IS_APPLICABLE"

  # Skip the quality gate entirely for non-Python/non-scorable diffs (markdown,
  # shell, config, bootstrap snapshots) — a score of 0 for these is meaningless.
  if [ "$IS_APPLICABLE" = "false" ]; then
    log "  PR #$pr_num: quality gate skipped (applicable=false — no scorable files)"
    FILTERED_MERGE+=("$pr_entry")
    continue
  fi

  HAS_ERROR=$(echo "$SCORE_JSON" | jq -e '.error' > /dev/null 2>&1 && echo "yes" || echo "no")

  # Skip gate entirely if the scorer errored or returned zero with no Python content.
  # A zero score on a markdown-only PR is not a real quality failure — the scorer
  # just has nothing to evaluate.
  NO_PYTHON_SCORED="no"
  if [ "$SCORE" -eq 0 ] 2>/dev/null; then
    _complexity_detail=$(echo "$SCORE_JSON" | jq -r '.breakdown.complexity.detail // ""' 2>/dev/null || true)
    if echo "$_complexity_detail" | grep -q "no Python files"; then
      NO_PYTHON_SCORED="yes"
    fi
  fi

  if [ "$HAS_ERROR" = "yes" ] || [ "$NO_PYTHON_SCORED" = "yes" ]; then
    log "  [quality-gate] PR #$pr_num: skipping quality gate (no scorable content) — score=$SCORE has_error=$HAS_ERROR no_python=$NO_PYTHON_SCORED"
    FILTERED_MERGE+=("$pr_entry")
    continue
  fi

  IS_LOW_SCORE=false
  if [ "$SCORE" -lt 60 ] 2>/dev/null; then
    IS_LOW_SCORE=true
  fi

  if [ "$IS_LOW_SCORE" = "true" ]; then
    # Build list of failing dimensions, excluding too_many_review_rounds —
    # that is a process signal about review history, not something an executor can fix.
    FAILING_DIMS=$(echo "$SCORE_JSON" | jq -r '
      [
        (if ((.breakdown.complexity.score // 30) < 20) then "complexity" else empty end),
        (if ((.breakdown.test_coverage.score // 25) < 15) then "test_coverage" else empty end),
        (if ((.breakdown.size.score // 20) < 10) then "size" else empty end)
      ] | if length == 0 then "below threshold" else join(", ") end
    ' 2>/dev/null || echo "below threshold")

    # If the only thing dragging the score down was too_many_review_rounds, skip —
    # there is nothing actionable for the executor.
    if [ "$FAILING_DIMS" = "below threshold" ]; then
      _rounds_score=$(echo "$SCORE_JSON" | jq -r '.breakdown.review_rounds.score // 25' 2>/dev/null || echo 25)
      if [ "$_rounds_score" -lt 15 ] 2>/dev/null; then
        log "  [quality-gate] PR #$pr_num: skipping quality gate (only too_many_review_rounds — not executor-actionable)"
        FILTERED_MERGE+=("$pr_entry")
        continue
      fi
    fi

    # Build a per-dimension detail block from the scorer's own breakdown.detail fields
    # so the executor gets file:line / function names, not just a dimension label.
    QG_DETAIL=$(echo "$SCORE_JSON" | jq -r '
      .breakdown as $bd |
      [
        (if (($bd.complexity.score // 30) < 20) then
          "- **complexity** \($bd.complexity.score // 0)/30: \($bd.complexity.detail // "see Python files")"
        else empty end),
        (if (($bd.test_coverage.score // 25) < 15) then
          "- **test_coverage** \($bd.test_coverage.score // 0)/25: \($bd.test_coverage.detail // "check module coverage")"
        else empty end),
        (if (($bd.size.score // 20) < 10) then
          "- **size** \($bd.size.score // 0)/20: \($bd.size.detail // "diff too large")"
        else empty end)
      ] | join("\n")
    ' 2>/dev/null || echo "- see quality_scorer output for details")

    if [ "$DRY_RUN" = "false" ]; then
      # Use REST API here. Not because gh pr edit --add-label "silently no-ops
      # on this repo" — that was folklore, debunked and removed (D#2031/D#2045,
      # wiki/Team-Lead-Operations.md). REST is kept for reasons unrelated to
      # that claim (Projects classic deprecation).
      _qg_all_ok=true
      _qg_errors_dir="${REPO_ROOT}/.autonomous-team/hook-events"
      mkdir -p "$_qg_errors_dir"

      gh api -X DELETE "repos/${REPO}/issues/${pr_num}/labels/code-review-passed" >/dev/null 2>&1
      _rc_del=$?
      if [ "$_rc_del" -ne 0 ]; then
        echo "[quality-gate] FAIL pr=#${pr_num} op=delete-label rc=${_rc_del}" >&2
        printf '{"ts":"%s","pr":%s,"op":"delete-label","rc":%s}\n' \
          "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$pr_num" "$_rc_del" \
          >> "${_qg_errors_dir}/quality-gate-errors.jsonl"
        _qg_all_ok=false
      fi

      gh api -X POST "repos/${REPO}/issues/${pr_num}/labels" -f labels[]="code-review-needs-fix" >/dev/null 2>&1
      _rc_add=$?
      if [ "$_rc_add" -ne 0 ]; then
        echo "[quality-gate] FAIL pr=#${pr_num} op=add-label rc=${_rc_add}" >&2
        printf '{"ts":"%s","pr":%s,"op":"add-label","rc":%s}\n' \
          "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$pr_num" "$_rc_add" \
          >> "${_qg_errors_dir}/quality-gate-errors.jsonl"
        _qg_all_ok=false
      fi

      _qg_comment_body="Quality score ${SCORE}/100 is below threshold (60). Failing dimensions: ${FAILING_DIMS}.

${QG_DETAIL}

PR requires another fix round before merge."
      gh pr comment "$pr_num" --body "$_qg_comment_body" --repo "$REPO" 2>/dev/null
      _rc_comment=$?
      if [ "$_rc_comment" -ne 0 ]; then
        echo "[quality-gate] FAIL pr=#${pr_num} op=post-comment rc=${_rc_comment}" >&2
        printf '{"ts":"%s","pr":%s,"op":"post-comment","rc":%s}\n' \
          "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$pr_num" "$_rc_comment" \
          >> "${_qg_errors_dir}/quality-gate-errors.jsonl"
        _qg_all_ok=false
      fi

      if [ "$_qg_all_ok" = "true" ]; then
        log "  Labels updated and comment posted for PR #$pr_num"
      else
        log "  [quality-gate] PR #$pr_num: one or more gh calls failed (see stderr + quality-gate-errors.jsonl)"
      fi
    else
      log "  [DRY-RUN] Would remove code-review-passed and add code-review-needs-fix on PR #$pr_num (score=$SCORE, failing: $FAILING_DIMS)"
    fi
    # Do not add to FILTERED_MERGE — blocked from merge
  else
    FILTERED_MERGE+=("$pr_entry")
  fi
done
NEEDS_MERGE=("${FILTERED_MERGE[@]+"${FILTERED_MERGE[@]}"}")

# ─────────────────────────────────────────────────────────────────
# Step 5 — Spawn recommendations
# Recommendations come from workflow-resolved plans above.
# ─────────────────────────────────────────────────────────────────
log "=== Step 5: Spawn recommendations ==="

SPAWN_NEEDED=false

# PRs needing code review (workflow: review-pr -> code-reviewer)
for pr_entry in "${NEEDS_REVIEW[@]+"${NEEDS_REVIEW[@]}"}"; do
  pr_num="${pr_entry%%:*}"
  pr_title="${pr_entry#*:}"
  printf '\nSPAWN RECOMMENDATION:\n'
  printf '  role:          code-reviewer\n'
  printf '  pr:            #%s\n' "$pr_num"
  printf '  title:         %s\n' "$pr_title"
  printf '  subagent_type: code-reviewer\n'
  printf '  isolation:     worktree\n'
  printf '  workflow:      review-pr\n'
  printf '  pre_spawn:     bash scripts/pre-spawn-check.sh --role code-reviewer --pr %s --event-id "code-reviewer-pr%s-$(date +%%s)"\n' "$pr_num" "$pr_num"
  SPAWN_NEEDED=true
done

# PRs needing security review
for pr_entry in "${NEEDS_SECURITY_REVIEW[@]+"${NEEDS_SECURITY_REVIEW[@]}"}"; do
  pr_num="${pr_entry%%:*}"
  pr_title="${pr_entry#*:}"
  printf '\nSPAWN RECOMMENDATION:\n'
  printf '  role:          security-reviewer\n'
  printf '  pr:            #%s\n' "$pr_num"
  printf '  title:         %s\n' "$pr_title"
  printf '  subagent_type: security-reviewer\n'
  printf '  isolation:     worktree\n'
  printf '  pre_spawn:     bash scripts/pre-spawn-check.sh --role security-reviewer --pr %s --event-id "security-reviewer-pr%s-$(date +%%s)"\n' "$pr_num" "$pr_num"
  SPAWN_NEEDED=true
done

# PRs needing fixes — route back to executor
for pr_entry in "${NEEDS_FIX[@]+"${NEEDS_FIX[@]}"}"; do
  pr_num="${pr_entry%%:*}"
  pr_title="${pr_entry#*:}"
  printf '\nSPAWN RECOMMENDATION:\n'
  printf '  role:          executor\n'
  printf '  pr:            #%s (fix round)\n' "$pr_num"
  printf '  title:         %s\n' "$pr_title"
  printf '  subagent_type: executor\n'
  printf '  isolation:     worktree\n'
  printf '  pre_spawn:     bash scripts/pre-spawn-check.sh --role executor --pr %s --event-id "executor-pr%s-$(date +%%s)"\n' "$pr_num" "$pr_num"
  SPAWN_NEEDED=true
done

# SPEC_READY discussions (workflow: implement-discussion -> executor)
for disc_entry in "${NEW_DISC_SPEC_READY[@]+"${NEW_DISC_SPEC_READY[@]}"}"; do
  disc_num="${disc_entry%%:*}"
  disc_title="${disc_entry#*:}"
  printf '\nSPAWN RECOMMENDATION:\n'
  printf '  role:          executor\n'
  printf '  discussion:    #%s\n' "$disc_num"
  printf '  title:         %s\n' "$disc_title"
  printf '  subagent_type: executor\n'
  printf '  isolation:     worktree\n'
  printf '  workflow:      implement-discussion\n'
  printf '  pre_spawn:     bash scripts/pre-spawn-check.sh --role executor --discussion %s --event-id "executor-disc%s-$(date +%%s)"\n' "$disc_num" "$disc_num"
  SPAWN_NEEDED=true
done

# DISCUSSING discussions — need a spec
for disc_entry in "${NEW_DISC_DISCUSSING[@]+"${NEW_DISC_DISCUSSING[@]}"}"; do
  disc_num="${disc_entry%%:*}"
  disc_title="${disc_entry#*:}"
  printf '\nSPAWN RECOMMENDATION:\n'
  printf '  role:          project-manager\n'
  printf '  discussion:    #%s\n' "$disc_num"
  printf '  title:         %s\n' "$disc_title"
  printf '  subagent_type: project-manager\n'
  printf '  isolation:     worktree\n'
  printf '  pre_spawn:     bash scripts/pre-spawn-check.sh --role project-manager --discussion %s --event-id "project-manager-disc%s-$(date +%%s)"\n' "$disc_num" "$disc_num"
  SPAWN_NEEDED=true
done

# Queued spawn items (drained in step 5.1)
for rec in "${QUEUE_SPAWN_RECOMMENDATIONS[@]+"${QUEUE_SPAWN_RECOMMENDATIONS[@]}"}"; do
  qrole="${rec%%:*}"
  rest="${rec#*:}"
  qdisc="${rest%%:*}"
  qpr="${rest#*:}"
  printf '\nSPAWN RECOMMENDATION (from queue):\n'
  printf '  role:          %s\n' "$qrole"
  [ "$qdisc" != "null" ] && printf '  discussion:    #%s\n' "$qdisc"
  [ "$qpr"   != "null" ] && printf '  pr:            #%s\n' "$qpr"
  printf '  subagent_type: %s\n' "$qrole"
  printf '  isolation:     worktree\n'
  printf '  pre_spawn:     bash scripts/pre-spawn-check.sh --role %s --event-id "%s-$(date +%%s)"\n' "$qrole" "$qrole"
  SPAWN_NEEDED=true
done

if [ "$SPAWN_NEEDED" = "true" ]; then
  printf '\n'
  log "Work available — spawn the recommended agents above using pre-spawn-check.sh, then re-run."
  printf '\n'
  printf 'IMPORTANT:\n'
  printf '  1. For EVERY spawn, run bash scripts/pre-spawn-check.sh first.\n'
  printf '     If it exits non-zero, skip that spawn (circuit breaker or budget block).\n'
  printf '  2. After running pre-spawn-check.sh, call workflow_runner.py resolve\n'
  printf '     to get the authoritative spawn role before dispatching.\n'
  printf '  3. ALWAYS use named subagent_types: executor, code-reviewer, security-reviewer,\n'
  printf '     project-manager, acceptance-tester.\n'
  printf '     NEVER use general-purpose for project work.\n'
  printf '  4. Pass isolation: worktree when agents touch the same files in parallel.\n'
  printf '  5. After each agent completes, run bash scripts/post-agent-hook.sh.\n'
  exit 5
fi

log "No spawn-eligible work found at this time."

# ─────────────────────────────────────────────────────────────────
# Step 6 — Auto-merge check
# ─────────────────────────────────────────────────────────────────
log "=== Step 6: Auto-merge check ==="

AUTO_MERGE_GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.auto_merge 2>/dev/null | tr -d '"' || echo "true")
HUMAN_GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.human_approval_before_merge 2>/dev/null | tr -d '"' || echo "false")

if [ "$AUTO_MERGE_GATE" = "false" ]; then
  log "auto_merge gate is off — skipping merge step"
elif [ "$HUMAN_GATE" = "true" ]; then
  BOSS=$(python3 "$REPO_ROOT/backend/control_plane.py" get boss_github_username 2>/dev/null | tr -d '"' || echo "boss")
  log "human_approval_before_merge is on — posting wait comment, not merging"
  for pr_entry in "${NEEDS_MERGE[@]+"${NEEDS_MERGE[@]}"}"; do
    pr_num="${pr_entry%%:*}"
    [ -z "$pr_num" ] && continue
    dry "gh pr comment $pr_num --body 'All reviews passed. Waiting for @$BOSS to merge.' --repo $REPO"
  done
else
  for pr_entry in "${NEEDS_MERGE[@]+"${NEEDS_MERGE[@]}"}"; do
    pr_num="${pr_entry%%:*}"
    pr_title="${pr_entry#*:}"
    [ -z "$pr_num" ] && continue

    # Double-check labels before merging
    LABELS=$(gh pr view "$pr_num" --repo "$REPO" --json labels --jq '[.labels[].name]' 2>/dev/null || echo "[]")
    has_code_review=$(echo "$LABELS" | jq -r '[.[] | select(. == "code-review-passed")] | length' 2>/dev/null || echo 0)

    if [ "$has_code_review" -gt 0 ]; then
      log "Merging PR #$pr_num: $pr_title"
      dry "gh pr merge $pr_num --squash --delete-branch --repo $REPO"
      if [ "$DRY_RUN" = "false" ]; then
        PR_BODY=$(gh pr view "$pr_num" --repo "$REPO" --json body --jq '.body' 2>/dev/null || echo "")
        DISC_NUM=$(echo "$PR_BODY" | grep -oE 'Closes #([0-9]+)|Discussion #([0-9]+)' | grep -oE '[0-9]+' | head -1 || echo "")
        bash "$SCRIPT_DIR/post-merge-hook.sh" --pr "$pr_num" ${DISC_NUM:+--discussion "$DISC_NUM"}
      fi
    fi
  done
fi

# ─────────────────────────────────────────────────────────────────
# Step 7 — Heartbeat
# ─────────────────────────────────────────────────────────────────
log "=== Step 7: Heartbeat ==="
log "Heartbeat check complete (PM heartbeat recorded in step 7.5 team-log post)"

# ─────────────────────────────────────────────────────────────────
# Step 7.5 — Subsystem sweep (ALL subsystems exercised every iteration)
# ─────────────────────────────────────────────────────────────────
log "=== Step 7.5: Subsystem sweep ==="

BUDGET_STATUS=$(python3 "$REPO_ROOT/backend/budget.py" status 2>/dev/null || echo '{"error":"budget unavailable"}')
BUDGET_LINE=$(echo "$BUDGET_STATUS" | jq -r '"budget \(.spent // 0)/\(.ceiling // 0)"' 2>/dev/null || echo "budget N/A")
log "$BUDGET_LINE"

COST_STATUS=$(python3 "$REPO_ROOT/backend/cost_tracker.py" summary 2>/dev/null || echo '{"error":"cost_tracker unavailable"}')
COST_LINE=$(echo "$COST_STATUS" | jq -r '.summary // "unavailable"' 2>/dev/null || echo "unavailable")
log "Cost: $COST_LINE"

# ── Cost spike detection (Discussion #540 metric #22) ────────────────────
# Compute the per-iteration agent cost from budget/agents/ records finished in
# this iteration window, record it as 'iteration_cost_usd', then run 3σ spike
# detection.  On spike: increment counter; on 3 consecutive spikes in 1h, trip
# gates.budget_check=true as a defensive throttle.
if [ "$DRY_RUN" = "false" ]; then
  SPIKE_RESULT=$(python3 -c "
import sys, json
sys.path.insert(0, '$REPO_ROOT')
from backend.blackboard import Blackboard
from backend.cost_tracker import _compute_cost, _load_pricing, detect_cost_spike
from backend.stats_writer import record_iteration_cost, record_cost_spike
from datetime import datetime, timezone, timedelta

bb = Blackboard()
pricing = _load_pricing()
cutoff = datetime.now(timezone.utc) - timedelta(seconds=700)  # ~1 iteration + buffer

keys = bb.list_keys('budget/agents/')
iter_cost = 0.0
for key in keys:
    rec = bb.read(key)
    if not isinstance(rec, dict):
        continue
    fin = rec.get('finished', '')
    if not fin:
        continue
    try:
        ts = datetime.fromisoformat(fin.replace('Z', '+00:00'))
    except ValueError:
        continue
    if ts < cutoff:
        continue
    cost = _compute_cost(
        int(rec.get('input', 0)),
        int(rec.get('output', 0)),
        rec.get('model', 'default') or 'default',
        pricing,
        cache_read_tokens=int(rec.get('cache_read_tokens', 0)),
        cache_write_tokens=int(rec.get('cache_write_tokens', 0)),
    )
    iter_cost += cost

# Record the iteration cost data point (feeds future spike detection)
record_iteration_cost(iter_cost)

# Run spike detection
result = detect_cost_spike()
result['iter_cost'] = round(iter_cost, 6)

if result.get('spike') and not result.get('insufficient_data'):
    record_cost_spike(result['value'], result['mu'], result['sigma'])

print(json.dumps(result))
" 2>/dev/null || echo '{"spike":false,"insufficient_data":true,"iter_cost":0}')

  SPIKE=$(echo "$SPIKE_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('spike',False)).lower())" 2>/dev/null || echo "false")
  INSUF=$(echo "$SPIKE_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('insufficient_data',True)).lower())" 2>/dev/null || echo "true")
  ITER_COST=$(echo "$SPIKE_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('iter_cost',0))" 2>/dev/null || echo "0")

  if [ "$SPIKE" = "true" ] && [ "$INSUF" = "false" ]; then
    SPIKE_MU=$(echo "$SPIKE_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('mu',0))" 2>/dev/null || echo "0")
    SPIKE_SIGMA=$(echo "$SPIKE_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sigma',0))" 2>/dev/null || echo "0")
    SPIKE_THRESH=$(echo "$SPIKE_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('threshold',0))" 2>/dev/null || echo "0")
    log "WARNING: cost spike detected — iter_cost=\$${ITER_COST} threshold=\$${SPIKE_THRESH} (mu=\$${SPIKE_MU} sigma=\$${SPIKE_SIGMA})"
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment "[$(date +%H:%M)] team-lead: WARNING cost spike \$${ITER_COST} > threshold \$${SPIKE_THRESH} (3σ)" 2>/dev/null || true

    # Check for 3 consecutive spikes in the last 1h → trip gates.budget_check
    SPIKE_COUNT=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from backend.stats_writer import cost_spike_history
spikes = cost_spike_history(hours=1)
print(len(spikes))
" 2>/dev/null || echo "0")

    if [ "${SPIKE_COUNT:-0}" -ge 3 ]; then
      log "ERROR: 3 cost spikes in 1h (count=$SPIKE_COUNT) — tripping gates.budget_check=true"
      python3 "$REPO_ROOT/backend/control_plane.py" set gates.budget_check true 2>/dev/null || true
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment "[$(date +%H:%M)] team-lead: ALERT cost-spike-auto-trip — 3 spikes in 1h; gates.budget_check=true (defensive throttle)" 2>/dev/null || true
    fi
  else
    log "Cost spike check: iter_cost=\$${ITER_COST} — no spike"
  fi
fi

KPI_STATUS=$(python3 "$REPO_ROOT/backend/kpi_engine.py" show 2>/dev/null || echo '{"error":"kpi unavailable"}')
KPI_LINE=$(echo "$KPI_STATUS" | jq -r '.tasks_per_24h // "N/A"' 2>/dev/null | head -1 || echo "N/A")
log "KPI: $KPI_LINE tasks/24h"

QUALITY_STATUS=$(python3 "$REPO_ROOT/backend/quality_scorer.py" stats 2>/dev/null || echo '{"error":"quality_scorer unavailable"}')
QUALITY_LINE=$(echo "$QUALITY_STATUS" | jq -r '.avg_total // "N/A"' 2>/dev/null || echo "N/A")
log "Quality avg: $QUALITY_LINE/100"

python3 "$REPO_ROOT/backend/health_monitor.py" check 2>/dev/null || log "WARNING: health_monitor check failed"

log "Audit trail recent activity (last 10):"
python3 "$REPO_ROOT/backend/audit_trail.py" tail --n 10 2>/dev/null || log "WARNING: audit_trail tail failed"
python3 "$REPO_ROOT/backend/audit_trail.py" stats 2>/dev/null || log "WARNING: audit_trail stats failed"

# Event bus — read agent-feed.jsonl (event_bus has no standalone drain CLI;
# the FileAppender writes to this file from within the server process)
AGENT_FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
if [ -f "$AGENT_FEED" ]; then
  FEED_LINES=$(wc -l < "$AGENT_FEED" 2>/dev/null || echo 0)
  log "Event bus (agent-feed.jsonl): $FEED_LINES events on disk"
  tail -3 "$AGENT_FEED" 2>/dev/null | while IFS= read -r line; do
    parsed=$(echo "$line" | jq -r '"\(.timestamp // "?") \(.agent_role // "?") \(.event_subtype // "?")"' 2>/dev/null || echo "$line")
    log "  event: $parsed"
  done
else
  log "Event bus: agent-feed.jsonl not present (server not running or no events yet)"
fi

# Report audit failures from snapshot
AUDIT_FAILS=$(jq '(.audit_recent_failures // []) | length' "$SNAPSHOT_FILE" 2>/dev/null || echo 0)
if [ "$AUDIT_FAILS" -gt 0 ]; then
  log "WARNING: $AUDIT_FAILS recent failed audit entries:"
  jq -r '.audit_recent_failures // [] | .[] | "  FAIL: \(.actor // "?") \(.action // "?") \(.source // "?")"' "$SNAPSHOT_FILE" 2>/dev/null || true
fi

# Stats freshness watchdog — warn when any metric_event row is older than 2h
python3 "$REPO_ROOT/backend/stats_freshness_watchdog.py" check --file-bugs 2>/dev/null || true

# Worktree state watcher — detective for D#630 preventive fix (non-fatal)
python3 "$REPO_ROOT/backend/worktree_state_watcher.py" scan 2>&1 | head -20 || \
  bash "$SCRIPT_DIR/rotate-team-log.sh" comment "[$(date +%H:%M)] team-lead: WARN worktree_state_watcher failed (non-fatal)" 2>/dev/null || true

# Append metrics to loop-metrics.jsonl via the shared helper (single source of truth).
T_END=$(date +%s)
ITER_END_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DURATION=$((T_END - T_START))

# Compute scan_to_spawn_ratio over the last 24h window from loop-metrics.jsonl.
# Definition: (iterations_with_scan_no_spawn) / (total_iterations) over last 24h.
# Emit metric to stats_writer as well.
SCAN_TO_SPAWN_RATIO=$(python3 -c "
import json, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

metrics_file = Path('$REPO_ROOT/.autonomous-team/loop-metrics.jsonl')
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
" 2>/dev/null || echo "null")

# Emit scan_to_spawn_ratio to stats_writer (best-effort)
if [[ "$SCAN_TO_SPAWN_RATIO" != "null" ]] && [[ -n "$SCAN_TO_SPAWN_RATIO" ]] && [[ "$DRY_RUN" = "false" ]]; then
  python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from backend.stats_writer import record
record('scan_to_spawn_ratio', float('$SCAN_TO_SPAWN_RATIO'), 'ratio',
       tags={'window_hours': '24'}, source='team-lead-iteration')
" 2>/dev/null || true
fi

APPEND_ARGS=(
  --iter-start-iso    "$ITER_START_ISO"
  --iter-end-iso      "$ITER_END_ISO"
  --duration-seconds  "$DURATION"
  --event-count       "$EVENT_COUNT"
  --queue-depth       "$QUEUE_DEPTH"
  --discussion-count  "$DISC_COUNT"
  --pr-count          "$PR_COUNT"
  --needs-review      "${#NEEDS_REVIEW[@]}"
  --needs-merge       "${#NEEDS_MERGE[@]}"
  --needs-fix         "${#NEEDS_FIX[@]}"
)

# Include scan_to_spawn_ratio in loop-metrics row if available
if [[ "$SCAN_TO_SPAWN_RATIO" != "null" ]] && [[ -n "$SCAN_TO_SPAWN_RATIO" ]]; then
  APPEND_ARGS+=(--scan-to-spawn-ratio "$SCAN_TO_SPAWN_RATIO")
fi

if [ "$DRY_RUN" = "false" ]; then
  bash "$SCRIPT_DIR/append-loop-metrics.sh" "${APPEND_ARGS[@]}" 2>/dev/null \
    && log "Metrics appended to .autonomous-team/loop-metrics.jsonl" \
    || log "WARNING: append-loop-metrics.sh failed — metrics row not written"
else
  DRY_ROW=$(bash "$SCRIPT_DIR/append-loop-metrics.sh" "${APPEND_ARGS[@]}" --dry-run true 2>/dev/null \
    || echo '{}')
  log "[DRY-RUN] Would append metrics: $(echo "$DRY_ROW" | jq -c . 2>/dev/null || echo "$DRY_ROW")"
fi

# Log full status to team-log
if [ -n "$LOG" ]; then
  STATUS_MSG="[$(date +%H:%M)] team-lead: $BUDGET_LINE | cost $COST_LINE | KPI $KPI_LINE tasks/24h | quality $QUALITY_LINE/100 | PRs: $PR_COUNT open (${#NEEDS_MERGE[@]} ready, ${#NEEDS_REVIEW[@]} need-review) | events=$EVENT_COUNT queue=$QUEUE_DEPTH | ${DURATION}s"
  if [ "$DRY_RUN" = "false" ]; then
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment "$STATUS_MSG" 2>/dev/null || log "WARNING: team-log comment failed"
  else
    log "[DRY-RUN] Would post to team-log: $STATUS_MSG"
  fi
fi

# The canonical snapshot is deliberately NOT deleted here. It is the artefact
# seven readers depend on; deleting it at end-of-iteration is what left them
# reading a file nothing ever wrote. loop-snapshot-refresh.timer keeps it fresh
# between iterations. Only the failure-path scratch stub gets cleaned up.
rm -f "/tmp/loop-snapshot-stub-$$.json"

# ─────────────────────────────────────────────────────────────────
# Step 8 — Update now.md
# ─────────────────────────────────────────────────────────────────
log "=== Step 8: Update now.md ==="
NOW_FILE="$REPO_ROOT/.autonomous-team/now.md"
NOW_BLOCK="

---
<!-- loop-iteration: $(date -u +%Y-%m-%dT%H:%M:%SZ) -->
**Last loop:** $(date -u '+%Y-%m-%d %H:%M UTC')
- Open PRs: $PR_COUNT (${#NEEDS_MERGE[@]} ready-to-merge, ${#NEEDS_REVIEW[@]} need-review, ${#NEEDS_FIX[@]} needs-fix)
- Events drained: $EVENT_COUNT | Queue depth: $QUEUE_DEPTH | Discussions scanned: $DISC_COUNT
- Duration: ${DURATION}s
- Dry-run: $DRY_RUN
"

if [ "$DRY_RUN" = "false" ]; then
  printf '%s\n' "$NOW_BLOCK" >> "$NOW_FILE"
  log "now.md updated"
else
  log "[DRY-RUN] Would append to now.md: $NOW_BLOCK"
fi

log "=== Iteration complete ==="
exit 0
