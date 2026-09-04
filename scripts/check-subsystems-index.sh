#!/usr/bin/env bash
# check-subsystems-index.sh — verify wiki/Subsystems-Index.md is up to date.
#
# Lists all backend/*.py modules (excluding __init__.py, test_*.py, conftest.py)
# and checks that each one is referenced in wiki/Subsystems-Index.md.
#
# Exit 0 — all modules present in the index.
# Exit 1 — one or more modules are missing; names are printed to stderr.
#
# Usage:
#   bash scripts/check-subsystems-index.sh
#   # From preflight.sh (guarded — only runs when backend/*.py files changed):
#   bash scripts/check-subsystems-index.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INDEX="$REPO_ROOT/wiki/Subsystems-Index.md"

# wiki/ (and this index within it) is internal-only and doesn't ship in the
# open-source export (D#1858) — this tree not having it is expected, not a
# failure. Only fail when wiki/ exists but the index inside it is stale
# (the actual regression this script exists to catch, on the team's own
# checkout where wiki/ is present).
if [ ! -d "$REPO_ROOT/wiki" ]; then
  echo "[SKIP] wiki/ not present in this tree (not shipped in the open-source export) — nothing to check."
  exit 0
fi

if [ ! -f "$INDEX" ]; then
  echo "[FAIL] wiki/Subsystems-Index.md does not exist." >&2
  exit 1
fi

# Build sorted list of backend module filenames (basename only, e.g. budget.py)
MODULES=$(
  ls "$REPO_ROOT"/backend/*.py 2>/dev/null \
  | xargs -n1 basename \
  | grep -v '^__init__\.py$' \
  | grep -v '^test_' \
  | grep -v '^conftest\.py$' \
  | sort
)

if [ -z "$MODULES" ]; then
  echo "[WARN] No backend/*.py modules found — nothing to check."
  exit 0
fi

# Build sorted list of modules mentioned in the index.
# Matches any *.py filename appearing in the document (in backticks or table cells).
INDEXED=$(
  grep -oE '[a-zA-Z0-9_]+\.py' "$INDEX" \
  | sort -u
)

MISSING=()
while IFS= read -r mod; do
  if ! echo "$INDEXED" | grep -qx "$mod"; then
    MISSING+=("$mod")
  fi
done <<< "$MODULES"

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "[FAIL] The following backend modules are missing from wiki/Subsystems-Index.md:" >&2
  for m in "${MISSING[@]}"; do
    echo "  - $m" >&2
  done
  echo "" >&2
  echo "Add a row for each missing module and re-run this script." >&2
  exit 1
fi

echo "[PASS] wiki/Subsystems-Index.md covers all $(echo "$MODULES" | wc -l | tr -d ' ') backend modules."
exit 0
