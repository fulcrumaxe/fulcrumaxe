#!/usr/bin/env bash
# check-subsystems-index.sh — verify wiki/Subsystems-Index.md is up to date.
#
# Lists all backend/*.py modules (excluding __init__.py, test_*.py, conftest.py)
# and checks that each one is referenced in wiki/Subsystems-Index.md.
#
# Exit 0 — all modules present in the index, or nothing to check in this tree.
# Exit 1 — modules are missing from an existing index, or zero backend
#          modules were found (a broken glob, not a legitimate pass).
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
  echo "[SKIP] wiki/ absent in this tree — not shipped in the open-source export (D#1858). Nothing to check."
  exit 0
fi

# wiki/ can carry files unrelated to this index (an operator runbook, say)
# without carrying the index itself. The index is a local-operator artifact
# (gitignored on the engine plane, untracked on the code plane, synced out by
# scripts/sync-wiki.sh) and has never existed in any CI checkout of either
# plane — an absent index here is not staleness, it's the same "nothing to
# check" state as wiki/ being absent, one directory level down.
if [ ! -f "$INDEX" ]; then
  echo "[SKIP] wiki/ present but wiki/Subsystems-Index.md absent — the index isn't present in any CI checkout of either plane. Nothing to check."
  exit 0
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
  # This branch only fires when the gate's own trigger (a changed backend/*.py
  # file) has already guaranteed at least one exists — reaching it means the
  # glob is broken. Discovering zero files is a failure, not a pass (same
  # principle scripts/ci/guard-registry-check.py applies to its own glob).
  echo "[FAIL] No backend/*.py modules found — the glob is broken." >&2
  exit 1
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
