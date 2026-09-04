#!/usr/bin/env bash
# post-merge-hook.sh — run after EVERY PR merge to enforce coordination discipline.
#
# Usage:
#   bash scripts/post-merge-hook.sh --pr <N> [--discussion <N>] [--event-id <id>] [--resume]
#
# Idempotent: pass the same --event-id twice and the second call is a no-op.
# Crash-safe: re-run with the same --event-id to resume from where it stopped.
#
# Steps (in order):
#   agent_feed → wiki_sync → discussion_close → team_log → auto_pull

set -uo pipefail

# ── Shared umbrella detection ─────────────────────────────────────────────────
# Detects whether a Discussion body represents an "umbrella" Spec spanning
# multiple PRs, and computes how many PRs are planned. Four signals:
#   1. Explicit UMBRELLA:N-PR marker
#   2. >=2 "### PR-[a-z]:" headings
#   3. >=2 distinct "**Batch <letter>" mentions (inline convention, e.g. D#1534/D#1552)
#   4. >=2 distinct "Slice <letter><digit>" sub-slice mentions (e.g. "Slice B1"/
#      "Slice B2" — D#1535)
# This is the single source of truth for umbrella detection — both the
# discussion_close step and the completion_block step call it, so they can
# never drift out of sync with each other (that drift is what caused D#1566).
#
# Args: $1 = Discussion body
# Sets globals (read immediately after calling, before the next call):
#   UMBRELLA_IS_UMBRELLA    true|false
#   UMBRELLA_PLANNED_COUNT  integer (0 if not an umbrella or count unknown)
#   UMBRELLA_PLANNED_LABELS newline-separated "pr-a"/"pr-b"/... labels, empty if not tracked by heading
detect_umbrella() {
  local body="$1"
  local umbrella_marker pr_sections batch_letters batch_count slice_labels slice_count

  umbrella_marker=$(echo "$body" | grep -oE 'UMBRELLA:[0-9]+-PR' | head -1 || echo "")
  pr_sections=$(echo "$body" | grep -cE '^### PR-[a-z]:' || true)
  pr_sections="${pr_sections:-0}"

  # Inline "**Batch A —", "**Batch B —" style bullets instead of ### PR-[a-z]:
  # headings. Requires a non-letter right after the captured letter so a
  # mid-word match (e.g. "**Batch ordering**" capturing the "o" of "ordering")
  # doesn't count as a batch letter. Dedup by letter so repeated prose mentions
  # ("Batches A, B, C") don't inflate the count beyond the real distinct batches.
  batch_letters=$(echo "$body" | grep -oiE '\*\*Batch[[:space:]]+[A-Za-z][^A-Za-z]' | sed -E 's/^\*\*Batch[[:space:]]+([A-Za-z]).$/\1/' | tr 'a-z' 'A-Z' | sort -u || true)
  # grep -c already prints "0" (clean, single line) on no-match — do NOT add
  # `|| echo "0"` on top of that, it appends a second "0" line (D#1566 Bug 1).
  batch_count=$(echo "$batch_letters" | grep -c '[A-Z]' || true)
  batch_count="${batch_count:-0}"

  # "Slice B1", "Slice B2" style sub-slice mentions (D#1535's convention).
  # D#1584's own Discussion body suggested requiring a bold "**Slice <letter>"
  # prefix (mirroring the Batch precedent), but that hypothesis does NOT match
  # D#1535's real body — it never bold-prefixes "Slice". What actually
  # distinguishes D#1535's own multi-slice commitment from D#1526/D#1528's
  # bare prose mentions of *another* Discussion's slices ("D#1528 Slice A",
  # "D#1528 Slice B") is the digit suffix: D#1535 uses numbered sub-slices
  # ("Slice B1"/"Slice B2") while D#1526/D#1528 only ever use single-letter
  # "Slice A"/"Slice B" with no digit. Requiring [A-Za-z][0-9]+ verified
  # against all three real bodies: matches D#1535 (B1, B2), matches neither
  # D#1526 nor D#1528. Dedup by label so repeated prose mentions don't
  # inflate the count beyond the real distinct sub-slices.
  slice_labels=$(echo "$body" | grep -oiE 'Slice[[:space:]]+[A-Za-z][0-9]+[^A-Za-z0-9]' | sed -E 's/^[Ss]lice[[:space:]]+([A-Za-z][0-9]+).$/\1/' | tr 'a-z' 'A-Z' | sort -u || true)
  slice_count=$(echo "$slice_labels" | grep -c '[A-Z0-9]' || true)
  slice_count="${slice_count:-0}"

  UMBRELLA_IS_UMBRELLA=false
  if [[ -n "$umbrella_marker" ]] || [[ "$pr_sections" -gt 1 ]] || [[ "$batch_count" -ge 2 ]] || [[ "$slice_count" -ge 2 ]]; then
    UMBRELLA_IS_UMBRELLA=true
  fi

  UMBRELLA_PLANNED_LABELS=""
  UMBRELLA_PLANNED_COUNT=0
  if [[ "$UMBRELLA_IS_UMBRELLA" == "true" ]]; then
    local planned_labels planned_count
    planned_labels=$(echo "$body" | grep -oE '^### PR-[a-z]:' | sed 's/^### //' | sed 's/://' | sort || echo "")
    # Same grep -c fix as batch_count above — no `|| echo "0"` fallback.
    planned_count=$(echo "$planned_labels" | grep -c '[a-z]' || true)
    planned_count="${planned_count:-0}"

    # Fall back to UMBRELLA:N-PR count if no sections found
    if [[ "$planned_count" -eq 0 && -n "$umbrella_marker" ]]; then
      planned_count=$(echo "$umbrella_marker" | grep -oE '[0-9]+' | head -1 || echo "0")
      planned_labels=""
    fi

    # Fall back to distinct "**Batch X" letters when that's what triggered
    # umbrella detection (no ### PR-[a-z]: headings, no UMBRELLA:N-PR marker).
    # Batch labels are inline prose, not consistent headings, so this is a
    # count-only fallback — no per-batch remaining-labels tracking.
    if [[ "$planned_count" -eq 0 && "$batch_count" -ge 2 ]]; then
      planned_count="$batch_count"
      planned_labels=""
    fi

    # Fall back to distinct "Slice X1"/"Slice X2" sub-slice labels when that's
    # what triggered umbrella detection (no headings, no UMBRELLA:N-PR marker,
    # no **Batch letters). Same count-only fallback as Batch above.
    if [[ "$planned_count" -eq 0 && "$slice_count" -ge 2 ]]; then
      planned_count="$slice_count"
      planned_labels=""
    fi

    UMBRELLA_PLANNED_COUNT="$planned_count"
    UMBRELLA_PLANNED_LABELS="$planned_labels"
  fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
_REPO="$(_resolve_repo)"
_REPO_OWNER="${_REPO%/*}"
_REPO_NAME="${_REPO#*/}"
# shellcheck source=scripts/lib/discussion-close-guard.sh
source "$SCRIPT_DIR/lib/discussion-close-guard.sh"
# shellcheck source=scripts/lib/planned-prs-label.sh
source "$SCRIPT_DIR/lib/planned-prs-label.sh"

# ── Merge-count resolution (D#2021 §3) ────────────────────────────────────────
# Recorded merges via backend/pr_state.py replace the old title-prefix-only
# count as the merge-count source for discussion-close-guard.sh's
# planned_prs > 1 branch. Title-prefix counting stays only as a FALLBACK for a
# Discussion with no pr_state record at all — it biases toward undercounting
# (and therefore toward staying open, the safe direction of error), which is
# why it is safe to keep and unsafe to rely on. The two counts are never
# combined/maxed: the recorded count wins whenever any record exists.
# Args: $1 = Discussion number
# Echoes: integer merge count
resolve_merged_count() {
  local disc="$1"
  local all_records record_total recorded_true title_count

  all_records=$(python3 "$REPO_ROOT/backend/pr_state.py" list --discussion "$disc" 2>/dev/null || echo "[]")
  record_total=$(echo "$all_records" | python3 -c "
import json, sys
try:
    print(len(json.load(sys.stdin)))
except Exception:
    print(0)
" 2>/dev/null || echo "0")
  record_total="${record_total:-0}"

  if [[ "$record_total" -gt 0 ]]; then
    recorded_true=$(echo "$all_records" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(sum(1 for e in d if e.get('merged') is True))
except Exception:
    print(0)
" 2>/dev/null || echo "0")
    echo "${recorded_true:-0}"
  else
    title_count=$(gh pr list --repo $_REPO \
      --state merged --json number,title \
      --jq "[.[] | select(.title | startswith(\"#${disc}:\"))] | length" \
      2>/dev/null || echo "0")
    echo "${title_count:-0}"
  fi
}
# shellcheck source=scripts/lib/state-dir.sh
source "$SCRIPT_DIR/lib/state-dir.sh" || true
# The auto_pull step, and the untracked-collision recovery it calls, both live
# under lib/ so tests can drive them against a fixture repo — auto-pull-step.sh
# sources auto-pull-recover.sh itself.
# shellcheck source=scripts/lib/auto-pull-step.sh
source "$SCRIPT_DIR/lib/auto-pull-step.sh"

PR=""
DISCUSSION=""
EVENT_ID_ARG=""
RESUME_FLAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)         PR="$2";         shift 2 ;;
    --discussion) DISCUSSION="$2"; shift 2 ;;
    --event-id)   EVENT_ID_ARG="$2"; shift 2 ;;
    --resume)     RESUME_FLAG="--resume"; shift ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 --pr <N> [--discussion <N>]" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$PR" ]]; then
  echo "Error: --pr is required" >&2
  exit 1
