#!/usr/bin/env bash
# scripts/lib/discussion-close-guard.sh — decides whether a post-merge event
# may close a Discussion (D#2021).
#
# The old logic asked "is this an umbrella?" and closed on NO — a question
# with no reliable answer, because a stacked chain (D#1997's shape) matches
# none of the four recognised umbrella vocabularies and got treated as
# "not an umbrella" i.e. "single PR, safe to close". That closed D#1997 twice
# in one hour with five open PRs still gated on it.
#
# This file inverts the question: it asks "has the declared work finished?"
# and closes only on a positive YES. Unknown holds. The declared PR count
# comes from the Spec's `planned_prs` frontmatter field — the only place that
# actually knows it. The four legacy vocabularies (UMBRELLA:N-PR marker,
# ### PR-[a-z]: headings, **Batch <letter>, Slice <letter><digit>) are kept
# but demoted: they may prove a count is greater than one, never that it
# equals one.
#
# Sourceable, no side effects, testable standalone. Deliberately does NOT
# re-implement umbrella-vocabulary detection itself — that stays the single
# source of truth in scripts/post-merge-hook.sh's detect_umbrella() (drifting
# out of sync with that function is exactly what caused D#1566). Callers
# compute is_umbrella via detect_umbrella (or their own test fixture, for
# unit tests) and pass the boolean in.
#
# Usage:
#   source scripts/lib/discussion-close-guard.sh
#   discussion_close_decision "$BODY" "$MERGED_COUNT" "$IS_UMBRELLA" "$SPEC_COMMENTS_TEXT"
#   echo "$CLOSE_DECISION"   # close | hold | unknown
#   echo "$CLOSE_REASON"     # one-line human-readable explanation

