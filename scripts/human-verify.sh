#!/usr/bin/env bash
# human-verify.sh — start services and run the human verification checklist.
#
# Usage:
#   bash scripts/human-verify.sh
#   bash scripts/human-verify.sh --skip-service-check
#   bash scripts/human-verify.sh --check-reverify
#
# What it does:
#   1. Checks if the Python API is running (port 18099). Starts it if not.
#   2. Loads the human checklist from verification-report/human-checklist.json.
#   3. Iterates pending/re-verify items, prompting for PASS/FAIL.
#   4. On FAIL: auto-files a SPEC_READY Discussion with the bug details.
#   5. Writes a proof report to verification-report/proof/{timestamp}/human-results.json.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKLIST="${REPO_ROOT}/verification-report/human-checklist.json"
API_PORT=18099
API_PID_FILE="${REPO_ROOT}/.autonomous-team/api.pid"
API_STARTED=false

SKIP_SERVICE_CHECK=false
CHECK_REVERIFY=false

for arg in "$@"; do
  case "$arg" in
    --skip-service-check) SKIP_SERVICE_CHECK=true ;;
    --check-reverify)     CHECK_REVERIFY=true ;;
  esac
done

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

check_api() {
  curl -sf "http://localhost:${API_PORT}/health" > /dev/null 2>&1
}

start_api() {
  echo "Starting Python API on port ${API_PORT}..."
  cd "${REPO_ROOT}"
  python3 backend/api.py &
  API_PID=$!
  echo "$API_PID" > "$API_PID_FILE"
  API_STARTED=true

  # Wait up to 15 seconds for it to be healthy
  for i in $(seq 1 15); do
    if check_api; then
      echo "Python API is ready (PID $API_PID)"
      return 0
    fi
    sleep 1
  done

  echo "WARNING: Python API did not respond after 15 seconds. Continuing anyway."
}

cleanup() {
  if [ "$API_STARTED" = "true" ] && [ -f "$API_PID_FILE" ]; then
    PID=$(cat "$API_PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      echo "Stopping Python API (PID $PID)..."
      kill "$PID" 2>/dev/null || true
    fi
    rm -f "$API_PID_FILE"
  fi
}
trap cleanup EXIT

# -----------------------------------------------------------------------
# Service check
# -----------------------------------------------------------------------

if [ "$SKIP_SERVICE_CHECK" = "false" ]; then
  echo "Checking Python API health..."
  if check_api; then
    echo "Python API is already running on port ${API_PORT}."
  else
    echo "Python API is not running."
    start_api
  fi
else
  echo "Skipping service check (--skip-service-check)."
fi

# -----------------------------------------------------------------------
# Run verification
# -----------------------------------------------------------------------

cd "${REPO_ROOT}"

EXTRA_ARGS=""
if [ "$CHECK_REVERIFY" = "true" ]; then
  EXTRA_ARGS="--check-reverify"
fi

echo ""
echo "Starting human verification session..."
echo "Checklist: ${CHECKLIST}"
echo ""

python3 backend/human_verification.py \
  --checklist "${CHECKLIST}" \
  --skip-service-check \
  $EXTRA_ARGS

EXIT_CODE=$?

echo ""
echo "Verification session complete."
echo "Results saved to: ${CHECKLIST}"
echo "Proof reports:    ${REPO_ROOT}/verification-report/proof/"

exit $EXIT_CODE