fi

# ── Auto-detect discussions from PR body if not provided ─────────────────────
# Matches (case-insensitive): Closes/Fixes/Resolves followed by D#N or #N.
# Bare "D#N" or "Discussion #N" with no closing keyword are intentionally excluded
# to avoid auto-closing Discussions that are only mentioned as related work.
# Each candidate is validated via GraphQL — Issues/PRs are skipped.
# Builds DISCUSSIONS array; DISCUSSION is set to first entry for backward compat.
declare -a DISCUSSIONS=()
if [[ -n "$DISCUSSION" ]]; then
  DISCUSSIONS=("$DISCUSSION")
else
  PR_BODY=$(gh pr view "$PR" --repo $_REPO --json body \
    --jq '.body' 2>/dev/null || echo "")

  # Extract all candidate numbers from recognised closing-keyword patterns only
  RAW_NUMS=$(echo "$PR_BODY" \
    | grep -oiE '([Cc]loses|[Rr]esolves|[Ff]ixes) (D#|#)[0-9]+' \
    | grep -oE '[0-9]+' \
    | sort -u)

  for CAND in $RAW_NUMS; do
    # Validate: GraphQL returns non-null id only for Discussions (not Issues/PRs)
    DISC_VALID=$(gh api graphql \
      -f query="query { repository(owner:\"${_REPO_OWNER}\", name:\"${_REPO_NAME}\") { discussion(number:$CAND) { id } } }" \
      --jq '.data.repository.discussion.id' 2>/dev/null || echo "")
    if [[ -n "$DISC_VALID" && "$DISC_VALID" != "null" ]]; then
      DISCUSSIONS+=("$CAND")
      echo "[post-merge-hook] Auto-detected Discussion #$CAND from PR #$PR body"
    else
      echo "[post-merge-hook] Skipping #$CAND — not a Discussion (Issue/PR or missing)"
    fi
  done
fi

# First entry used for backward-compat single-Discussion fields
DISCUSSION="${DISCUSSIONS[0]:-}"

# Export context for hook-event.sh ID generation
export HOOK_ROLE="merge"
export HOOK_DISCUSSION="${DISCUSSION:-}"
export HOOK_PR="$PR"
export HOOK_VERDICT="done"
export HOOK_CALLER="post-merge-hook"

# Source shared idempotency helpers
# shellcheck source=scripts/lib/hook-event.sh
source "$SCRIPT_DIR/lib/hook-event.sh"

# Initialize event
INIT_ARGS=()
[[ -n "$EVENT_ID_ARG" ]] && INIT_ARGS+=(--event-id "$EVENT_ID_ARG")
[[ -n "$RESUME_FLAG" ]]  && INIT_ARGS+=(--resume)

hook_event_init "post-merge-hook" \
  "agent_feed,wiki_sync,discussion_close,cost_comment,completion_block,worktree_merge_registry,quality_score,lessons_record,team_log,tmux_reload_flag,auto_pull,browser_tour_queue,stats_metrics,release_manager_queue,interactive_metrics_tick,hourly_stats_refresh,reap_chromes,drain_pending_prs" \
  "${INIT_ARGS[@]:-}"

echo "[post-merge-hook] event_id=${HOOK_EVENT_ID} pr=$PR"

# ── 0. Agent feed — JSONL merge event FIRST (full event, sync, flocked) ───────
if ! hook_event_has_step "agent_feed"; then
  FEED_MSG="merged PR #$PR"
  [[ -n "$DISCUSSION" ]] && FEED_MSG="$FEED_MSG (closes D#$DISCUSSION)"
  FEED_MSG="${FEED_MSG:0:280}"

  FEED_ARGS=(
    --role "merge"
    --event-type "merge"
    --message "$FEED_MSG"
    --verdict "done"
    --pr "$PR"
  )
  [[ -n "$DISCUSSION" ]] && FEED_ARGS+=(--discussion "$DISCUSSION")

  bash "$SCRIPT_DIR/agent-feed-append.sh" "${FEED_ARGS[@]}" \
    || true  # non-fatal unless disk full
  hook_event_mark_step "agent_feed"
fi

# ── 1. Wiki sync ──────────────────────────────────────────────────────────────
if ! hook_event_has_step "wiki_sync"; then
  echo "[post-merge-hook] Running wiki sync for PR #$PR"
  timeout --kill-after=5s 60 bash "$SCRIPT_DIR/post-merge-wiki.sh" 2>&1 \
    || echo "[post-merge-hook] Warning: wiki sync failed or timed out (non-fatal)" >&2
  hook_event_mark_step "wiki_sync"
fi

