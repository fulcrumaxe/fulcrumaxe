#!/usr/bin/env bash
# apply-tiers.sh — ONE-TIME migration: copy tier: frontmatter from fixtures
# into the real memory dir at ~/.claude/projects/.../memory/
#
# Run this once after merging Sub-PR 1 of D#874:
#   bash scripts/memory-triage/apply-tiers.sh             # apply (with .bak backup per file)
#   bash scripts/memory-triage/apply-tiers.sh --dry-run   # print planned changes only
#
# It is idempotent — re-running skips files that already have a tier line.
# Backups are written alongside each modified file (foo.md.bak).

set -euo pipefail

DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    -h|--help)
      echo "Usage: $0 [--dry-run]"
      echo "  --dry-run  Print planned changes without modifying files."
      exit 0 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE_DIR="$SCRIPT_DIR"
REAL_MEMORY_DIR="$HOME/.claude/projects/-home-agent-autonomous-forever/memory"

if [ ! -d "$REAL_MEMORY_DIR" ]; then
  echo "ERROR: memory dir not found: $REAL_MEMORY_DIR" >&2
  exit 1
fi

if $DRY_RUN; then
  echo "[DRY RUN] No files will be modified."
fi

patched=0
skipped=0
missing=0

for fixture in "$FIXTURE_DIR"/*.md; do
  fname=$(basename "$fixture")
  [ "$fname" = "MEMORY.md" ] && continue

  real="$REAL_MEMORY_DIR/$fname"
  if [ ! -f "$real" ]; then
    echo "SKIP (not in real dir): $fname" >&2
    missing=$((missing + 1))
    continue
  fi

  # Already has tier — skip
  if grep -q "^tier:" "$real" 2>/dev/null; then
    skipped=$((skipped + 1))
    continue
  fi

  # Extract tier from fixture
  tier_line=$(grep -m1 "^tier:" "$fixture" 2>/dev/null || true)
  if [ -z "$tier_line" ]; then
    echo "WARN: fixture has no tier: $fname" >&2
    continue
  fi
  tier_val="${tier_line#tier: }"

  # Find closing --- of frontmatter in real file and insert tier before it
  closing=$(grep -n "^---" "$real" | awk -F: 'NR==2{print $1}')
  if [ -z "$closing" ]; then
    echo "WARN: no closing frontmatter in real file: $fname" >&2
    continue
  fi

  if $DRY_RUN; then
    echo "WOULD PATCH [$tier_val]: $fname (insert at line $closing)"
  else
    cp "$real" "${real}.bak"
    # Portable line-insert (D#2263): sed's "Ni text" insert form needs the
    # "i\"-newline spelling on BSD sed (macOS), so a plain "${closing}i ..."
    # GNU-style call would silently break there. awk works identically on
    # both, and a one-off insert doesn't earn its own platform-compat.sh
    # helper for a single caller.
    awk -v n="$closing" -v line="tier: ${tier_val}" \
      'NR==n{print line} {print}' "$real" > "${real}.tmp" && mv "${real}.tmp" "$real"
    echo "PATCHED [$tier_val]: $fname (backup: ${fname}.bak)"
  fi
  patched=$((patched + 1))
done

echo ""
echo "apply-tiers: patched=$patched skipped=$skipped missing=$missing"
