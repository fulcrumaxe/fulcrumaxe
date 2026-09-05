#!/usr/bin/env bash
# scripts/lib/auto-pull-step.sh — the auto_pull step of scripts/post-merge-hook.sh,
# lifted out as a sourceable function (D#1948).
#
# Why this exists at all: the hook computes REPO_ROOT unconditionally from its
# own location —
#
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
#
# — so there is no way to point the shipping script at a fixture repo. A test
# that ran it would fetch, switch to main and pull *the operator's actual
# repository*. The previous test suite worked around that by pasting the step
# into a heredoc and running the paste; the paste then sat unresynced through
# 37 commits to the hook and ended up asserting the exact `rm -f` behaviour that
# D#1911 / PR #1954 removed. A copy can be green while the code that runs after
# every merge is broken.
#
# Taking repo_root as an argument is the whole fix: tests source this file and
# call the shipping function against a throwaway repo pair under `mktemp -d`.
# Same shape as scripts/lib/auto-pull-recover.sh, one level up.
#
# Public contract:
#
#   auto_pull_step <repo_root>
#     0 = pulled, or already current.  The caller should mark the step done.
#     1 = declined or failed.          The caller must NOT mark the step — the
#                                      tree was left alone and a later merge has
#                                      to re-attempt it.
#     2 = fatal: the working tree is dirty, or the switch back to main failed.
#                                      The caller should exit 1 *without*
#                                      marking the step, for the same reason.
#
# Returning a status instead of calling hook_event_mark_step in here is what
# keeps those three cases distinguishable. Step bookkeeping is the hook's job;
# this file only knows how to pull.
#
#   auto_pull_step_teamlog <message>
#     Every team-log write in this file goes through this one function. It is
#     the seam the tests need: rotate-team-log.sh is invoked by absolute path,
#     so a PATH stub does not reach it. Tests redefine this function after
#     sourcing, to capture messages into a temp file.
#
# Bash 4+, git, coreutils. No network beyond the git remote it is pointed at.

_APS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_APS_SCRIPT_DIR="$(cd "${_APS_LIB_DIR}/.." && pwd)"

# Self-contained on purpose: sourcing this file is enough to get both recovery
# halves too, so a caller (or a test) does not have to know the dependency
# graph.
# shellcheck source=scripts/lib/auto-pull-recover.sh
source "${_APS_LIB_DIR}/auto-pull-recover.sh"
# shellcheck source=scripts/lib/auto-pull-stash-recover.sh
source "${_APS_LIB_DIR}/auto-pull-stash-recover.sh"

auto_pull_step_teamlog() {
  bash "${_APS_SCRIPT_DIR}/rotate-team-log.sh" comment "$1" || true
}