# ── 2. Close linked Discussions ───────────────────────────────────────────────
# D#2021: inverted from "is this an umbrella? close on NO" to "has the
# declared work finished? close only on YES" — unknown holds. See
# scripts/lib/discussion-close-guard.sh for the decision and why: a stacked
# chain matches none of the four umbrella vocabularies and got read as
# "single PR, safe to close", which closed D#1997 twice in one hour with
# five PRs still open and gated on it.
if ! hook_event_has_step "discussion_close"; then
  for DISCUSSION in "${DISCUSSIONS[@]}"; do
    # Fetch Discussion body and id together. An empty/unreadable body here
    # (fetch degraded but the node id still resolved) used to run the close
    # path — discussion_close_decision's own empty-body check (below) now
    # holds instead, closing that fail-open gap at the source.
    # D#2064: also fetch comments — PMs post Specs (and therefore
    # planned_prs) as comments, and a body-only query never sees them.
    # first:100 covers every Discussion's comment count in this repo today;
    # no pagination here (resolve-spec-text.sh owns that job for its own,
    # narrower use). If a Discussion ever exceeds 100 comments, warn loudly
    # below rather than silently maximising over a truncated population.
    # SPEC_COMMENTS_TEXT is newline-joined comment bodies; discussion-close-
    # guard.sh does the max-across-body-and-comments resolution, not this
    # hub file.
    DISC_DATA=$(gh api graphql \
      -f query="query { repository(owner:\"${_REPO_OWNER}\", name:\"${_REPO_NAME}\") { discussion(number:$DISCUSSION) { id body comments(first:100) { pageInfo { hasNextPage } nodes { body } } } } }" \
      --jq '.data.repository.discussion' 2>/dev/null || echo "")
    DISC_ID=$(echo "$DISC_DATA" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
    CURRENT_BODY=$(echo "$DISC_DATA" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('body',''))" 2>/dev/null || echo "")
    SPEC_COMMENTS_TEXT=$(echo "$DISC_DATA" | python3 -c "
import json, sys
d = json.load(sys.stdin)
nodes = (d.get('comments') or {}).get('nodes') or []
print('\n'.join(n.get('body', '') for n in nodes))
" 2>/dev/null || echo "")
    if echo "$DISC_DATA" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d.get('comments') or {}).get('pageInfo',{}).get('hasNextPage') else 1)" 2>/dev/null; then
      echo "[post-merge-hook] Warning: Discussion #$DISCUSSION has more than 100 comments — planned_prs resolution only saw the first 100, a later comment may be missed (non-fatal)" >&2
    fi

    # Record this merge in the pr_state registry (D#2021 §3) — becomes the
    # merge-count source of truth for the guard's planned_prs > 1 branch.
    # Title-prefix counting (resolve_merged_count's fallback) stays only for
    # a Discussion with no recorded entry at all.
    EXISTING_PR_ENTRY=$(python3 "$REPO_ROOT/backend/pr_state.py" get "$PR" 2>/dev/null || echo "null")
    if [[ -z "$EXISTING_PR_ENTRY" || "$EXISTING_PR_ENTRY" == "null" ]]; then
      python3 "$REPO_ROOT/backend/pr_state.py" init "$PR" --discussion "$DISCUSSION" >/dev/null 2>&1 || true
    fi
    python3 "$REPO_ROOT/backend/pr_state.py" set "$PR" --field "merged=true" >/dev/null 2>&1 || true

    MERGED_FOR_GUARD=$(resolve_merged_count "$DISCUSSION")

    # Umbrella vocabulary detection (unchanged, shared with completion_block
    # via detect_umbrella) — demoted to a signal the guard may use to prove
    # a count is greater than one, never that it equals one.
    detect_umbrella "$CURRENT_BODY"

    discussion_close_decision "$CURRENT_BODY" "$MERGED_FOR_GUARD" "$UMBRELLA_IS_UMBRELLA" "${SPEC_COMMENTS_TEXT:-}"

    if [[ "$CLOSE_DECISION" == "close" ]]; then
      echo "[post-merge-hook] Closing Discussion #$DISCUSSION ($CLOSE_REASON)"

      if [[ -z "$DISC_ID" ]]; then
        echo "[post-merge-hook] Warning: could not resolve Discussion #$DISCUSSION id — skipping close" >&2
      else
        gh api graphql \
          -f query="mutation { closeDiscussion(input:{discussionId:\"$DISC_ID\", reason:RESOLVED}) { discussion { id closed } } }" \
          2>/dev/null \
          || echo "[post-merge-hook] Warning: closeDiscussion mutation failed (non-fatal)" >&2
        echo "[post-merge-hook] Discussion #$DISCUSSION closed (id=$DISC_ID)"

        # needs-planned-prs backstop (D#2272): a closed Discussion must not
        # keep a stale "missing the field" flag on it.
        planned_prs_label_clear "$_REPO_OWNER" "$_REPO_NAME" "$DISC_ID"

        # Update Discussion STATUS to DONE — anchored, single-marker write
        # via backend/discussion_status.py's set-status CLI (D#2021 §4).
        # Replaces the old unanchored, global `sed` that rewrote every
        # occurrence of the marker token anywhere in the body.
        echo "[post-merge-hook] Updating Discussion #$DISCUSSION STATUS to DONE"
        if [[ -n "$CURRENT_BODY" ]]; then
          UPDATED_BODY=$(printf '%s' "$CURRENT_BODY" | python3 "$REPO_ROOT/backend/discussion_status.py" set-status --stdin DONE 2>/dev/null)
          SET_STATUS_RC=$?
          if [[ "$SET_STATUS_RC" -eq 0 && -n "$UPDATED_BODY" && "$UPDATED_BODY" != "$CURRENT_BODY" ]]; then
            ESCAPED_BODY=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$UPDATED_BODY" 2>/dev/null || echo "")
            if [[ -n "$ESCAPED_BODY" ]]; then
              gh api graphql \
                -f query="mutation { updateDiscussion(input:{discussionId:\"$DISC_ID\", body:$ESCAPED_BODY}) { discussion { id } } }" \
                2>/dev/null \
                || echo "[post-merge-hook] Warning: updateDiscussion STATUS mutation failed (non-fatal)" >&2
              echo "[post-merge-hook] Discussion #$DISCUSSION STATUS updated to DONE"
              CURRENT_BODY="$UPDATED_BODY"
            fi
          else
            echo "[post-merge-hook] Discussion #$DISCUSSION STATUS not updated (unanchored or set-status failed, rc=$SET_STATUS_RC — non-fatal)"
          fi
        fi
      fi
    else
      # hold or unknown — never close, never rewrite the marker, never write
      # the completion block (see step 2b below). Post one comment naming
      # the reason, unless the most recent hook comment already says the
      # same thing — idempotent so this doesn't spam a stalled Discussion on
      # every merge of an unrelated PR.
      echo "[post-merge-hook] Discussion #$DISCUSSION: $CLOSE_DECISION — $CLOSE_REASON"

      # needs-planned-prs backstop (D#2272): the mechanical spawn gate
      # (scripts/lib/planned-prs-gate.sh) only covers executors spawned
      # through scripts/spawn-agent.sh — a Team Lead spawning via Agent()
      # directly bypasses it. This label reports the same condition here,
      # after the fact, via the guard that already ran above. It never
      # closes a Discussion; only discussion_close_decision's "close" path
      # does that.
      if [[ -n "$DISC_ID" ]]; then
        PLANNED_PRS_LABEL_ACTION=$(planned_prs_label_action "$CLOSE_DECISION")
        case "$PLANNED_PRS_LABEL_ACTION" in
          apply) planned_prs_label_apply "$_REPO_OWNER" "$_REPO_NAME" "$DISC_ID" ;;
          # noop for "hold" — planned_prs WAS declared, nothing missing to flag.
        esac
      fi

      if [[ -n "$DISC_ID" ]]; then
        LAST_HOOK_COMMENT=$(gh api graphql \
          -f query="query { repository(owner:\"${_REPO_OWNER}\", name:\"${_REPO_NAME}\") { discussion(number:$DISCUSSION) { comments(last:1) { nodes { body } } } } }" \
          --jq '.data.repository.discussion.comments.nodes[0].body' 2>/dev/null || echo "")
        HOLD_COMMENT="PR #${PR} merged. Not closing Discussion #${DISCUSSION}: ${CLOSE_REASON}"
        if [[ "$LAST_HOOK_COMMENT" != "$HOLD_COMMENT" ]]; then
          ESCAPED_HOLD=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$HOLD_COMMENT" 2>/dev/null || echo "")
          if [[ -n "$ESCAPED_HOLD" ]]; then
            gh api graphql \
              -f query="mutation { addDiscussionComment(input:{discussionId:\"$DISC_ID\", body:$ESCAPED_HOLD}) { comment { id } } }" \
              2>/dev/null \
              || echo "[post-merge-hook] Warning: addDiscussionComment (hold reason) failed (non-fatal)" >&2
            echo "[post-merge-hook] Hold-reason comment posted: $HOLD_COMMENT"
          fi
        else
          echo "[post-merge-hook] Discussion #$DISCUSSION: identical hold reason already posted — skipping duplicate comment"
        fi
      fi
    fi
  done
  hook_event_mark_step "discussion_close"
