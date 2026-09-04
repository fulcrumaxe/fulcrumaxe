#!/usr/bin/env bash
# scripts/wiki-linkcheck.sh — verify every repo-path reference in the wiki and
# README actually exists in the working tree.
#
# Extracts backtick-quoted tokens matching known repo-path prefixes
# (scripts/*, backend/*, wiki/*, dashboard/*, hooks/*, .autonomous-team/*,
# LICENSE) from wiki/**/*.md (all subdirs) plus README.md, resolves each
# against the working tree, and exits non-zero listing every path that
# doesn't exist. Dependency-free — pure bash + grep + test.
#
# Usage:
#   bash scripts/wiki-linkcheck.sh
#
# Exit codes:
#   0 — every referenced path resolves
#   1 — at least one referenced path is missing

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Files to scan: README.md + every markdown file under wiki/ (all subdirs).
FILES=(README.md)
while IFS= read -r -d '' f; do
    FILES+=("$f")
done < <(find wiki -type f -name '*.md' -print0 2>/dev/null)

# Prefixes we consider a "repo path" worth checking. LICENSE is matched as a
# bare filename (no trailing slash prefix).
PREFIX_PATTERN='(scripts|backend|wiki|dashboard|hooks|\.autonomous-team)/[A-Za-z0-9_./*-]+|LICENSE'

TOTAL=0
MISSING=0
declare -A REPORTED

for file in "${FILES[@]}"; do
    [ -f "$file" ] || continue

    # Extract backtick-quoted spans, then filter to ones matching our prefix
    # pattern. grep -oE on each line for `...` spans is simplest & dependency-free.
    while IFS= read -r match; do
        [ -z "$match" ] && continue

        # Strip a trailing glob (*.md, *.json) down to the directory part —
        # globs aren't real paths, so we check the containing directory.
        candidate="$match"
        base_check="$candidate"
        if [[ "$candidate" == *'*'* ]]; then
            base_check="${candidate%/*}"
            [ -z "$base_check" ] && continue
        fi

        # Strip a trailing punctuation character that's part of prose, not the path
        # (e.g. "`wiki/Home.md`," or "`LICENSE`.")
        base_check="${base_check%,}"
        base_check="${base_check%.}"
        base_check="${base_check%:}"
        base_check="${base_check%;}"
        base_check="${base_check%)}"

        TOTAL=$((TOTAL + 1))

        if [ ! -e "$base_check" ]; then
            key="${base_check}"
            if [ -z "${REPORTED[$key]:-}" ]; then
                REPORTED[$key]=1
                MISSING=$((MISSING + 1))
                echo "MISSING: $base_check  (referenced in $file)"
            fi
        fi
    done < <(grep -oE '`[^`]+`' "$file" 2>/dev/null | sed -E 's/^`(.*)`$/\1/' | grep -oE "$PREFIX_PATTERN" 2>/dev/null)
done

echo "---"
echo "wiki-linkcheck: checked $TOTAL path reference(s) across ${#FILES[@]} file(s), $MISSING missing"

if [ "$MISSING" -gt 0 ]; then
    exit 1
fi

exit 0
