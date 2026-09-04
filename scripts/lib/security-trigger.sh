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

# Resolve the project repo (project.json → env → fallback)
# shellcheck source=repo-resolve.sh
source "$(dirname "${BASH_SOURCE[0]}")/repo-resolve.sh"
_SECURITY_TRIGGER_REPO="$(_resolve_repo)"

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