fi

# ── 2a. Cost comment — post per-Discussion spend to the closed Discussion ──────
# Calls cost_tracker.py by-discussion --discussion N --json and formats the
# result as a markdown table, then posts it as a Discussion comment.
# Non-fatal: silently logs and skips when no spend is recorded or when the
# cost_tracker or cost_formatter exits nonzero.
if ! hook_event_has_step "cost_comment"; then
  if [[ -n "$DISCUSSION" && -n "${DISC_ID:-}" ]]; then
    COST_JSON=$(python3 "${REPO_ROOT}/backend/cost_tracker.py" \
      by-discussion --discussion "$DISCUSSION" --json 2>/dev/null || echo "")

    if [[ -n "$COST_JSON" && "$COST_JSON" != "null" ]]; then
      COST_TOTAL=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('total_cost_usd', 0) if d else 0)
except Exception:
    print(0)
" "$COST_JSON" 2>/dev/null || echo "0")

      # Only post when there is actual recorded spend
      if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "$COST_TOTAL" 2>/dev/null; then
        COST_MD=$(python3 "${REPO_ROOT}/backend/cost_formatter.py" <<< "$COST_JSON" 2>/dev/null || echo "")

        if [[ -n "$COST_MD" ]]; then
          ESCAPED_MD=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$COST_MD" 2>/dev/null || echo "")
          if [[ -n "$ESCAPED_MD" ]]; then
            gh api graphql \
              -f query="mutation { addDiscussionComment(input:{discussionId:\"$DISC_ID\", body:$ESCAPED_MD}) { comment { id } } }" \
              --repo "$_REPO" \
              2>/dev/null \
              && echo "[post-merge-hook] Cost comment posted to Discussion #$DISCUSSION (total: \$$COST_TOTAL)" \
              || echo "[post-merge-hook] Warning: cost comment GraphQL mutation failed (non-fatal)" >&2
          else
            echo "[post-merge-hook] Warning: cost_formatter produced unescapable output — skipping cost comment" >&2
          fi
        else
          echo "[post-merge-hook] Warning: cost_formatter returned empty output for Discussion #$DISCUSSION (non-fatal)" >&2
        fi
      else
        echo "[post-merge-hook] cost_comment: total_cost=0 for Discussion #$DISCUSSION — skipping comment"
      fi
    else
      echo "[post-merge-hook] cost_comment: no spend record for Discussion #$DISCUSSION — skipping"
    fi
  else
    echo "[post-merge-hook] cost_comment: no Discussion linked or DISC_ID missing — skipping"
  fi
  hook_event_mark_step "cost_comment"
fi

# ── 2b. Completion block — write actual_hours to Discussion body ──────────────
# Idempotent: replaces any existing <!-- COMPLETION --> block.
# D#2021: gated on the same discussion_close_decision the discussion_close
# step above uses, instead of its own copy of the umbrella-vs-merged-count
# check — so the fabricated actual_hours figure stops being written to
# Discussions the guard would not have closed either.
if ! hook_event_has_step "completion_block"; then
  if [[ -n "$DISCUSSION" && -n "${DISC_ID:-}" && -n "${CURRENT_BODY:-}" ]]; then
    MERGED_FOR_CB=$(resolve_merged_count "$DISCUSSION")
    detect_umbrella "$CURRENT_BODY"
    discussion_close_decision "$CURRENT_BODY" "$MERGED_FOR_CB" "$UMBRELLA_IS_UMBRELLA" "${SPEC_COMMENTS_TEXT:-}"

    WRITE_COMPLETION=true
    if [[ "$CLOSE_DECISION" != "close" ]]; then
      echo "[post-merge-hook] Discussion #$DISCUSSION: skipping completion_block — $CLOSE_REASON"
      WRITE_COMPLETION=false
    fi

    if [[ "$WRITE_COMPLETION" == "true" ]]; then
      # Fetch Discussion created_at and PR merged_at to compute actual_hours
      DISC_CREATED_AT=$(gh api graphql \
        -f query="query { repository(owner:\"${_REPO_OWNER}\", name:\"${_REPO_NAME}\") { discussion(number:${DISCUSSION}) { createdAt } } }" \
        --jq '.data.repository.discussion.createdAt' 2>/dev/null || echo "")
      PR_MERGED_AT=$(gh pr view "$PR" --repo $_REPO \
        --json mergedAt --jq '.mergedAt' 2>/dev/null || echo "")

      if [[ -n "$DISC_CREATED_AT" && -n "$PR_MERGED_AT" ]]; then
        ACTUAL_HOURS=$(python3 -c "
import sys
from datetime import datetime, timezone
def parse_iso(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))
try:
    created = parse_iso(sys.argv[1])
    merged  = parse_iso(sys.argv[2])
    delta_hours = (merged - created).total_seconds() / 3600
    print(round(delta_hours, 2))
except Exception as e:
    print('', file=__import__('sys').stderr)
    print('')
