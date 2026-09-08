#!/usr/bin/env bash
# scripts/lib/pr-pickup-gate.sh — the PR pickup path, with an author gate in
# front of it (D#2404).
#
# WHY THIS FILE EXISTS
#   `team-lead-iteration.sh` Step 4 listed every open PR on the code plane and
#   fed the result straight into spawn recommendations (Step 5) and the quality
#   gate's label writes (Step 5.3). No author, draft or fork filter anywhere in
#   that chain. After the D#2348 cutover that means a stranger's PR gets a
#   reviewer spawned on it and labels written to it, exactly like ours.
#
#   The gate below runs `scripts/lib/pr_intake_gate.py check-pr` per PR and
#   drops blocked PRs before they reach ANY of the four work arrays. A blocked
#   PR is inert to automation — not listed, not spawned on, not labelled — and
#   stays visible on GitHub for a human to look at. Same rule and same
#   `intake-approved` vocabulary as the Discussion side.
#
#   It lives in lib/ rather than inline in the hub for the reason CLAUDE.md's
#   Module-per-Feature default gives, and for one more: a function can be
#   sourced by a test with a stubbed `gh`, so the gate's effect on the loop is
#   assertable as behaviour rather than as a helper's return value (D#2377).
#
# PROVIDES
#   pr_pickup_blocked <pr>        -> 0 = blocked (reason in $_PR_GATE_REASON)
#   classify_open_prs <prs_json>  -> fills NEEDS_REVIEW / NEEDS_MERGE /
#                                    NEEDS_FIX / NEEDS_SECURITY_REVIEW,
#                                    GATED_PRS and GATED_PR_COUNT from
#                                    `gh pr list --json number,title,labels`
#                                    output

_PR_PICKUP_GATE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# D#2422: resolved once, the same way sweep-stuck-prs.sh resolves its own
# $REPO — via repo-resolve.sh's bash-side resolver, not pr_intake_gate.py's
# internal default (which reads a *different* file, backend._repo.py's
# project.json, at a different precedence order). Both callers of
# `check-pr` share one on-disk PR-head-baseline store keyed by repo slug
# (pr_head_baseline.pr_key()); if this caller and the sweeper resolved to
# different slugs, they would silently maintain two separate baseline
# entries for the same real PR, and whichever one runs second would treat
# an already-drifted head as a fresh first observation — the anti-drift
# check D#2421 exists for, bypassed. Pinning both to the same bash-side
# resolver is what keeps that from happening.
#
# `2>/dev/null || true`, not `_require_code_repo`: this file is sourced by
# a library, not run as its own script (repo-resolve.sh's own header
# documents this pattern for exactly that reason — a top-level `exit` in a
# sourced file would kill the caller). When the bash resolver can't find a
# slug (no .autonomous-team/config.json and no AUTONOMOUS_TEAM_REPO — the
# case in a bare checkout with no local operator config, e.g. this file's
# own unit tests), $_PR_PICKUP_GATE_REPO stays empty and check-pr is called
# exactly as before this change: no --repo, falling back to its own
# internal default. That preserves existing behaviour anywhere the bash
# resolver has nothing to resolve, and unifies the two callers everywhere
# it does.
_PR_PICKUP_GATE_REPO="$(source "$_PR_PICKUP_GATE_LIB_DIR/repo-resolve.sh" && _resolve_code_repo 2>/dev/null || true)"

# Reason for the most recent pr_pickup_blocked call. Empty when not blocked.
_PR_GATE_REASON=""

# Hint for the most recent pr_pickup_blocked call — sourced from check-pr's
# `hint` field (D#2444) rather than computed here, so this file and the
# sweeper's embedded Python (scripts/sweep-stuck-prs.sh) print the identical
# remedy text for the identical reason instead of keeping two copies that can
# drift apart from each other. Empty when not blocked.
_PR_GATE_HINT=""

# Log through the caller's log() when it has one, so gate decisions land in the
# same iteration log as everything else.
_ppg_log() {
  if declare -F log >/dev/null 2>&1; then
    log "$*"
  else
    echo "$*"
  fi
}

# _ppg_gate_hint <reason> <pr_number>
#   What a human should actually do about this gated PR. Pure string
#   selection — it makes no decision, and no PR's classification depends on
#   it.
#
#   It exists because the line used to end "awaiting intake-approved from a
#   maintainer" for every reason, and that is FALSE for
#   external_pr_head_unrecorded: such a PR *is* intake-approved, by a trusted
#   account. What it lacks is a recorded head. That reason was near
#   unreachable until D#2421 PR 3 bounded the first-observation window, and
#   is now the routine state-dir-loss state — so the misdirection went from
#   theoretical to the thing an operator reads first, and the difference is
#   between running one command and hunting for a maintainer who has already
#   approved the PR.
#
#   D#2444: this used to carry its own case statement here, and
#   scripts/sweep-stuck-prs.sh carried an independent copy that D#2421 PR 3
#   never touched — the sweeper kept telling an already-approved PR it was
#   "awaiting intake-approved". `check-pr`'s JSON now carries this text as
#   `hint`, computed once in scripts/lib/pr_intake_gate.py's `_gate_hint`;
#   `pr_pickup_blocked` parses it into `_PR_GATE_HINT` alongside
#   `_PR_GATE_REASON`, and this function just echoes it back — no case
#   statement left to drift.
_ppg_gate_hint() {
  printf '%s' "$_PR_GATE_HINT"
}

