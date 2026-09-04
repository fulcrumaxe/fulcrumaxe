#!/usr/bin/env bash
# check-wiki-index.sh — flag any tracked wiki/*.md absent from wiki/_Sidebar.md
# Exit 0 if all tracked pages are indexed, exit 1 if any are missing.
#
# Only checks git-tracked files (git ls-files wiki/*.md) so that untracked
# auto-generated pages (e.g. Changelog.md, PR-Index.md produced by sync-wiki.sh)
# do not trigger false MISSING failures on a fresh checkout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WIKI_DIR="$REPO_ROOT/wiki"
SIDEBAR="$WIKI_DIR/_Sidebar.md"

# wiki/ is internal-only and doesn't ship in the open-source export
# (D#1858) — this tree not having it is expected, not a failure. Only
# error when wiki/ exists but its own sidebar file is missing (the actual
# defect this script exists to catch).
if [ ! -d "$WIKI_DIR" ]; then
  echo "OK: wiki/ not present in this tree (not shipped in the open-source export) — nothing to check."
  exit 0
fi

if [ ! -f "$SIDEBAR" ]; then
  echo "ERROR: $SIDEBAR not found"
  exit 1
fi

MISSING=0

while IFS= read -r tracked; do
  name=$(basename "$tracked" .md)

  # Skip _Sidebar itself
  [ "$name" = "_Sidebar" ] && continue

  # Skip _template files
  [[ "$name" == _* ]] && continue

  if ! grep -q "$name" "$SIDEBAR"; then
    echo "MISSING: wiki/${name}.md"
    MISSING=$((MISSING + 1))
  fi
done < <(git -C "$REPO_ROOT" ls-files wiki/*.md)

if [ "$MISSING" -gt 0 ]; then
  echo ""
  echo "ERROR: $MISSING page(s) not indexed in wiki/_Sidebar.md"
  exit 1
fi

echo "OK: all tracked wiki pages are indexed in _Sidebar.md"
exit 0
