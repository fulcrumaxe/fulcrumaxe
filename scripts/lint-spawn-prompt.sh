#!/usr/bin/env bash
# scripts/lint-spawn-prompt.sh — Check that the BRIEF section of a rendered
# spawn prompt does not exceed the line-count hard cap.
#
# Usage:
#   bash scripts/lint-spawn-prompt.sh --role <role>
#   bash scripts/lint-spawn-prompt.sh --role <role> --brief-text <text>
#   bash scripts/lint-spawn-prompt.sh --role executor --brief-text "$(cat brief.md)"
#
# Exit codes:
#   0 — BRIEF is within hard cap (≤ 250 lines)
#   1 — BRIEF exceeds hard cap (> 250 lines)
#   2 — usage error
#
# Warns (non-fatal) when BRIEF is between 151 and 250 lines (soft cap).
#
# How BRIEF is counted:
#   The rendered template body is searched for a block starting at ## BRIEF
#   and ending at the next ## heading (or end of file).  The line count of that
#   block determines whether the hard/soft cap is triggered.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ROLE=""
BRIEF_TEXT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)       ROLE="$2";       shift 2 ;;
    --brief-text) BRIEF_TEXT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --role <role> [--brief-text <text>]"
      echo "  --role        Role to render (required)"
      echo "  --brief-text  Override brief text instead of rendering from role"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ROLE" ]]; then
  echo "Error: --role is required" >&2
  exit 2
fi

HARD_CAP=250
SOFT_CAP=150

# If --brief-text not provided, render the template and extract the BRIEF section
if [[ -z "$BRIEF_TEXT" ]]; then
  TMPL_FILE="$REPO_ROOT/backend/spawn_templates/${ROLE}.tmpl"
  if [[ ! -f "$TMPL_FILE" ]]; then
    echo "WARN: lint-spawn-prompt: no template found for role='$ROLE' — skipping lint" >&2
    exit 0
  fi
  RENDERED=$(python3 "$REPO_ROOT/backend/spawn_templates.py" render "$ROLE" \
    --body-only \
    --ignore-unknown-vars 2>/dev/null) || {
    echo "ERROR: lint-spawn-prompt: failed to render template for role='$ROLE'" >&2
    exit 1
  }
  # Extract the ## BRIEF section: from "## BRIEF" to the next "## " heading
  BRIEF_TEXT=$(echo "$RENDERED" | python3 -c "
import sys, re
text = sys.stdin.read()
m = re.search(r'## BRIEF\n(.*?)(?=\n## |\Z)', text, re.DOTALL)
if m:
    print(m.group(1).rstrip())
" 2>/dev/null || echo "")
fi

if [[ -z "$BRIEF_TEXT" ]]; then
  echo "INFO: lint-spawn-prompt: no ## BRIEF section found in role='$ROLE' — skipping lint" >&2
  exit 0
fi

BRIEF_LINES=$(echo "$BRIEF_TEXT" | wc -l)

if [[ "$BRIEF_LINES" -gt "$HARD_CAP" ]]; then
  echo "ERROR: lint-spawn-prompt: BRIEF section for role='$ROLE' is ${BRIEF_LINES} lines (hard cap is ${HARD_CAP}). Trim the BRIEF before spawning." >&2
  exit 1
fi

if [[ "$BRIEF_LINES" -gt "$SOFT_CAP" ]]; then
  echo "WARN: lint-spawn-prompt: BRIEF section for role='$ROLE' is ${BRIEF_LINES} lines (soft cap is ${SOFT_CAP}). Consider trimming." >&2
fi

echo "OK: lint-spawn-prompt: BRIEF for role='$ROLE' is ${BRIEF_LINES} lines (hard cap=${HARD_CAP}, soft cap=${SOFT_CAP})"
exit 0