# pr_pickup_blocked <pr_number>
#   Exit 0 when automation must NOT touch this PR. Fail closed: an
#   unparseable or missing verdict blocks, because the failure modes here
#   (network down, gh unauthenticated, trust set unresolvable) are exactly the
#   ones an attacker would rather we treated as "allow".
pr_pickup_blocked() {
  local pr="$1"
  _PR_GATE_REASON=""
  _PR_GATE_HINT=""

  local -a _ppg_check_pr_cmd=(python3 "$_PR_PICKUP_GATE_LIB_DIR/pr_intake_gate.py" check-pr "$pr")
  if [ -n "$_PR_PICKUP_GATE_REPO" ]; then
    _ppg_check_pr_cmd+=(--repo "$_PR_PICKUP_GATE_REPO")
  fi
  local gate_json
  gate_json=$("${_ppg_check_pr_cmd[@]}" 2>/dev/null) || true
  if [ -z "$gate_json" ]; then
    _PR_GATE_REASON="gate_check_failed"
    # No JSON was produced at all, so there is no `hint` field to read — this
    # is the one case that still needs a literal default, matched by the
    # sweeper's equivalent fallback for AC-5.
    _PR_GATE_HINT="awaiting intake-approved from a maintainer"
    return 0
  fi

  # D#2422: one `jq` call replaces the three python3 subprocesses this used
  # to spawn (the gate call above plus a separate python3 per field). `jq`
  # is already a hard dependency of this file (see classify_open_prs below).
  # Line-based output, not @tsv: bash's IFS whitespace-collapsing treats a
  # tab as ordinary whitespace even when IFS is set to just "\t", so two
  # consecutive tabs (an empty middle field, e.g. blocked=false with no
  # reason) silently merge and shift every field after it. Newlines don't
  # collapse under mapfile, so an empty reason/hint stays its own line.
  local blocked
  local -a _ppg_fields
  mapfile -t _ppg_fields < <(printf '%s' "$gate_json" \
    | jq -r '(if has("blocked") then .blocked else true end), (.reason // ""), (.hint // "")' 2>/dev/null)
  if [ "${#_ppg_fields[@]}" -eq 3 ]; then
    blocked="${_ppg_fields[0]}"
    _PR_GATE_REASON="${_ppg_fields[1]}"
    _PR_GATE_HINT="${_ppg_fields[2]}"
  else
    blocked="true"
    _PR_GATE_REASON="gate_check_failed"
    _PR_GATE_HINT=""
  fi
  if [ -z "$_PR_GATE_HINT" ]; then
    _PR_GATE_HINT="awaiting intake-approved from a maintainer"
  fi

  [ "$blocked" = "true" ]
}

# classify_open_prs <open_prs_json>
#   The Step-4 body, moved here unchanged apart from the gate call at the top
#   of each iteration. Sets the four work arrays plus GATED_PRS (audit only —
#   nothing downstream reads it, which is the point).
classify_open_prs() {
  local open_prs="${1:-[]}"

  NEEDS_REVIEW=()
  NEEDS_MERGE=()
  NEEDS_FIX=()
  NEEDS_SECURITY_REVIEW=()
  GATED_PRS=()
  GATED_PR_COUNT=0

  local pr_count
  pr_count=$(echo "$open_prs" | jq 'length' 2>/dev/null || echo 0)
  [ "$pr_count" -gt 0 ] 2>/dev/null || return 0

  local pr_num pr_title labels_json
  local has_code_review has_needs_fix has_security_triggered has_security_passed
  while IFS=$'\t' read -r pr_num pr_title labels_json; do
    [ -z "$pr_num" ] && continue

    # Author gate FIRST — before any label is read, before any array is
    # appended to, and therefore before anything downstream can spawn on this
    # PR or write a label to it.
    if pr_pickup_blocked "$pr_num"; then
      GATED_PRS+=("$pr_num:$pr_title")
      GATED_PR_COUNT=$((GATED_PR_COUNT + 1))
      _ppg_log "  PR #$pr_num gated: not picked up ($_PR_GATE_REASON) — $(_ppg_gate_hint "$_PR_GATE_REASON" "$pr_num")"
      continue
    fi

    has_code_review=$(echo "$labels_json" | jq -r 'map(select(.name == "code-review-passed")) | length' 2>/dev/null || echo 0)
    has_needs_fix=$(echo "$labels_json" | jq -r 'map(select(.name | test("needs-fix|code-review-needs-fix"))) | length' 2>/dev/null || echo 0)
    has_security_triggered=$(echo "$labels_json" | jq -r 'map(select(.name == "security-review-triggered")) | length' 2>/dev/null || echo 0)
    has_security_passed=$(echo "$labels_json" | jq -r 'map(select(.name == "security-review-passed")) | length' 2>/dev/null || echo 0)

    if [ "$has_needs_fix" -gt 0 ]; then
      NEEDS_FIX+=("$pr_num:$pr_title")
      _ppg_log "  PR #$pr_num needs-fix: $pr_title"
    elif [ "$has_code_review" -gt 0 ]; then
      if [ "$has_security_triggered" -gt 0 ] && [ "$has_security_passed" -eq 0 ]; then
        NEEDS_SECURITY_REVIEW+=("$pr_num:$pr_title")
        _ppg_log "  PR #$pr_num awaiting security review: $pr_title"
      else
        NEEDS_MERGE+=("$pr_num:$pr_title")
        _ppg_log "  PR #$pr_num ready to merge: $pr_title"
      fi
    else
      NEEDS_REVIEW+=("$pr_num:$pr_title")
      _ppg_log "  PR #$pr_num needs code review: $pr_title"
    fi
  done < <(echo "$open_prs" | jq -r '.[] | "\(.number)\t\(.title)\t\(.labels|tojson)"' 2>/dev/null)

  # Explicit: a `while` loop's status is that of the last command in its body,
  # and this function is called at top level under `set -e` in the hub.
  return 0
}