" "$DISC_CREATED_AT" "$PR_MERGED_AT" 2>/dev/null || echo "")

        if [[ -n "$ACTUAL_HOURS" ]]; then
          # Build the completion block string
          COMPLETION_BLOCK=$(printf '\n<!-- COMPLETION -->\nactual_hours: %s\nmerged_at: %s\nmerged_pr: %s\n<!-- /COMPLETION -->' \
            "$ACTUAL_HOURS" "$PR_MERGED_AT" "$PR")

          # Remove any existing COMPLETION block, then append fresh one
          UPDATED_BODY=$(python3 -c "
import sys, re
body = sys.argv[1]
block = sys.argv[2]
# Strip existing block (including optional leading newline)
cleaned = re.sub(r'\n?<!-- COMPLETION -->.*?<!-- /COMPLETION -->', '', body, flags=re.DOTALL)
print(cleaned.rstrip() + block)
" "$CURRENT_BODY" "$COMPLETION_BLOCK" 2>/dev/null || echo "")

          if [[ -n "$UPDATED_BODY" ]]; then
            ESCAPED_BODY=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$UPDATED_BODY" 2>/dev/null || echo "")
            if [[ -n "$ESCAPED_BODY" ]]; then
              gh api graphql \
                -f query="mutation { updateDiscussion(input:{discussionId:\"$DISC_ID\", body:$ESCAPED_BODY}) { discussion { id } } }" \
                2>/dev/null \
                || echo "[post-merge-hook] Warning: updateDiscussion COMPLETION block failed (non-fatal)" >&2
              echo "[post-merge-hook] Discussion #$DISCUSSION COMPLETION block written (actual_hours=$ACTUAL_HOURS)"
            fi
          fi
        else
          echo "[post-merge-hook] Warning: could not compute actual_hours for Discussion #$DISCUSSION (non-fatal)" >&2
        fi
      else
        echo "[post-merge-hook] Warning: missing timestamps for COMPLETION block on Discussion #$DISCUSSION (non-fatal)" >&2
      fi
    fi
  else
    echo "[post-merge-hook] completion_block: no Discussion linked — skipping"
  fi
  hook_event_mark_step "completion_block"
fi

# ── 2b. Worktree registry — mark merged ───────────────────────────────────────
if ! hook_event_has_step "worktree_merge_registry"; then
  if [[ -f "$SCRIPT_DIR/lib/worktree-registry.sh" ]]; then
    # shellcheck source=scripts/lib/worktree-registry.sh
    source "$SCRIPT_DIR/lib/worktree-registry.sh" 2>/dev/null || true
    # Find any registry entry with this PR number and mark it merged
    if [[ -n "$PR" ]]; then
      REGISTRY_FILE="${REPO_ROOT}/.autonomous-team/worktrees.json"
      if [[ -f "$REGISTRY_FILE" ]]; then
        WORKTREE_WITH_PR=$(python3 -c "
import json,sys
try:
    data=json.load(open(sys.argv[1]))
    pr=int(sys.argv[2])
    for e in data:
        if e.get('pr')==pr and e.get('status') in ('active','committed','pushed'):
            print(e['worktree_id'])
            break
except Exception:
    pass
" "$REGISTRY_FILE" "$PR" 2>/dev/null || true)
        if [[ -n "$WORKTREE_WITH_PR" ]]; then
          worktree_registry mark-status "$WORKTREE_WITH_PR" merged 2>/dev/null || true
          echo "[post-merge-hook] Marked worktree $WORKTREE_WITH_PR as merged (PR #$PR)"
        fi
      fi
    fi
  fi
  hook_event_mark_step "worktree_merge_registry"
fi

# ── 2c-pre. Quality score — compute if not already in blackboard ──────────────
# For manual merges (merge-and-hook.sh), no loop-iteration step runs quality_scorer
# beforehand. This step self-computes the score so lessons_record always has data.
# Idempotent: skipped when quality/<PR> is already populated (loop-driven merges).
# Non-fatal: a scorer failure logs a warning and lets the hook continue.
if ! hook_event_has_step "quality_score"; then
  BB_HAS_QUALITY=$(python3 -c "
import sys
sys.path.insert(0, '${REPO_ROOT}')
try:
    from backend.blackboard import get_blackboard
    bb = get_blackboard()
    data = bb.read('quality/${PR}')
    print('yes' if data else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

  if [[ "$BB_HAS_QUALITY" == "yes" ]]; then
    echo "[post-merge-hook] quality_score: quality/${PR} already in blackboard — skipping scorer"
  else
    echo "[post-merge-hook] quality_score: no entry for PR #${PR} — computing now (manual-merge path)"
    SCORE_OUT=$(python3 "${REPO_ROOT}/backend/quality_scorer.py" score --pr "${PR}" 2>&1) \
      && SCORE_RC=0 || SCORE_RC=$?
    if [[ $SCORE_RC -eq 0 ]]; then
      echo "[post-merge-hook] quality_score: scored PR #${PR} successfully"
    else
      echo "[post-merge-hook] WARNING: quality_scorer.py exited ${SCORE_RC} for PR #${PR} — lessons may be skipped (non-fatal): ${SCORE_OUT}" >&2
    fi
  fi
  hook_event_mark_step "quality_score"
fi

# ── 2c. Lessons record — emit lessons for sub-threshold quality dimensions ─────
# After merge, fetch quality score from blackboard and record one-line lessons
# for each dimension scoring below its threshold. Lessons feed back into
# executor spawn prompts via pre-spawn-check.sh.
#
# Thresholds (matching quality_scorer.py defaults from Spec):
#   complexity < 20/30, test_coverage < 15/25, review_rounds_score < 15/25
if ! hook_event_has_step "lessons_record"; then
  QUALITY_JSON=$(python3 -c "
import sys
sys.path.insert(0, '${REPO_ROOT}')
try:
    from backend.blackboard import get_blackboard
    bb = get_blackboard()
    data = bb.read('quality/${PR}')
    if data:
        import json
        print(json.dumps(data))
except Exception:
    pass
" 2>/dev/null || echo "")

  if [[ -n "$QUALITY_JSON" ]]; then
    python3 - "$PR" "$QUALITY_JSON" "$REPO_ROOT" <<'LESSONS_SCRIPT'
import json, sys
from pathlib import Path

pr = int(sys.argv[1])
quality = json.loads(sys.argv[2])
repo_root = Path(sys.argv[3])

sys.path.insert(0, str(repo_root))
from backend.lessons import LessonsStore

store = LessonsStore()

# Derive files_pattern from files_touched (top directories)
files_touched = quality.get("files_touched", [])
if files_touched:
    dirs = set()
    for f in files_touched[:10]:
        parts = Path(f).parts
        if len(parts) > 1:
            dirs.add(parts[0] + "/**")
        else:
            dirs.add("*")
    files_pattern = ",".join(sorted(dirs)[:2]) if dirs else "*"
else:
    files_pattern = "*"

# Dimension thresholds and lesson templates
THRESHOLDS = {
    "complexity":     (20, "Keep functions small — avg McCabe complexity exceeded threshold"),
    "test_coverage":  (15, "Add test file for every changed module (test_<module>.py in diff)"),
    "review_rounds":  (15, "Avoid multiple review rounds — fix issues in one shot via preflight"),
}

dimensions = quality.get("breakdown", {})
for dim, (threshold, template) in THRESHOLDS.items():
    dim_data = dimensions.get(dim, {})
    score = dim_data.get("score", None)
    if score is None:
        continue
    if score < threshold:
        detail = dim_data.get("detail", "")
        lesson = f"{template}. Detail: {detail}" if detail else template
        store.record(
            pr=pr,
            dimension=dim,
            score=float(score),
            lesson=lesson[:200],
            files_pattern=files_pattern,
            role="executor",
        )
        print(f"[post-merge-hook] Lesson recorded: dim={dim} score={score} pattern={files_pattern}")
LESSONS_SCRIPT
  else
    echo "[post-merge-hook] No quality score for PR #$PR in blackboard — skipping lessons record"
  fi
  hook_event_mark_step "lessons_record"
fi

# ── 3. Log to team-log — TERSE one-liner ─────────────────────────────────────
if ! hook_event_has_step "team_log"; then
  # Terse: [HH:MM] merged PR #N (closes D#N)
  # Full merge event is in agent-feed.jsonl
  MSG="[$(date +%H:%M)] merged PR #$PR"
  [[ -n "$DISCUSSION" ]] && MSG="$MSG (closes D#$DISCUSSION)"
  bash "$SCRIPT_DIR/rotate-team-log.sh" comment "$MSG" \
    || echo "[post-merge-hook] Warning: team-log comment failed (non-fatal)" >&2
  hook_event_mark_step "team_log"
fi

# ── 3b. Tmux reload flag — write flag file if CLAUDE.md was changed ──────────
# Operator reads .autonomous-team/needs-tmux-reload and reloads the tmux session.
# Auto-restart is intentionally out of scope — we only signal, not act.
if ! hook_event_has_step "tmux_reload_flag"; then
  CLAUDE_MD_TOUCHED=$(gh pr view "$PR" --repo $_REPO \
    --json files --jq '[.files[].path | select(. == "CLAUDE.md")] | length' \
    2>/dev/null || echo "0")

  if [[ "${CLAUDE_MD_TOUCHED:-0}" -gt 0 ]]; then
    FLAG_FILE="${REPO_ROOT}/.autonomous-team/needs-tmux-reload"
    TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf 'pr=%s\nts=%s\n' "$PR" "$TS" > "$FLAG_FILE"
    echo "[post-merge-hook] CLAUDE.md changed in PR #$PR — wrote $FLAG_FILE"

    TMUX_MSG="[$(date +%H:%M)] post-merge-hook: CLAUDE.md changed in PR #$PR — tmux session needs reload (see .autonomous-team/needs-tmux-reload)"
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment "$TMUX_MSG" \
      || echo "[post-merge-hook] Warning: team-log comment for tmux-reload failed (non-fatal)" >&2
  else
    echo "[post-merge-hook] PR #$PR does not touch CLAUDE.md — no tmux reload needed"
  fi
  hook_event_mark_step "tmux_reload_flag"
fi

# ── 4. Auto-pull main — keep local checkout current after merge ───────────────
# Operates on the parent repo root, not on any worktree.
# Uses git -C "$REPO_ROOT" so the pull targets main even when this script
# is called from inside a .claude/worktrees/agent-XXX/ directory.
#
# Defensive branch reset: if the parent repo is not on main (common when an
# executor's git checkout leaks into the parent worktree), we attempt to
# switch back to main before pulling. This prevents the silent no-op that
# previously caused stale-main rebase races (see D#496).
if ! hook_event_has_step "auto_pull"; then
  # The pull itself lives in scripts/lib/auto-pull-step.sh so the suite can drive
  # it against a throwaway repo instead of the operator's checkout (D#1948).
  # Step bookkeeping stays here: the return code is what separates "done" from
  # "leave it unmarked and let the next merge try again".
  auto_pull_step "$REPO_ROOT" && AUTO_PULL_RC=0 || AUTO_PULL_RC=$?
  case "$AUTO_PULL_RC" in
    0)
      # Pulled, or already current. The step really is done.
      hook_event_mark_step "auto_pull"
      ;;
    2)
      # Fatal: the tree is dirty, or the switch back to main failed. Stop here
      # and leave the step unmarked, so a retry re-attempts it.
      exit 1
      ;;
    127)
      # auto_pull_step is undefined — the source above did not load. Without
      # this arm that lands in *) and the parent repo quietly stops being
      # updated after every merge, which is the failure nobody would notice.
      APS_MISSING_MSG="[$(date +%H:%M)] post-merge-hook: ERROR — auto_pull_step is undefined, so scripts/lib/auto-pull-step.sh did not load. The parent repo was NOT updated."
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment "$APS_MISSING_MSG" || true
      echo "$APS_MISSING_MSG" >&2
      ;;
    *)
      # Declined or failed. Unmarked for the same reason, but the later steps
      # still run — same as before.
      :
      ;;
  esac
fi

# ── 5. Browser-tour queue — enqueue a tour if this PR touched dashboard/ ────────
if ! hook_event_has_step "browser_tour_queue"; then
  CHANGED_DASHBOARD_FILES=$(gh pr view "$PR" --repo $_REPO \
    --json files --jq '[.files[].path | select(startswith("dashboard/"))] | join("\n")' \
    2>/dev/null || echo "")

  if [[ -z "$CHANGED_DASHBOARD_FILES" ]]; then
    echo "[post-merge-hook] PR #$PR does not touch dashboard — no browser-tour queued"
  else
    echo "[post-merge-hook] PR #$PR touches dashboard — queuing browser-tour"

    # Map dashboard file paths to page routes
    AFFECTED_PAGES=$(python3 - "$CHANGED_DASHBOARD_FILES" <<'PAGE_MAP'
import sys, re

files_raw = sys.argv[1]
files = [f.strip() for f in files_raw.splitlines() if f.strip()]

PAGE_ROUTE_MAP = {
    "ideaspage":       "/ideas",
    "discussionspage": "/discussions",
    "prspage":         "/prs",
    "kpipage":         "/kpi",
    "agentspage":      "/agents",
    "budgetpage":      "/budget",
    "looppage":        "/loop",
    "settingspage":    "/settings",
}

pages = set()
for f in files:
    # Check if it is a Pages file: dashboard/src/pages/<X>Page.tsx
    m = re.search(r'dashboard/src/pages/([A-Za-z0-9]+)Page\.tsx', f, re.IGNORECASE)
    if m:
        key = m.group(1).lower() + "page"
        route = PAGE_ROUTE_MAP.get(key, "/" + m.group(1).lower())
        pages.add(route)
    else:
        # Non-page dashboard file — catch-all root tour
        pages.add("/")

if not pages:
    pages.add("/")

import json
print(json.dumps(sorted(pages)))
PAGE_MAP
)

    QUEUED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    TOUR_GOAL="Regression tour after PR #${PR} — verify ${AFFECTED_PAGES} renders correctly and has no console errors"

    QUEUE_FILE="${REPO_ROOT}/.autonomous-team/browser-tour-queue.jsonl"
    python3 -c "
import json, sys
entry = {
    'trigger': 'post-merge',
    'pr': int(sys.argv[1]),
    'affected_pages': json.loads(sys.argv[2]),
    'tour_goal': sys.argv[3],
    'queued_at': sys.argv[4],
    'status': 'pending'
}
print(json.dumps(entry, indent=2).replace('\n  ', '\n ').replace('{\n ', '{').replace('\n}', '}'))
" "$PR" "$AFFECTED_PAGES" "$TOUR_GOAL" "$QUEUED_AT" >> "$QUEUE_FILE" 2>/dev/null \
      || echo "[post-merge-hook] Warning: browser-tour queue write failed (non-fatal)" >&2

    echo "[post-merge-hook] Browser-tour queued for PR #$PR — pages: $AFFECTED_PAGES"
  fi
  hook_event_mark_step "browser_tour_queue"
fi

# ── 6. Stats metrics — emit 4 post-merge metrics to stats.duckdb ─────────────
if ! hook_event_has_step "stats_metrics"; then
  # Determine Discussion tag (Bug/Feature/Small etc.) from Discussion title
  DISC_TAG=""
  if [[ -n "$DISCUSSION" ]]; then
    DISC_TAG=$(gh api graphql \
      -f query="query { repository(owner:\"${_REPO_OWNER}\", name:\"${_REPO_NAME}\") { discussion(number:${DISCUSSION}) { title } } }" \
      --jq '.data.repository.discussion.title' 2>/dev/null \
      | grep -oP '^\[([A-Za-z]+)\]' | tr -d '[]' || echo "")
  fi

  # PR creation time for time_to_merge calculation
  PR_CREATED_AT=$(gh pr view "$PR" --repo $_REPO \
    --json createdAt --jq '.createdAt' 2>/dev/null || echo "")

  # Count fix cycles: times code-review-needs-fix was applied before merge
  FIX_CYCLE_COUNT=$(gh pr view "$PR" --repo $_REPO \
    --json timelineItems \
    --jq '[.timelineItems.nodes[] | select(.label.name == "code-review-needs-fix")] | length' \
    2>/dev/null || echo "0")
  FIX_CYCLE_COUNT="${FIX_CYCLE_COUNT:-0}"

  # Cost for this Discussion in USD — via cost_tracker.py, the single source
  # of truth for cost. (This used to shell out to `budget.py status` and
  # re-price by hand with an inline copy of the pricing table; that read a
  # store — budget/agents/ — that could silently stop being written, and it
  # duplicated pricing rates that already lived in cost_tracker.py.)
  #
  # cost_tracker already puts a `source` field on the returned entry
  # ("agent_run" | "budget_blackboard", or the entry is absent entirely when
  # nothing matches) — see backend/cost_tracker.py's by_discussion loop. Read
  # it here instead of discarding it: `budget_blackboard` and "no match" are
  # a different instrument than `agent_run`, in the same USD field, and a
  # reader comparing two cost_per_merged_pr_usd rows has no way to tell them
  # apart unless the resolver travels with the number (D#2282).
  COST_USD="0"
  COST_SOURCE="none"
  if [[ -n "$DISCUSSION" ]]; then
    COST_JSON_FOR_STATS=$(python3 "${REPO_ROOT}/backend/cost_tracker.py" \
      by-discussion --discussion "$DISCUSSION" --json 2>/dev/null || echo "")
    COST_USD=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1]) if sys.argv[1] else None
    print(d.get('total_cost_usd', 0) if d else 0)
