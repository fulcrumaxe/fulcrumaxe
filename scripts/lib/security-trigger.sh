#!/usr/bin/env bash
# scripts/lib/security-trigger.sh — detect security-sensitive patterns in a PR diff.
#
# Usage (source or call directly):
#   source scripts/lib/security-trigger.sh
#   detect_security_trigger <PR_NUMBER>    # returns 0 = triggered, 1 = not triggered
#
# Or standalone:
#   bash scripts/lib/security-trigger.sh <PR_NUMBER>
#   echo $?  # 0 = triggered, 1 = not triggered
#
# Single source of truth for security trigger detection — used by phased orchestration.

# Resolve the CODE plane — this file reads PR diffs, which live with the code.
#
# Failure direction is what makes this the highest-severity site in the audit.
# detect_security_trigger returns 1 ("no trigger") on any API error, which is
# the right call for a transient 403 but is indistinguishable from a real
# "nothing security-sensitive here". Post-cutover a Discussion-plane slug would
# make `gh pr diff` fail for every PR — every PR would report no trigger, and
# the security-review gate would switch itself off silently, in the direction
# that looks healthy. Hence the code plane, and hence the explicit empty check
# in the entry point below.
#
# shellcheck source=repo-resolve.sh
source "$(dirname "${BASH_SOURCE[0]}")/repo-resolve.sh"
# Resolved at source time; checked in detect_security_trigger. A top-level
# `exit` here would kill any caller that sources this file.
_SECURITY_TRIGGER_REPO="$(_resolve_code_repo 2>/dev/null || true)"

# -----------------------------------------------------------------------
# Security-trigger file patterns (matched against changed file names)
# -----------------------------------------------------------------------
SECURITY_FILE_PATTERNS=(
  "server.py"
  "*.env*"
  "auth/"
  "secret/"
  "credential/"
  "token/"
  "manifest*.json"
)

# -----------------------------------------------------------------------
# Security-trigger diff keywords (matched against diff content)
# -----------------------------------------------------------------------
SECURITY_DIFF_KEYWORDS=(
  "api_key"
  "API_KEY"
  "secret"
  "SECRET"
  "token"
  "TOKEN"
  "password"
  "subprocess"
  "spawn"
  "exec("
  "eval("
  "fetch("
  "localStorage"
  "sessionStorage"
  "chrome.storage"
  "__proto__"
  "Content-Security-Policy"
)

# -----------------------------------------------------------------------
# detect_security_trigger <PR_NUMBER>
#
# Returns 0 (triggered) or 1 (not triggered).
# On GitHub API errors (403, network) returns 1 (not triggered) with a
# warning to stderr — caller should treat as "no trigger" and not retry.
# -----------------------------------------------------------------------
detect_security_trigger() {
  local pr="${1:?detect_security_trigger requires a PR number}"

  # An unresolved code plane fails CLOSED — return 0 ("triggered"), forcing a
  # security review rather than skipping one.
  #
  # Returning non-zero here would be wrong in a way that is hard to see: this
  # function's contract is 0 = triggered, non-zero = not triggered, and
  # loop-phased-step5.sh's _check_security_trigger passes that straight through.
  # So *any* non-zero — including a bespoke error code — is read by the only
  # caller as "no security review needed". The one exit status that cannot be
  # misread as a clean bill of health is the one that demands the review.
  #
  # We must also not fall through to `gh` with an empty --repo: `gh pr diff
  # --repo ""` exits 0 against whatever repo the checkout's remote points at,
  # so it would return a real diff for the wrong PR and scan that instead.
  if [ -z "${_SECURITY_TRIGGER_REPO:-}" ]; then
    echo "[security-trigger] ERROR: could not resolve the code repo — failing closed (reporting 'triggered') so a PR is not waved through unscanned. Add a \"code_repo\" (or \"repo\") field to .autonomous-team/config.json." >&2
    return 0
  fi

  # --- Get list of changed files ---
  local changed_files
  changed_files=$(gh pr diff --name-only "$pr" \
    --repo "$_SECURITY_TRIGGER_REPO" 2>/dev/null) || {
    echo "[security-trigger] WARNING: could not fetch file list for PR #$pr (API error)" >&2
    return 1
  }

  # Check file patterns
  local file pattern
  while IFS= read -r file; do
    [ -z "$file" ] && continue
    for pattern in "${SECURITY_FILE_PATTERNS[@]}"; do
      # Shell glob match
      case "$file" in
        *"$pattern"*)
          echo "[security-trigger] triggered by file pattern '$pattern' in '$file'" >&2
          return 0
          ;;
      esac
    done
  done <<< "$changed_files"

  # --- Get diff content and scan keywords ---
  local diff_content
  diff_content=$(gh pr diff "$pr" \
    --repo "$_SECURITY_TRIGGER_REPO" 2>/dev/null) || {
    echo "[security-trigger] WARNING: could not fetch diff for PR #$pr (API error)" >&2
    return 1
  }

  local keyword
  for keyword in "${SECURITY_DIFF_KEYWORDS[@]}"; do
    if echo "$diff_content" | grep -qF "$keyword"; then
      echo "[security-trigger] triggered by diff keyword '$keyword'" >&2
      return 0
    fi
  done

  return 1
}

# -----------------------------------------------------------------------
# Standalone entry point
# -----------------------------------------------------------------------
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  PR="${1:?Usage: security-trigger.sh <PR_NUMBER>}"
  if detect_security_trigger "$PR"; then
    echo "triggered"
    exit 0
  else
    echo "not-triggered"
    exit 1
  fi
fi
