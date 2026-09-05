#!/usr/bin/env bash
# scripts/ci/readme-links.sh — verify every markdown link target and every
# `.claude/`-prefixed backtick path reference in README.md resolves inside
# the tree it is pointed at.
#
# Ported from open-source/checks/readme-links.sh (D#2348 PR-i). The original
# still exists and still runs from open-source/verify-export.sh against a
# produced export tree; this one defaults to the repository itself, because
# once development happens in the public repo the repository IS the tree a
# reader clones and follows.
#
# The check logic is unchanged. The only difference is the default target,
# and the reason that difference matters is worth stating plainly: the
# original could only ever be wrong about the export, so a README link that
# pointed at a file the export did not carry was the whole defect class.
# Run against the source tree it answers a narrower question — does every
# README reference resolve at all — and that is the question that survives
# the cutover.
#
# scripts/wiki-linkcheck.sh already covers backtick-quoted
# scripts|backend|dashboard|hooks|.autonomous-team paths in README.md, but
# it does not parse markdown `[text](path)` link targets and its prefix
# pattern does not include `.claude/`. This script closes exactly that gap
# rather than duplicating what wiki-linkcheck.sh already checks. See D#1861.
#
# Usage: bash scripts/ci/readme-links.sh [target-dir]     (default: repo root)
#
# Exit 0 = every reference resolves.
# Exit 1 = at least one reference is missing.
# Exit 2 = usage/argument error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

if [[ ! -d "$TARGET_DIR" ]]; then
  echo "error: target dir not found: $TARGET_DIR" >&2
  exit 2
fi

README="$TARGET_DIR/README.md"
if [[ ! -f "$README" ]]; then
  echo "error: $README not found" >&2
  exit 2
fi

TOTAL=0
MISSING=0
declare -A REPORTED

check_path() {
  local raw="$1"
  raw="${raw%%#*}"   # drop a trailing #fragment
  [[ -z "$raw" ]] && return
  TOTAL=$((TOTAL + 1))
  if [[ ! -e "$TARGET_DIR/$raw" ]]; then
    if [[ -z "${REPORTED[$raw]:-}" ]]; then
      REPORTED[$raw]=1
      MISSING=$((MISSING + 1))
      echo "MISSING: $raw  (referenced in README.md)"
    fi
  fi
}

# 1. Markdown [text](path) targets — skip http(s), mailto, and bare anchors.
while IFS= read -r target; do
  [[ -z "$target" ]] && continue
  case "$target" in
    http://*|https://*|mailto:*|\#*) continue ;;
  esac
  check_path "$target"
done < <(grep -oE '\]\([^)]+\)' "$README" | sed -E 's/^\]\((.*)\)$/\1/')

# 2. Backtick-quoted .claude/... paths — the one prefix wiki-linkcheck.sh
# doesn't scan for.
while IFS= read -r match; do
  [[ -z "$match" ]] && continue
  candidate="$match"
  if [[ "$candidate" == *'*'* ]]; then
    candidate="${candidate%/*}"
    [[ -z "$candidate" ]] && continue
  fi
  candidate="${candidate%,}"
  candidate="${candidate%.}"
  candidate="${candidate%:}"
  candidate="${candidate%;}"
  candidate="${candidate%)}"
  check_path "$candidate"
done < <(grep -oE '`[^`]+`' "$README" | sed -E 's/^`(.*)`$/\1/' | grep -oE '\.claude/[A-Za-z0-9_./*-]+')

echo "---"
echo "readme-links: checked $TOTAL reference(s), $MISSING missing"

if [[ "$MISSING" -gt 0 ]]; then
  exit 1
fi

exit 0