except Exception:
    print(0)
" "$COST_JSON_FOR_STATS" 2>/dev/null || echo "0")
    COST_SOURCE=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1]) if sys.argv[1] else None
    print(d.get('source') or 'none') if d else print('none')
except Exception:
    print('none')
" "$COST_JSON_FOR_STATS" 2>/dev/null || echo "none")
    [[ -z "$COST_SOURCE" ]] && COST_SOURCE="none"
  fi

  # pr_file_conflict_score: overlap with PRs merged in previous 6h
  CONFLICT_SCORE=$(_PMH_PR="$PR" _PMH_REPO="$_REPO" python3 - <<'PYEOF'
import subprocess, json, sys, datetime, os

pr = os.environ.get("_PMH_PR", "")
repo = os.environ.get("_PMH_REPO", "")
if not pr:
    print(0)
    sys.exit(0)

try:
    r = subprocess.run(
        ["gh", "pr", "view", pr, "--repo", repo,
         "--json", "files", "--jq", "[.files[].path]"],
        capture_output=True, text=True, check=True,
    )
    pr_files = set(json.loads(r.stdout))
except Exception:
    print(0)
    sys.exit(0)

if not pr_files:
    print(0)
    sys.exit(0)

cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)).isoformat()
try:
    r2 = subprocess.run(
        ["gh", "pr", "list", "--repo", repo,
         "--state", "merged", "--json", "number,mergedAt,files",
         "--jq", f'[.[] | select(.number != {pr} and .mergedAt != null and .mergedAt > "{cutoff}")]'],
        capture_output=True, text=True, check=True,
    )
    recent_prs = json.loads(r2.stdout)
