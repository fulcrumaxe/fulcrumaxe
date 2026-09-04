#!/usr/bin/env bash
# scripts/rotate-team-log.sh — resolve and rotate the team activity log Issue.
# Subcommands: current | rotate | comment "<msg>"
#
# Repo is resolved from .autonomous-team/config.json → AUTONOMOUS_TEAM_REPO env
# → loud failure if neither is set (via repo-resolve.sh; see D#1870).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"
LOCK="/tmp/team-log-rotate-${REPO//\//-}.lock"
THRESHOLD=2400

_log() { echo "[rotate-team-log] $*" >&2; }

# _close_old_logs: if multiple team-log issues are open, close all but the newest
# (highest issue number). This handles historical drift from parallel log creation.
_close_old_logs() {
  local nums
  # Sort ascending, drop the last (newest = highest number), close the rest
  nums=$(gh issue list --repo "$REPO" --label team-log --state open --limit 200 \
    --json number --jq 'map(.number) | sort | reverse | .[1:][]' 2>/dev/null || true)
  for n in $nums; do
    _log "WARN: closing extra open team-log Issue #$n (backfill — keeping newest)"
    gh issue close "$n" --repo "$REPO" --reason completed 2>/dev/null || true
  done
}

# _current_number: print the active (newest open) team-log Issue number to stdout.
# Returns empty string if none exist.
_current_number() {
  _close_old_logs
  local n
  n=$(gh issue list --repo "$REPO" --label team-log --state open --limit 200 \
    --json number --jq 'sort_by(.number) | reverse | .[0].number' 2>/dev/null || true)
  echo "${n:-}"
}

# _true_comment_count: get the real comment count from the REST API (bypasses the
# 100-item pagination limit of the GraphQL comments field).
_true_comment_count() {
  local n="$1"
  gh api "repos/$REPO/issues/$n" --jq '.comments' 2>/dev/null || echo 0
}

# cmd_current: print the active team-log Issue number. Creates one if none exist.
cmd_current() {
  local n
  n=$(_current_number)
  if [[ -z "$n" || "$n" == "null" ]]; then
    _log "INFO: no open team-log Issue found — creating one"
    n=$(cmd_rotate)
  fi
  echo "$n"
}

# cmd_rotate: atomically create a successor Issue and close the current one.
# flock on LOCK prevents concurrent callers from creating duplicate successors.
cmd_rotate() {
  (
    exec 9>"$LOCK"
    flock 9

    # Re-read current inside the lock (another caller may have already rotated)
    _close_old_logs
    local old
    old=$(_current_number)

    if [[ -z "$old" || "$old" == "null" ]]; then
      # No existing log — just create a fresh one
      local new_num gh_out
      gh_out=$(gh issue create --repo "$REPO" \
        --title "Team Activity Log $(date -u +%Y-%m-%d)" \
        --body "Team activity log. Created $(date -u +%Y-%m-%dT%H:%M:%SZ)." \
        --label team-log 2>&1) || {
          _log "ERROR: gh issue create failed: $gh_out"
          echo "$gh_out" >&2
          return 1
        }
      new_num=$(echo "$gh_out" | tail -1 | grep -oE '[0-9]+$')
      if [[ -z "$new_num" ]]; then
        _log "ERROR: gh issue create output did not contain an issue number: $gh_out"
        echo "$gh_out" >&2
        return 1
      fi
      _log "INFO: created fresh team-log Issue #$new_num"
      echo "$new_num"
      return 0
    fi

    # Create successor
    local new_body="Continuation of team activity log. Previous: #$old (reached 2500-comment cap). $(date -u +%Y-%m-%dT%H:%M:%SZ)."
    local new_num gh_out
    gh_out=$(gh issue create --repo "$REPO" \
      --title "Team Activity Log $(date -u +%Y-%m-%d) (continued from #$old)" \
      --body "$new_body" \
      --label team-log 2>&1) || {
        _log "ERROR: gh issue create failed during rotation: $gh_out"
        echo "$gh_out" >&2
        return 1
      }
    new_num=$(echo "$gh_out" | tail -1 | grep -oE '[0-9]+$')
    if [[ -z "$new_num" ]]; then
      _log "ERROR: gh issue create output did not contain an issue number: $gh_out"
      echo "$gh_out" >&2
      return 1
    fi

    # Post final comment on old Issue (non-fatal if it's already locked at 2500)
    gh issue comment "$old" --repo "$REPO" \
      --body "Continued in #$new_num — this log is full (2500-comment cap)." 2>/dev/null || true

    # Close old Issue
    gh issue close "$old" --repo "$REPO" --reason completed 2>/dev/null || \
      _log "WARN: could not close old team-log Issue #$old (non-fatal)"

    _log "INFO: rotated to issue #$new_num (from #$old)"
    echo "$new_num"
  )
}

# cmd_comment: post a message to the active team-log Issue.
# Proactively rotates if comment count >= THRESHOLD.
# On HTTP 422 "more than 2500 comments", rotates and retries once.
# Non-fatal on all other errors — prints WARN to stderr, exits 0.
cmd_comment() {
  local msg="$1"

  # Support LOG_OVERRIDE for testing (e.g. LOG_OVERRIDE=2 forces Issue #2)
  local n
  if [[ -n "${LOG_OVERRIDE:-}" ]]; then
    n="$LOG_OVERRIDE"
  else
    n=$(cmd_current)
  fi

  if [[ -z "$n" || "$n" == "null" ]]; then
    _log "WARN: could not determine active team-log Issue — message dropped: $msg"
    return 0
  fi

  # Proactive threshold check
  local count
  count=$(_true_comment_count "$n")
  if [[ "$count" -ge "$THRESHOLD" ]]; then
    _log "INFO: Issue #$n at $count comments (>= $THRESHOLD threshold) — rotating before post"
    n=$(cmd_rotate)
    if [[ -z "$n" || "$n" == "null" ]]; then
      _log "WARN: rotation failed — message dropped: $msg"
      return 0
    fi
  fi

  # Attempt to post; catch 422 and rotate-then-retry once
  local err
  if err=$(gh issue comment "$n" --repo "$REPO" --body "$msg" 2>&1); then
    return 0
  fi

  if echo "$err" | grep -q "more than 2500 comments\|Commenting is disabled"; then
    _log "INFO: hit 2500 cap on #$n — rotating and retrying"
    local new_n
    new_n=$(cmd_rotate)
    if [[ -z "$new_n" || "$new_n" == "null" ]]; then
      _log "WARN: rotation failed after cap hit — message dropped: $msg"
      return 0
    fi
    gh issue comment "$new_n" --repo "$REPO" --body "$msg" 2>/dev/null || \
      _log "WARN: retry after rotation also failed — message dropped: $msg"
  else
    _log "WARN: comment failed on Issue #$n: $err"
  fi

  # Always exit 0 — team-log failures are non-fatal
  return 0
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "${1:-current}" in
  current) cmd_current ;;
  rotate)  cmd_rotate ;;
  comment)
    if [[ $# -lt 2 ]]; then
      _log "Usage: $0 comment \"<message>\""
      exit 1
    fi
    shift
    cmd_comment "$1"
    ;;
  *)
    _log "Usage: $0 {current|rotate|comment \"<message>\"}"
    exit 1
    ;;
esac
