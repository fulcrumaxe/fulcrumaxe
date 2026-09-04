#!/usr/bin/env bash
# scripts/hooks/post-agent.d/scope-drift-check.sh
#
# Posts a "### Scope-drift warning" PR comment when an executor commits files
# that fall outside the Spec's declared file list.
#
# Sourced by post-agent-hook.sh — expects these caller variables:
#   ROLE, VERDICT, PR, DISCUSSION, REPO_ROOT, _REPO
#
# Warning only — never blocks the PR or changes the verdict.
# Skipped silently when: role != executor, no PR, no DISCUSSION, or no Spec file list.

if [[ "$ROLE" == "executor" && "$VERDICT" == "done" && -n "${PR:-}" && -n "${DISCUSSION:-}" ]]; then
  echo "[post-agent-hook] scope_drift_check: executor PR #$PR D#$DISCUSSION"

  # Get declared file list from Spec (empty → skip)
  SPEC_FILES=$(python3 "$REPO_ROOT/backend/spec_file_list.py" "$DISCUSSION" 2>/dev/null || true)

  if [[ -n "$SPEC_FILES" ]]; then
    # Get files committed in the PR
    PR_FILES=$(gh api "repos/$_REPO/pulls/$PR/files" --jq '.[].filename' 2>/dev/null || true)

    if [[ -n "$PR_FILES" ]]; then
      # Files in PR that are NOT in the declared spec list
      DRIFT_FILES=$(comm -23 \
        <(echo "$PR_FILES" | sort) \
        <(echo "$SPEC_FILES" | sort) \
        2>/dev/null || true)

      if [[ -n "$DRIFT_FILES" ]]; then
        BULLET_LIST=$(echo "$DRIFT_FILES" | sed 's/^/- /')
        COMMENT_BODY="### Scope-drift warning

The following files were committed to this PR but are not in the Spec's declared file list:

${BULLET_LIST}

If the changes are intentional (e.g. auto-fixes, collateral cleanup), the reviewer can accept them. Otherwise consider splitting the out-of-scope work into a separate PR.

_Posted automatically by post-agent-hook.sh — Discussion #${DISCUSSION}_"

        gh api "repos/$_REPO/issues/$PR/comments" \
          --method POST \
          --field body="$COMMENT_BODY" \
          2>/dev/null \
          && echo "[post-agent-hook] scope_drift_check: posted drift warning for PR #$PR" \
          || echo "[post-agent-hook] scope_drift_check: WARN — failed to post comment (non-fatal)" >&2
      else
        echo "[post-agent-hook] scope_drift_check: no drift detected for PR #$PR"
      fi
    else
      echo "[post-agent-hook] scope_drift_check: could not fetch PR file list (non-fatal)" >&2
    fi
  else
    echo "[post-agent-hook] scope_drift_check: no declared file list in Spec D#$DISCUSSION — skipping"
  fi
fi