except Exception:
    print(0)
    sys.exit(0)

overlap_count = 0
for other_pr in recent_prs:
    other_files = {f["path"] for f in other_pr.get("files", [])}
    overlap = pr_files & other_files
    if overlap:
        overlap_count += len(overlap)

print(overlap_count)
PYEOF
  )
  CONFLICT_SCORE="${CONFLICT_SCORE:-0}"

  # Fetch extra data needed for Phase 2 metrics
  # spec_to_first_pr_latency_seconds: SPEC_READY timestamp from Discussion body
  SPEC_READY_TS=""
  if [[ -n "$DISCUSSION" ]]; then
    DISC_BODY=$(gh api graphql \
      -f query="query { repository(owner:\"${_REPO_OWNER}\", name:\"${_REPO_NAME}\") { discussion(number:${DISCUSSION}) { body } } }" \
      --jq '.data.repository.discussion.body' 2>/dev/null || echo "")
    # [^>]* rather than a literal space: BLOCKED-BY: sits between STATUS: and
    # SINCE: on the canonical line (wiki/Discussion-Status-Protocol.md), and
    # requiring adjacency silently zeroed this metric for exactly the
    # Discussions that adopt the field. [^>]* cannot cross the closing --> so
    # it still can't reach a second STATUS comment on the same line.
    SPEC_READY_TS=$(echo "$DISC_BODY" | grep -oP 'STATUS:SPEC_READY[^>]*SINCE:\K[^\s>]+' | head -1 || echo "")
  fi

  # reviewer_acceptance_latency_seconds: PR open -> first code-review-passed label
  REVIEWER_ACCEPT_TS=$(gh api "repos/$_REPO/issues/${PR}/timeline" \
    --jq '[.[] | select(.event == "labeled" and .label.name == "code-review-passed")] | first | .created_at' \
    2>/dev/null || echo "")

  # acceptance_criteria_pass_rate: parse AC from Discussion spec, check PR body coverage
  AC_PASS_RATE="-1"
  if [[ -n "$DISCUSSION" && -n "${DISC_BODY:-}" ]]; then
    AC_PASS_RATE=$(_PMH_DISC_BODY="$DISC_BODY" _PMH_PR="$PR" _PMH_REPO="$_REPO" python3 - <<'PYEOF'
import os, re, subprocess, json

disc_body = os.environ.get("_PMH_DISC_BODY", "")
pr = os.environ.get("_PMH_PR", "")
repo = os.environ.get("_PMH_REPO", "")

# Extract numbered AC lines from the Acceptance Criteria section
ac_section = re.search(r'### Acceptance Criteria\s*([\s\S]*?)(?=\n###|\Z)', disc_body)
if not ac_section:
    print("-1.0")
    exit()

ac_text = ac_section.group(1)
ac_lines = re.findall(r'^\s*(?:\d+\.|[-*])\s+(.+)', ac_text, re.MULTILINE)
if not ac_lines:
    print("-1.0")
    exit()

# Get PR body + comments as evidence corpus
try:
    r = subprocess.run(
        ["gh", "pr", "view", pr, "--repo", repo,
         "--json", "body,comments"],
        capture_output=True, text=True, check=True,
    )
    pr_data = json.loads(r.stdout)
    evidence_text = (pr_data.get("body") or "") + " ".join(
        c.get("body", "") for c in pr_data.get("comments", [])
    )
except Exception:
    evidence_text = ""

# Check what fraction of AC items are keyword-referenced in the evidence
referenced = 0
for ac in ac_lines:
    words = [w.lower() for w in re.findall(r'\w+', ac) if len(w) > 3][:6]
    if any(w in evidence_text.lower() for w in words):
        referenced += 1

rate = referenced / len(ac_lines)
print(f"{rate:.4f}")
PYEOF
    )
  fi

  # Emit metrics via a single record_many() call (Phase 2: batched). One of
  # cost_per_merged_pr_usd / cost_attribution_unresolved_count is appended
  # below depending on resolver provenance (D#2282).
  python3 - <<PYEOF
import sys, os
sys.path.insert(0, "$REPO_ROOT/backend")
from stats_writer import record_many
import datetime

pr = "$PR"
disc_tag = "$DISC_TAG" or "unknown"
fix_cycles_str = "$FIX_CYCLE_COUNT"
cost_usd_str = "$COST_USD"
cost_source_str = "$COST_SOURCE" or "none"
conflict_str = "$CONFLICT_SCORE"
pr_created = "$PR_CREATED_AT"
spec_ready_ts_str = "$SPEC_READY_TS"
reviewer_accept_ts_str = "$REVIEWER_ACCEPT_TS"
ac_rate_str = "$AC_PASS_RATE"

fix_cycles = int(fix_cycles_str) if fix_cycles_str.strip().isdigit() else 0
cost_usd = float(cost_usd_str) if cost_usd_str.strip().replace('.','',1).isdigit() else 0.0
conflict_score = int(conflict_str) if conflict_str.strip().lstrip('-').isdigit() else 0

now_dt = datetime.datetime.now(datetime.timezone.utc)

def _parse_iso(s):
    """Parse an ISO8601 string to UTC-aware datetime, or return None."""
    s = s.strip()
    if not s:
        return None
    dt = None
    # stdlib fallback (Python 3.11+)
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            from dateutil.parser import parse as dtparse
            dt = dtparse(s)
        except Exception:
            return None
    # Timestamps without a Z/offset suffix parse successfully but come back
    # tz-naive, which raises TypeError when later subtracted against gh's
    # always-tz-aware createdAt. Assume UTC for naive results.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt

# Phase 1 metrics
created_dt = _parse_iso(pr_created)
elapsed = max(0.0, (now_dt - created_dt).total_seconds()) if created_dt else 0.0

# Phase 2 metric: spec_to_first_pr_latency_seconds
spec_dt = _parse_iso(spec_ready_ts_str)
spec_latency = max(0.0, (created_dt - spec_dt).total_seconds()) if (spec_dt and created_dt) else -1.0

# Phase 2 metric: acceptance_criteria_pass_rate
try:
    ac_rate = float(ac_rate_str)
except Exception:
    ac_rate = -1.0

# Phase 2 metric: reviewer_acceptance_latency_seconds
reviewer_dt = _parse_iso(reviewer_accept_ts_str)
reviewer_latency = max(0.0, (reviewer_dt - created_dt).total_seconds()) if (reviewer_dt and created_dt) else -1.0

