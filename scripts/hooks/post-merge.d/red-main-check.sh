#!/usr/bin/env bash
# scripts/hooks/post-merge.d/red-main-check.sh
#
# Post-merge hook step: detect red-main after a PR merges and record
# verdict-overturn(kind=red_main) for each passing role on that PR.
#
# Called by post-merge-hook.sh with:
#   bash scripts/hooks/post-merge.d/red-main-check.sh --pr <N>
#
# Exits 0 unconditionally (non-fatal to the merge pipeline).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

PR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr) PR="$2"; shift 2 ;;
    *)    shift ;;
  esac
done

if [[ -z "${PR:-}" ]]; then
  echo "[red-main-check] No --pr argument; skipping." >&2
  exit 0
fi

python3 "$REPO_ROOT/backend/red_main_check.py" --pr "$PR" || true

exit 0