auto_pull_step() {
  local repo_root="${1:-}"
  local AUTO_PULL_SUCCESS=0
  local CURRENT_BRANCH DIRTY WARN_MSG ERR_MSG CHECKOUT_OUT CHECKOUT_RC
  local FETCH_OUT FETCH_RC RECOVERY_MSG LOCAL REMOTE
  local AUTO_PULL_BLOCKED_MARKER AUTO_PULL_BLOCKED_MODIFIED_MARKER UNMERGED_FILES UNMERGED_LIST TS
  local BUG_TITLE EXISTING_ISSUE NEW_ISSUE_URL BEHIND_COUNT STASH_NOTE
  local PULL_OUT PULL_RC RETRY_OUT RETRY_RC MSG MODIFIED AHEAD_COUNT CURRENCY_MSG

  CURRENT_BRANCH=$(git -C "$repo_root" branch --show-current 2>/dev/null || echo "")
  if [[ "$CURRENT_BRANCH" != "main" ]]; then
    WARN_MSG="[$(date +%H:%M)] post-merge-hook: parent repo was on '${CURRENT_BRANCH:-unknown}' instead of main — likely worktree contamination — attempting checkout main"
    auto_pull_step_teamlog "$WARN_MSG"
    echo "$WARN_MSG" >&2

    # Check for uncommitted changes — do NOT force-clobber
    DIRTY=$(git -C "$repo_root" status --porcelain 2>/dev/null | head -1 || echo "")
    if [[ -n "$DIRTY" ]]; then
      ERR_MSG="[$(date +%H:%M)] post-merge-hook: ERROR — cannot switch to main, parent repo has uncommitted changes. Run 'git stash && git checkout main && git pull' manually."
      auto_pull_step_teamlog "$ERR_MSG"
      echo "$ERR_MSG" >&2
      # Fatal. The tree is still dirty, so the step is not done and a retry has
      # to re-attempt it once the operator (or a later merge) fixes the tree.
      return 2
    fi

    # Safe to switch — no uncommitted changes
    CHECKOUT_OUT=$(git -C "$repo_root" checkout main 2>&1) && CHECKOUT_RC=0 || CHECKOUT_RC=$?
    if [[ $CHECKOUT_RC -ne 0 ]]; then
      ERR_MSG="[$(date +%H:%M)] post-merge-hook: ERROR — git checkout main failed: $CHECKOUT_OUT"
      auto_pull_step_teamlog "$ERR_MSG"
      echo "$ERR_MSG" >&2
      # Same reasoning: a failed switch means nothing was pulled.
      return 2
    fi

    echo "[post-merge-hook] Switched from '${CURRENT_BRANCH}' to main"
    CURRENT_BRANCH="main"
  fi

  if [[ "$CURRENT_BRANCH" == "main" ]]; then
    # Prune stale worktree entries before pulling — prevents "already used by worktree" errors
    git -C "$repo_root" worktree prune 2>/dev/null || true

    # Fetch first; handle "no such ref was fetched" — symptom of parent on orphan branch
    FETCH_OUT=$(git -C "$repo_root" fetch origin main 2>&1) && FETCH_RC=0 || FETCH_RC=$?
    if [[ $FETCH_RC -ne 0 ]]; then
      if echo "$FETCH_OUT" | grep -q "no such ref was fetched\|couldn't find remote ref"; then
        RECOVERY_MSG="[$(date +%H:%M)] post-merge-hook: fetch origin main failed ('no such ref') — forcing reset to origin/main"
        auto_pull_step_teamlog "$RECOVERY_MSG"
        echo "$RECOVERY_MSG" >&2
        git -C "$repo_root" fetch origin --quiet 2>/dev/null || true
        git -C "$repo_root" checkout -B main origin/main 2>/dev/null || true
      else
        echo "[post-merge-hook] Warning: fetch origin main returned error (non-fatal): $FETCH_OUT" >&2
      fi
    fi

    LOCAL=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || echo "")
    REMOTE=$(git -C "$repo_root" rev-parse origin/main 2>/dev/null || echo "")

    if [[ -n "$LOCAL" && "$LOCAL" == "$REMOTE" ]]; then
      AUTO_PULL_SUCCESS=1 # Already up to date — silent no-op
    else
      # ── Unmerged-paths guard: check BEFORE attempting pull ───────────────────
      # git pull aborts with "unmerged files" error when the index has UU entries.
      # Detect pre-emptively so we can emit one loud warning instead of a cryptic fail.
      AUTO_PULL_BLOCKED_MARKER="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}/auto-pull-blocked"
      AUTO_PULL_BLOCKED_MODIFIED_MARKER="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}/auto-pull-blocked-modified"
      UNMERGED_FILES=$(git -C "$repo_root" diff --name-only --diff-filter=U 2>/dev/null || echo "")
      if [[ -n "$UNMERGED_FILES" ]]; then
        if [[ -f "$AUTO_PULL_BLOCKED_MARKER" ]]; then
          # Marker already set from a previous run — suppress duplicate noise
          echo "[post-merge-hook] auto-pull: unmerged paths detected — skipping pull (already reported, marker present)"
        else
          # First occurrence: emit loud warning, write marker, open Bug Issue
          UNMERGED_LIST=$(echo "$UNMERGED_FILES" | head -5 | tr '\n' ' ')
          TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

          # Write marker to suppress duplicates on subsequent merges
          mkdir -p "$(dirname "$AUTO_PULL_BLOCKED_MARKER")"
          printf 'ts=%s\nfiles=%s\n' "$TS" "$UNMERGED_LIST" > "$AUTO_PULL_BLOCKED_MARKER"

          # One loud team-log comment tagged needs-boss
          WARN_MSG="[$(date +%H:%M)] post-merge-hook: WARNING needs-boss — auto-pull blocked by unmerged paths in parent repo: ${UNMERGED_LIST}. Resolve manually, then rm $AUTO_PULL_BLOCKED_MARKER"
          auto_pull_step_teamlog "$WARN_MSG"
          echo "$WARN_MSG" >&2

          # Open idempotent Bug Issue (deduped by exact title match)
          BUG_TITLE="[Bug] post-merge-hook auto-pull blocked by unmerged paths in parent repo"
          EXISTING_ISSUE=$(gh issue list --repo "${_REPO:-}" --state open \
            --json number,title \
            --jq "[.[] | select(.title == \"$BUG_TITLE\")] | first | .number" \
            2>/dev/null || echo "")
          if [[ -n "$EXISTING_ISSUE" && "$EXISTING_ISSUE" != "null" ]]; then
            gh issue comment "$EXISTING_ISSUE" --repo "${_REPO:-}" \
              --body "Recurred at ${TS}. Unmerged files: ${UNMERGED_LIST}" \
              2>/dev/null || true
            echo "[post-merge-hook] auto-pull: updated Bug Issue #$EXISTING_ISSUE (recurrence)"
          else
            NEW_ISSUE_URL=$(gh issue create --repo "${_REPO:-}" \
              --title "$BUG_TITLE" \
              --label "needs-boss" \
              --body "Detected at ${TS}. Parent repo has unmerged paths blocking auto-pull.

Unmerged files (first 5):
${UNMERGED_FILES}

**Resolution**: resolve conflicts in the repo root (or discard with \`git checkout -- <file>\`), then remove the marker:
\`\`\`
rm ~/.autonomous-forever-state/auto-pull-blocked
\`\`\`
" 2>/dev/null || echo "")
            if [[ -n "$NEW_ISSUE_URL" ]]; then
              echo "[post-merge-hook] auto-pull: opened Bug Issue $NEW_ISSUE_URL"
            else
              echo "[post-merge-hook] auto-pull: could not create Bug Issue (non-fatal)" >&2
            fi
          fi
          echo "[post-merge-hook] auto-pull: marker written — subsequent merges will suppress duplicate warnings"
        fi
      else
        # LC_ALL=C: the branches below gate on git's English error text. Under a
        # translated locale they silently never fire (D#1911, item 9).
        PULL_OUT=$(LC_ALL=C git -C "$repo_root" pull --ff-only origin main 2>&1) && PULL_RC=0 || PULL_RC=$?
        if [[ $PULL_RC -eq 0 ]]; then
          echo "[post-merge-hook] auto-pull: pulled main successfully"
          AUTO_PULL_SUCCESS=1
          # Clear stale markers if a previous blocked run left one
          if [[ -f "${AUTO_PULL_BLOCKED_MARKER:-}" ]]; then
            rm -f "$AUTO_PULL_BLOCKED_MARKER"
            echo "[post-merge-hook] auto-pull: cleared stale auto-pull-blocked marker"
          fi
          if [[ -f "${AUTO_PULL_BLOCKED_MODIFIED_MARKER:-}" ]]; then
            rm -f "$AUTO_PULL_BLOCKED_MODIFIED_MARKER"
            echo "[post-merge-hook] auto-pull: cleared stale auto-pull-blocked-modified marker"
          fi
        else
          # Check for untracked-file conflict
          if echo "$PULL_OUT" | grep -q "untracked working tree files would be overwritten"; then
            # git's stderr tells us *that* there is a collision. It does not tell
            # us which files, because it does not quote-escape the paths and the
            # message has no terminator — see scripts/lib/auto-pull-recover.sh.
            # The set of files acted on is derived from plumbing in there, gated
            # twice, and moved into archive/ rather than deleted.
            if auto_pull_recover_untracked "$repo_root"; then
              RETRY_OUT=$(LC_ALL=C git -C "$repo_root" pull --ff-only origin main 2>&1) && RETRY_RC=0 || RETRY_RC=$?
              if [[ $RETRY_RC -eq 0 ]]; then
                MSG="[$(date +%H:%M)] post-merge-hook: auto-pull recovered an untracked collision — ${AUTO_PULL_RECOVER_SUMMARY}, then pulled cleanly.
${AUTO_PULL_RECOVER_MOVED}${AUTO_PULL_RECOVER_SKIPPED}Displaced files are recoverable — see the README in that archive directory."
                auto_pull_step_teamlog "$MSG"
                echo "$MSG"
                AUTO_PULL_SUCCESS=1
              else
                MSG="[$(date +%H:%M)] post-merge-hook: auto-pull FAILED after moving files aside — ${AUTO_PULL_RECOVER_SUMMARY}.
${AUTO_PULL_RECOVER_MOVED}${AUTO_PULL_RECOVER_SKIPPED}Retry error: $RETRY_OUT"
                auto_pull_step_teamlog "$MSG"
                echo "$MSG" >&2
              fi
            else
              MSG="[$(date +%H:%M)] post-merge-hook: auto-pull SKIPPED — untracked collision, and recovery declined to act (${AUTO_PULL_RECOVER_SUMMARY}). Nothing was moved and nothing was deleted; run git pull manually.
${AUTO_PULL_RECOVER_SKIPPED}Pull output: $PULL_OUT"
              auto_pull_step_teamlog "$MSG"
              echo "$MSG" >&2
            fi
          # Check for modified-file conflict
          elif echo "$PULL_OUT" | grep -q "local changes to the following files would be overwritten\|Your local changes"; then
            MODIFIED=$(echo "$PULL_OUT" | grep -A20 "following files" | tail -n +2 | grep -v "^Please\|^$" | sed 's/^\s*//' | head -5 | tr '\n' ' ')
            if [[ -n "${AUTO_PULL_STASH_RECOVER_DISABLE:-}" ]]; then
              # Kill switch: today's exact behaviour, unchanged. No stash is
              # created, nothing is remediated, nothing is escalated.
              MSG="[$(date +%H:%M)] post-merge-hook: auto-pull SKIPPED — working tree has modifications: ${MODIFIED}. Run git stash + git pull manually."
              auto_pull_step_teamlog "$MSG"
              echo "$MSG" >&2
            elif auto_pull_recover_modified "$repo_root"; then
              # Path-limited stash, pull, and — only once a real 3-way merge
              # check certifies it conflict-free — reapply. See
              # scripts/lib/auto-pull-stash-recover.sh for the full mechanism.
              MSG="[$(date +%H:%M)] post-merge-hook: auto-pull recovered a modified-file collision — ${AUTO_PULL_STASH_SUMMARY}."
              auto_pull_step_teamlog "$MSG"
              echo "$MSG"
              AUTO_PULL_SUCCESS=1
              if [[ -f "${AUTO_PULL_BLOCKED_MODIFIED_MARKER:-}" ]]; then
                rm -f "$AUTO_PULL_BLOCKED_MODIFIED_MARKER"
                echo "[post-merge-hook] auto-pull: cleared stale auto-pull-blocked-modified marker"
              fi
            else
              # Recovery declined — either before touching anything (bound,
              # mid-merge/rebase/cherry-pick, staged index) or after the pull
              # already landed but the reapply was not safe (the stash is
              # preserved either way, never dropped, never reset away).
              #
              # Escalate exactly like the unmerged-paths guard above — marker
              # + one loud needs-boss team-log line + a deduped Bug Issue —
              # with its own marker filename so the two conditions can never
              # mask each other.
              BEHIND_COUNT=$(git -C "$repo_root" rev-list --count HEAD..origin/main 2>/dev/null || echo "unknown")
              STASH_NOTE=""
              [[ -n "${AUTO_PULL_STASH_REF:-}" ]] && STASH_NOTE=" Local content is recoverable from ${AUTO_PULL_STASH_REF}."
              if [[ -f "$AUTO_PULL_BLOCKED_MODIFIED_MARKER" ]]; then
                # Marker already set from a previous run — suppress duplicate noise
                echo "[post-merge-hook] auto-pull: modified-file collision persists — skipping escalation (already reported, marker present)"
              else
                TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

                # Write marker to suppress duplicates on subsequent merges
                mkdir -p "$(dirname "$AUTO_PULL_BLOCKED_MODIFIED_MARKER")"
                printf 'ts=%s\nreason=%s\n' "$TS" "$AUTO_PULL_STASH_SUMMARY" > "$AUTO_PULL_BLOCKED_MODIFIED_MARKER"

                # One loud team-log comment tagged needs-boss
                WARN_MSG="[$(date +%H:%M)] post-merge-hook: WARNING needs-boss — auto-pull could not remedy a modified-file collision (${AUTO_PULL_STASH_SUMMARY}); checkout is ${BEHIND_COUNT} commit(s) behind origin/main.${STASH_NOTE} Resolve manually, then rm $AUTO_PULL_BLOCKED_MODIFIED_MARKER"
                auto_pull_step_teamlog "$WARN_MSG"
                echo "$WARN_MSG" >&2

                # Open idempotent Bug Issue (deduped by exact title match)
                BUG_TITLE="[Bug] post-merge-hook auto-pull blocked by a modified-file collision"
                EXISTING_ISSUE=$(gh issue list --repo "${_REPO:-}" --state open \
                  --json number,title \
                  --jq "[.[] | select(.title == \"$BUG_TITLE\")] | first | .number" \
                  2>/dev/null || echo "")
                if [[ -n "$EXISTING_ISSUE" && "$EXISTING_ISSUE" != "null" ]]; then
                  gh issue comment "$EXISTING_ISSUE" --repo "${_REPO:-}" \
                    --body "Recurred at ${TS}. ${AUTO_PULL_STASH_SUMMARY}. Checkout is ${BEHIND_COUNT} commit(s) behind origin/main.${STASH_NOTE}" \
                    2>/dev/null || true
                  echo "[post-merge-hook] auto-pull: updated Bug Issue #$EXISTING_ISSUE (recurrence)"
                else
                  NEW_ISSUE_URL=$(gh issue create --repo "${_REPO:-}" \
                    --title "$BUG_TITLE" \
                    --label "needs-boss" \
                    --body "Detected at ${TS}. ${AUTO_PULL_STASH_SUMMARY}. Checkout is ${BEHIND_COUNT} commit(s) behind origin/main.${STASH_NOTE}

**Resolution**: resolve the working-tree collision by hand (inspect \`git stash list\`), then remove the marker:
\`\`\`
rm $AUTO_PULL_BLOCKED_MODIFIED_MARKER
\`\`\`
" 2>/dev/null || echo "")
                  if [[ -n "$NEW_ISSUE_URL" ]]; then
                    echo "[post-merge-hook] auto-pull: opened Bug Issue $NEW_ISSUE_URL"
                  else
                    echo "[post-merge-hook] auto-pull: could not create Bug Issue (non-fatal)" >&2
                  fi
                fi
                echo "[post-merge-hook] auto-pull: marker written — subsequent merges will suppress duplicate warnings"
              fi
            fi
          else
            MSG="[$(date +%H:%M)] post-merge-hook: auto-pull FAILED — $PULL_OUT"
            auto_pull_step_teamlog "$MSG"
            echo "$MSG" >&2
          fi
        fi
      fi
    fi
  fi

  # Only report success on a path that actually pulled (or was already current)
  # — never on a decline or a failure, so a retry re-attempts.
  if [[ "$AUTO_PULL_SUCCESS" == "1" ]]; then
    # Post-pull currency assertion: prove the step that just ran actually
    # worked, rather than trusting a zero exit code alone.
    AHEAD_COUNT=$(git -C "$repo_root" rev-list --count HEAD..origin/main 2>/dev/null || echo "")
    if [[ "$AHEAD_COUNT" != "0" ]]; then
      CURRENCY_MSG="[$(date +%H:%M)] post-merge-hook: WARNING — auto_pull reported success but parent repo is still ${AHEAD_COUNT:-unknown} commit(s) behind origin/main."
      auto_pull_step_teamlog "$CURRENCY_MSG"
      echo "$CURRENCY_MSG" >&2
    fi
    return 0
  fi
  return 1
}
