#!/usr/bin/env bash
# Thin wrapper around backend/prompt_drift.py — used by preflight and loop step 7.5.
#
# Usage:
#   bash scripts/check-prompt-drift.sh [--quiet]
#
# Exit codes:
#   0 — no drift detected
#   1 — drift detected (one or more roles missing gate keys)
#   2 — error running detector

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pass-through any arguments (e.g. --quiet) to the Python CLI
export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"
exec python3 "$REPO_ROOT/backend/prompt_drift.py" check "$@"
