#!/usr/bin/env bash
# spawn-run-analyst-if-stale.sh -- invoke run_analyst.py if latest report is >24h old.
# Called from /loop step 7.5 when gates.run_analyst_periodic is true.
# Usage: bash scripts/spawn-run-analyst-if-stale.sh [--file-discussions]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORTS_DIR="${REPO_ROOT}/.autonomous-team/run-reports"
GATE_KEY="gates.run_analyst_periodic"
MAX_AGE_SECONDS=86400  # 24 hours

# Check gate
GATE=$(python3 "${REPO_ROOT}/backend/control_plane.py" get "${GATE_KEY}" 2>/dev/null || echo "false")
if [[ "${GATE}" != "true" ]]; then
  echo "spawn-run-analyst-if-stale: gate ${GATE_KEY}=false -- skipping"
  exit 0
fi

# Find most recent report
mkdir -p "${REPORTS_DIR}"
LATEST=$(find "${REPORTS_DIR}" -name "*.json" -type f -printf "%T@ %p
" 2>/dev/null | sort -n | tail -1 | awk "{print \$2}")

if [[ -n "${LATEST}" ]]; then
  FILE_AGE=$(( $(date +%s) - $(date -r "${LATEST}" +%s) ))
  if [[ "${FILE_AGE}" -lt "${MAX_AGE_SECONDS}" ]]; then
    echo "spawn-run-analyst-if-stale: latest report is ${FILE_AGE}s old (< ${MAX_AGE_SECONDS}s) -- skipping"
    exit 0
  fi
fi

echo "spawn-run-analyst-if-stale: running analysis (report stale or missing)"
python3 "${REPO_ROOT}/backend/run_analyst.py" "$@"
