#!/usr/bin/env bash
# memory-triage.sh — query tier classifications in memory files
#
# Reads tier: fields from memory file fixtures in scripts/memory-triage/
# (these are copies of ~/.claude/projects/.../memory/*.md with tier frontmatter added).
#
# Usage:
#   scripts/memory-triage.sh --list-tier project
#   scripts/memory-triage.sh --list-tier transferable
#   scripts/memory-triage.sh --list-tier hardwire-candidate
#   scripts/memory-triage.sh --validate
#
# For the real memory dir, run scripts/memory-triage/apply-tiers.sh once.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE_DIR="$SCRIPT_DIR/memory-triage"

VALID_TIERS="project transferable hardwire-candidate"

usage() {
  echo "Usage: $0 --list-tier <tier> | --validate" >&2
  echo "  tiers: project, transferable, hardwire-candidate" >&2
  exit 1
}

list_tier() {
  local tier="$1"
  # Validate tier value
  local valid=0
  for t in $VALID_TIERS; do
    [ "$t" = "$tier" ] && valid=1
  done
  if [ "$valid" -eq 0 ]; then
    echo "ERROR: unknown tier '$tier'. Valid: $VALID_TIERS" >&2
    exit 1
  fi

  find "$FIXTURE_DIR" -maxdepth 1 -name "*.md" ! -name "MEMORY.md" \
    -exec grep -l "^tier: ${tier}$" {} \; \
    | xargs -r -n1 basename \
    | sort -u
}

validate() {
  local missing=0
  local invalid=0

  while IFS= read -r -d '' f; do
    fname=$(basename "$f")
    [ "$fname" = "MEMORY.md" ] && continue

    tier_line=$(grep -m1 "^tier:" "$f" 2>/dev/null || true)
    if [ -z "$tier_line" ]; then
      echo "MISSING tier: $fname" >&2
      missing=$((missing + 1))
      continue
    fi

    tier_val="${tier_line#tier: }"
    tier_val="${tier_val%"${tier_val##*[! ]}"}"  # rtrim whitespace
    local valid=0
    for t in $VALID_TIERS; do
      [ "$t" = "$tier_val" ] && valid=1
    done
    if [ "$valid" -eq 0 ]; then
      echo "INVALID tier '$tier_val' in: $fname" >&2
      invalid=$((invalid + 1))
    fi
  done < <(find "$FIXTURE_DIR" -maxdepth 1 -name "*.md" -print0)

  if [ "$missing" -gt 0 ] || [ "$invalid" -gt 0 ]; then
    echo "validate: FAIL — $missing missing, $invalid invalid" >&2
    exit 1
  fi
  echo "validate: PASS — all memory files have valid tier fields"
}

[ $# -lt 1 ] && usage

case "$1" in
  --list-tier)
    [ $# -lt 2 ] && usage
    list_tier "$2"
    ;;
  --validate)
    validate
    ;;
  *)
    usage
    ;;
esac