tags = {"pr": pr, "tag": disc_tag}
rows = [
    {"metric": "time_to_merge_seconds",              "value": elapsed,           "unit": "seconds", "tags": tags, "source": "post-merge-hook"},
    {"metric": "fix_cycle_count",                    "value": float(fix_cycles), "unit": "count",   "tags": tags, "source": "post-merge-hook"},
    {"metric": "pr_file_conflict_score",             "value": float(conflict_score), "unit": "count","tags": tags, "source": "post-merge-hook"},
    {"metric": "spec_to_first_pr_latency_seconds",   "value": spec_latency,      "unit": "seconds", "tags": tags, "source": "post-merge-hook"},
    {"metric": "acceptance_criteria_pass_rate",      "value": ac_rate,           "unit": "ratio",   "tags": tags, "source": "post-merge-hook"},
    {"metric": "reviewer_acceptance_latency_seconds","value": reviewer_latency,  "unit": "seconds", "tags": tags, "source": "post-merge-hook"},
    # Phase 3 metric: fix_rounds_per_pr — raw per-PR round count for avg_fix_rounds_24h aggregation
    {"metric": "fix_rounds_per_pr",                  "value": float(fix_cycles), "unit": "count",   "tags": tags, "source": "post-merge-hook"},
]

# Resolver provenance (D#2282): cost_tracker's `by-discussion` entry already
# carries which resolver produced total_cost_usd — "agent_run" (authoritative),
# "budget_blackboard" (a different, lossier instrument), or no entry at all
# (mapped to "none" above). Emitting all three as the same cost_per_merged_pr_usd
# field let a reader compare two different measurements without knowing they
# differed. Only the authoritative resolver gets to write that metric; the
# other two are suppressed and counted instead, so the gap is visible rather
# than silently averaged into the dashboard. This branch must never turn a
# fallback into a hook failure — omitting a row is not an error path.
cost_tags = dict(tags)
cost_tags["resolver"] = cost_source_str
if cost_source_str == "agent_run":
    rows.append({"metric": "cost_per_merged_pr_usd", "value": cost_usd, "unit": "usd", "tags": cost_tags, "source": "post-merge-hook"})
else:
    rows.append({"metric": "cost_attribution_unresolved_count", "value": 1.0, "unit": "count", "tags": cost_tags, "source": "post-merge-hook"})

record_many(rows)

print(f"[post-merge-hook] stats: time_to_merge={elapsed:.0f}s fix_cycles={fix_cycles} cost={cost_usd:.4f} resolver={cost_source_str} conflict={conflict_score}")
print(f"[post-merge-hook] stats: spec_latency={spec_latency:.0f}s ac_rate={ac_rate:.4f} reviewer_latency={reviewer_latency:.0f}s")
print(f"[post-merge-hook] stats: fix_rounds_per_pr={fix_cycles}")
PYEOF
  STATS_METRICS_RC=$?
  if [[ $STATS_METRICS_RC -eq 0 ]]; then
    hook_event_mark_step "stats_metrics"
  else
    echo "[post-merge-hook] WARNING: stats_metrics python block exited $STATS_METRICS_RC — step NOT marked complete"
  fi
fi

# ── 7. Release manager — record release artifact directly ───────────────────
if ! hook_event_has_step "release_manager_queue"; then
  RELEASE_GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.release_manager 2>/dev/null || echo "true")
  if [[ "$RELEASE_GATE" == "true" ]]; then
    python3 "$REPO_ROOT/backend/release_manager.py" record --pr "$PR" 2>/dev/null || true
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] post-merge-hook: release record written for PR #$PR" 2>/dev/null || true
  else
    bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] post-merge-hook: release_manager gate off — skipping record for PR #$PR" 2>/dev/null || true
  fi
  hook_event_mark_step "release_manager_queue"
fi

# ── 8. Interactive metrics tick — emit one loop-metrics row ──────────────
# Piggyback on every merge so the Loop Timeline chart has data even when
# the interactive /loop skill doesn't fire step 7.5. Non-fatal — a missing
# row is better than blocking a merge.
if ! hook_event_has_step "interactive_metrics_tick"; then
  bash "$SCRIPT_DIR/interactive-metrics-tick.sh" 2>/dev/null \
    || echo "[post-merge-hook] Warning: interactive-metrics-tick failed (non-fatal)" >&2
  hook_event_mark_step "interactive_metrics_tick"
fi

# ── 9. Hourly stats refresh — wasted_tokens_ratio, hard_rule_violation_count,
# impersonation_rate. Was designed as a cron job but never wired. Piggyback
# on merges so these tiles don't go stale; idempotent within a /loop iteration
# via hook_event_has_step. Non-fatal.
if ! hook_event_has_step "hourly_stats_refresh"; then
  bash "$SCRIPT_DIR/spawn-hourly-stats.sh" 2>/dev/null \
    || echo "[post-merge-hook] Warning: spawn-hourly-stats failed (non-fatal)" >&2
  hook_event_mark_step "hourly_stats_refresh"
fi

# ── 9b. Reap zombie puppeteer Chrome processes between merges ────────────────────
if ! hook_event_has_step "reap_chromes"; then
  bash "$SCRIPT_DIR/reap-zombie-chromes.sh" 2>/dev/null \
    || echo "[post-merge-hook] Warning: reap-zombie-chromes failed (non-fatal)" >&2
  hook_event_mark_step "reap_chromes"
fi

# ── 10. Drain pending PRs — rate limits often clear between merges ─────────────
# Executors that hit a secondary rate limit write their PR details to
# .autonomous-team/pending-prs.json instead of spinning in a sleep loop.
# We attempt to open them here, when the limit is likely cleared.
if ! hook_event_has_step "drain_pending_prs"; then
  if [[ -f "${REPO_ROOT}/.autonomous-team/pending-prs.json" ]]; then
    echo "[post-merge-hook] Draining pending-prs.json after merge of PR #$PR"
    bash "$SCRIPT_DIR/drain-pending-prs.sh" 2>/dev/null \
      || echo "[post-merge-hook] Warning: drain-pending-prs failed (non-fatal)" >&2
  else
    echo "[post-merge-hook] No pending-prs.json — drain step skipped"
  fi
  hook_event_mark_step "drain_pending_prs"
fi

# preflight_full step removed 2026-05-17 (D#973):
# Running the full pytest suite synchronously in a bookkeeping hook caused
# indefinite hangs when flaky tests (e.g. tui-tester/tmux-sweep.sh) ignored
# their own timeouts. Tests belong in CI / pre-merge gates, not post-merge
# record-keeping. The hook's job is stats, team-log, wiki sync, and Discussion
# close — none of which require a passing test suite.

# ── 11. Run post-merge.d/ drop-in scripts ────────────────────────────────────
# Any executable in scripts/hooks/post-merge.d/ is called here with PR=$PR.
# The directory is optional — no-op if missing.
if [[ -d "$SCRIPT_DIR/hooks/post-merge.d" ]]; then
  for _hook in "$SCRIPT_DIR/hooks/post-merge.d"/*; do
    [[ -x "$_hook" ]] || continue
    PR="$PR" bash "$_hook" --pr "$PR" 2>/dev/null \
      || echo "[post-merge-hook] Warning: $_hook failed (non-fatal)" >&2
  done
fi

# ── 12. Sweep stale loop-run logs (30-day retention, D#412) ──────────────────
# Idempotent — exits 0 when loop-runs/ is empty or missing.
if ! hook_event_has_step "sweep_loop_runs"; then
  bash "$SCRIPT_DIR/sweep-loop-runs.sh" 2>/dev/null \
    || echo "[post-merge-hook] Warning: sweep-loop-runs failed (non-fatal)" >&2
  hook_event_mark_step "sweep_loop_runs"
fi

hook_event_finish
echo "[post-merge-hook] Done."