# resolve_planned_prs <body> <comments_text>
#   The single source of truth for "where does planned_prs live" (D#2272).
#   discussion_close_decision below and scripts/lib/planned-prs-gate.sh's
#   planned_prs_gate_check both call this — not their own copy of the
#   extraction — because two implementations of this question is exactly the
#   drift that caused D#1566.
#
#   Anchored to line start (^planned_prs:[[:space:]]*[0-9]+) so a mention of
#   "planned_prs" in prose can't masquerade as the declared field. The
#   frontmatter block this reads can live in the Discussion body or in a Spec
#   comment (D#2064) — a "---" block is legal Markdown in either place, so the
#   same anchored grep applies to both sources unchanged.
#
#   Resolution is the MAXIMUM across body and comments, not "most recent
#   comment wins" — a stray low value quoted or restated later must not
#   override a real Spec's higher declared count. sort -rn | head -1 picks
#   the largest match within each source; comparing the two source maxima
#   then picks the overall winner and which source supplied it.
#
# Sets:
#   PLANNED_PRS          the winning integer, or "" if no anchored match was
#                         found in either source
#   PLANNED_PRS_SOURCE    "body" | "comment" | "" (empty exactly when
#                         PLANNED_PRS is empty)
resolve_planned_prs() {
  local body="${1:-}"
  local comments_text="${2:-}"

  PLANNED_PRS=""
  PLANNED_PRS_SOURCE=""

  local planned_prs_body planned_prs_comment
  planned_prs_body=$(printf '%s\n' "$body" | grep -oE '^planned_prs:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | sort -rn | head -1 || true)
  planned_prs_comment=$(printf '%s\n' "$comments_text" | grep -oE '^planned_prs:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | sort -rn | head -1 || true)

  if [[ -n "$planned_prs_body" && -n "$planned_prs_comment" ]]; then
    if [[ "$planned_prs_body" -ge "$planned_prs_comment" ]]; then
      PLANNED_PRS="$planned_prs_body"
      PLANNED_PRS_SOURCE="body"
    else
      PLANNED_PRS="$planned_prs_comment"
      PLANNED_PRS_SOURCE="comment"
    fi
  elif [[ -n "$planned_prs_body" ]]; then
    PLANNED_PRS="$planned_prs_body"
    PLANNED_PRS_SOURCE="body"
  elif [[ -n "$planned_prs_comment" ]]; then
    PLANNED_PRS="$planned_prs_comment"
    PLANNED_PRS_SOURCE="comment"
  fi
}

# discussion_close_decision <body> [merged_count] [is_umbrella] [spec_comments_text]
#   body                - the Discussion body text (required)
#   merged_count        - integer, recorded/estimated merges for this
#                         Discussion. Defaults to 0. Only consulted when
#                         planned_prs > 1.
#   is_umbrella         - "true"/"false", precomputed by the caller's umbrella
#                         detection. Defaults to "false". Only consulted when
#                         no planned_prs field is present in body or comments.
#   spec_comments_text  - optional (D#2064): the Discussion's comment bodies,
#                         newline-joined by the caller. PMs post Specs — and
#                         therefore planned_prs — as comments, which the body
#                         alone never sees. Defaults to empty, so existing
#                         1-, 2- and 3-argument call sites are unaffected.
#                         planned_prs resolves to the MAXIMUM anchored match
#                         found across body and this text, never "most
#                         recent" — a stray low value in a later comment must
#                         not override a real Spec's higher count and close a
#                         Discussion early (the exact bug D#2021 exists to
#                         prevent). The empty-body fail-closed check below
#                         stays keyed on $body alone: an unreadable body holds
#                         open even when comments carry a number.
#
# Sets (read immediately after calling, before the next call):
#   CLOSE_DECISION   close | hold | unknown
#   CLOSE_REASON     one-line human-readable explanation
discussion_close_decision() {
  local body="${1:-}"
  local merged_count="${2:-0}"
  local is_umbrella="${3:-false}"
  local spec_comments_text="${4:-}"

  CLOSE_DECISION=""
  CLOSE_REASON=""
  PLANNED_PRS=""
  PLANNED_PRS_SOURCE=""

  # 1. Body empty or unreadable -> unknown, never close. This is the fix for
  #    the fail-open defect at the old call site: an empty body fetch with a
  #    resolving node id must not be treated as "not an umbrella, safe to
  #    close" — it must be treated as "we don't know, hold."
  if [[ -z "$body" ]]; then
    CLOSE_DECISION="unknown"
    CLOSE_REASON="Discussion body was empty or unreadable — cannot confirm planned_prs. Holding open."
    return 0
  fi

  # planned_prs is a top-level frontmatter key (see D#2021's own Spec for the
  # convention: a "---"-delimited block under "## Spec" containing
  # estimated_hours/complexity_points/planned_prs/acceptance_files). Resolved
  # by the shared resolve_planned_prs() above — see its header for the
  # anchoring and max-across-sources rules; not reimplemented here.
  resolve_planned_prs "$body" "$spec_comments_text"

  if [[ -n "$PLANNED_PRS" ]]; then
    # 2. planned_prs: 0 -> hold, always, regardless of merged_count (D#2272).
    #    This is a DELIBERATE hold-open declaration for a Discussion whose
    #    completion is operational rather than a merged PR — not an omission,
    #    and not "close on the first merge". Without this branch, 0 falls
    #    through to the merged_count >= planned_prs check below, which is
    #    trivially true at merged_count=0 and closes immediately: the most
    #    aggressive value in the field's range, not the safest. Placed before
    #    the -eq 1 check so it can never be shadowed by it.
    if [[ "$PLANNED_PRS" -eq 0 ]]; then
      CLOSE_DECISION="hold"
      CLOSE_REASON="planned_prs: 0 declared (source: ${PLANNED_PRS_SOURCE}) — deliberate hold-open declaration, not an omission and not close-on-first-merge. Holding open until closed by its own recorded mechanism."
      return 0
    fi

    # 3. Frontmatter declares planned_prs: 1 -> close. This proves the change
    #    did not simply disable auto-close.
    if [[ "$PLANNED_PRS" -eq 1 ]]; then
      CLOSE_DECISION="close"
      CLOSE_REASON="planned_prs: 1 declared in Spec frontmatter (source: ${PLANNED_PRS_SOURCE}) — single-PR Discussion, closing."
      return 0
    fi

    # 3. Frontmatter declares planned_prs: N > 1 -> hold until N merges are
    #    recorded for this Discussion; close on the Nth.
    if [[ "$merged_count" -ge "$PLANNED_PRS" ]]; then
      CLOSE_DECISION="close"
      CLOSE_REASON="planned_prs: ${PLANNED_PRS} declared (source: ${PLANNED_PRS_SOURCE}), ${merged_count} merge(s) recorded — all planned PRs merged, closing."
    else
      CLOSE_DECISION="hold"
      CLOSE_REASON="planned_prs: ${PLANNED_PRS} declared (source: ${PLANNED_PRS_SOURCE}), ${merged_count} of ${PLANNED_PRS} merge(s) recorded — holding open."
    fi
    return 0
  fi

  # 4. No planned_prs field, but a demoted umbrella vocabulary matched (an
  #    UMBRELLA:N-PR marker, ### PR-[a-z]: headings, **Batch <letter>, or
  #    Slice <letter><digit> mentions). That proves the count is greater than
  #    one — it never proves the count equals one — so this branch NEVER
  #    closes, regardless of merged_count.
  if [[ "$is_umbrella" == "true" ]]; then
    CLOSE_DECISION="hold"
    CLOSE_REASON="No planned_prs field, but umbrella vocabulary (UMBRELLA:/PR-heading/Batch/Slice) matched this body — a prose-derived count can prove more than one PR but never exactly one. Add planned_prs to the Spec frontmatter. Holding open."
    return 0
  fi

  # 5. No planned_prs, no vocabulary match — the case that used to close.
  #    Absence of a recognised vocabulary is absence of evidence, not proof
  #    of a single-PR Discussion.
  CLOSE_DECISION="unknown"
  CLOSE_REASON="No planned_prs field in Spec frontmatter, and no umbrella vocabulary matched — cannot confirm this Discussion has only one planned PR. Add planned_prs: N to the Spec frontmatter. Holding open."
  return 0
}
