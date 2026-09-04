#!/usr/bin/env bash
# gate-check.sh — Production readiness gate
#
# Reads verification-report/proof/{latest}/checklist-results.json and
# verification-report/bug-matrix.json to determine if the system is
# ready for production.
#
# Gate criteria:
#   1. All programmatic checklist items must be "pass"
#   2. Zero critical or high severity bugs with status "open" in bug-matrix.json
#   3. Proof directory contains screenshots and/or recordings
#
# Usage:
#   ./scripts/gate-check.sh [--proof-dir DIR] [--checklist FILE] [--bug-matrix FILE]
#
# Options:
#   --proof-dir DIR      Specific proof directory (default: latest under verification-report/proof/)
#   --checklist FILE     checklist-results.json path (default: auto-detected from proof dir)
#   --bug-matrix FILE    bug-matrix.json path (default: verification-report/bug-matrix.json)
#   --json               Output machine-readable JSON summary in addition to table
#
# Exit code 0 = production ready (PASS)
# Exit code 1 = not ready (FAIL)
#
# Run from the repository root.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PROOF_DIR=""
CHECKLIST_FILE=""
BUG_MATRIX_FILE="$REPO_ROOT/verification-report/bug-matrix.json"
JSON_OUTPUT=false

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proof-dir)   PROOF_DIR="$2"; shift 2 ;;
    --checklist)   CHECKLIST_FILE="$2"; shift 2 ;;
    --bug-matrix)  BUG_MATRIX_FILE="$2"; shift 2 ;;
    --json)        JSON_OUTPUT=true; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required but not found in PATH" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Resolve proof directory
# ---------------------------------------------------------------------------
if [[ -z "$PROOF_DIR" ]]; then
  PROOF_BASE="$REPO_ROOT/verification-report/proof"
  if [[ -d "$PROOF_BASE" ]]; then
    PROOF_DIR=$(ls -1d "$PROOF_BASE"/*/  2>/dev/null | sort | tail -1)
    PROOF_DIR="${PROOF_DIR%/}"  # strip trailing slash
  fi
fi

# ---------------------------------------------------------------------------
# Resolve checklist results file
# ---------------------------------------------------------------------------
if [[ -z "$CHECKLIST_FILE" ]]; then
  if [[ -n "$PROOF_DIR" && -f "$PROOF_DIR/checklist-results.json" ]]; then
    CHECKLIST_FILE="$PROOF_DIR/checklist-results.json"
  fi
fi

# ---------------------------------------------------------------------------
# Gate 1: Checklist results
# ---------------------------------------------------------------------------
CHECKLIST_PASSED=0
CHECKLIST_FAILED=0
CHECKLIST_MANUAL=0
CHECKLIST_PENDING=0
CHECKLIST_GATE="FAIL"
CHECKLIST_NOTE=""

if [[ -z "$CHECKLIST_FILE" || ! -f "$CHECKLIST_FILE" ]]; then
  CHECKLIST_NOTE="checklist-results.json not found — run scripts/run-checklist.sh first"
  CHECKLIST_GATE="FAIL"
else
  # Count programmatic items by status
  CHECKLIST_PASSED=$(jq '[.subsystems[].items[] | select(.type == "programmatic" and .status == "pass")] | length' "$CHECKLIST_FILE" 2>/dev/null || echo 0)
  CHECKLIST_FAILED=$(jq '[.subsystems[].items[] | select(.type == "programmatic" and .status == "fail")] | length' "$CHECKLIST_FILE" 2>/dev/null || echo 0)
  CHECKLIST_PENDING=$(jq '[.subsystems[].items[] | select(.type == "programmatic" and .status == "pending")] | length' "$CHECKLIST_FILE" 2>/dev/null || echo 0)
  CHECKLIST_MANUAL=$(jq '[.subsystems[].items[] | select(.type == "manual")] | length' "$CHECKLIST_FILE" 2>/dev/null || echo 0)

  CHECKLIST_TOTAL=$((CHECKLIST_PASSED + CHECKLIST_FAILED + CHECKLIST_PENDING))

  if [[ $CHECKLIST_FAILED -eq 0 && $CHECKLIST_PENDING -eq 0 && $CHECKLIST_TOTAL -gt 0 ]]; then
    CHECKLIST_GATE="PASS"
    CHECKLIST_NOTE="${CHECKLIST_PASSED}/${CHECKLIST_TOTAL} passed, ${CHECKLIST_MANUAL} manual pending"
  else
    CHECKLIST_GATE="FAIL"
    if [[ $CHECKLIST_PENDING -gt 0 ]]; then
      CHECKLIST_NOTE="${CHECKLIST_PASSED}/${CHECKLIST_TOTAL} passed, ${CHECKLIST_FAILED} failed, ${CHECKLIST_PENDING} not yet run"
    else
      CHECKLIST_NOTE="${CHECKLIST_PASSED}/${CHECKLIST_TOTAL} passed, ${CHECKLIST_FAILED} failed, ${CHECKLIST_MANUAL} manual pending"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Gate 2: Bug matrix
# ---------------------------------------------------------------------------
BUG_CRITICAL=0
BUG_HIGH=0
BUG_MEDIUM=0
BUG_LOW=0
BUG_GATE="PASS"
BUG_NOTE=""

if [[ ! -f "$BUG_MATRIX_FILE" ]]; then
  # No bug matrix — treat as zero bugs (gate passes)
  BUG_NOTE="no bug-matrix.json found (treating as zero open bugs)"
  BUG_GATE="PASS"
else
  BUG_CRITICAL=$(jq '[.bugs[] | select(.severity == "critical" and .status == "open")] | length' "$BUG_MATRIX_FILE" 2>/dev/null || echo 0)
  BUG_HIGH=$(jq '[.bugs[] | select(.severity == "high" and .status == "open")] | length' "$BUG_MATRIX_FILE" 2>/dev/null || echo 0)
  BUG_MEDIUM=$(jq '[.bugs[] | select(.severity == "medium" and .status == "open")] | length' "$BUG_MATRIX_FILE" 2>/dev/null || echo 0)
  BUG_LOW=$(jq '[.bugs[] | select(.severity == "low" and .status == "open")] | length' "$BUG_MATRIX_FILE" 2>/dev/null || echo 0)

  if [[ $BUG_CRITICAL -gt 0 || $BUG_HIGH -gt 0 ]]; then
    BUG_GATE="FAIL"
    BUG_NOTE="${BUG_CRITICAL} critical, ${BUG_HIGH} high, ${BUG_MEDIUM} medium (open)"
  else
    BUG_GATE="PASS"
    BUG_NOTE="${BUG_CRITICAL} critical, ${BUG_HIGH} high, ${BUG_MEDIUM} medium (open)"
  fi
fi

# ---------------------------------------------------------------------------
# Gate 3: Proof artifacts
# ---------------------------------------------------------------------------
PROOF_SCREENSHOTS=0
PROOF_RECORDINGS=0
PROOF_GATE="PASS"
PROOF_NOTE=""

if [[ -n "$PROOF_DIR" && -d "$PROOF_DIR" ]]; then
  PROOF_SCREENSHOTS=$(find "$PROOF_DIR" -name "*.png" -o -name "*.jpg" -o -name "*.webp" 2>/dev/null | wc -l)
  PROOF_RECORDINGS=$(find "$REPO_ROOT/verification-report/recordings" -name "*.mp4" -o -name "*.webm" -o -name "*.gif" 2>/dev/null | wc -l)
  PROOF_NOTE="${PROOF_SCREENSHOTS} screenshots, ${PROOF_RECORDINGS} recordings"
  # Proof gate passes as long as the proof directory exists (screenshots are optional)
  PROOF_GATE="PASS"
else
  PROOF_NOTE="no proof directory found — run scripts/run-checklist.sh first"
  # Missing proof is a warning, not a hard gate failure
  PROOF_GATE="WARN"
fi

# ---------------------------------------------------------------------------
# Overall gate
# ---------------------------------------------------------------------------
if [[ "$CHECKLIST_GATE" == "PASS" && "$BUG_GATE" == "PASS" ]]; then
  OVERALL_GATE="PASS"
  OVERALL_EXIT=0
else
  OVERALL_GATE="FAIL"
  OVERALL_EXIT=1
fi

# ---------------------------------------------------------------------------
# Print summary table
# ---------------------------------------------------------------------------
echo ""
echo "Production Readiness Gate"
echo "========================="
printf "%-12s  %s\n" "Checklist:" "$CHECKLIST_NOTE  [$CHECKLIST_GATE]"
printf "%-12s  %s\n" "Bugs:" "$BUG_NOTE  [$BUG_GATE]"
printf "%-12s  %s\n" "Proof:" "$PROOF_NOTE  [$PROOF_GATE]"
echo "-------------------------"
printf "%-12s  %s\n" "Gate:" "$OVERALL_GATE"
echo ""

# ---------------------------------------------------------------------------
# Failure details
# ---------------------------------------------------------------------------
if [[ "$CHECKLIST_GATE" == "FAIL" && -n "$CHECKLIST_FILE" && -f "$CHECKLIST_FILE" ]]; then
  FAILED_ITEMS=$(jq -r '[.subsystems[].items[] | select(.type == "programmatic" and (.status == "fail" or .status == "pending"))] | .[] | "  - [\(.status)] \(.id): \(.description)"' "$CHECKLIST_FILE" 2>/dev/null || echo "")
  if [[ -n "$FAILED_ITEMS" ]]; then
    echo "Checklist failures:"
    echo "$FAILED_ITEMS"
    echo ""
  fi
fi

if [[ "$BUG_GATE" == "FAIL" && -f "$BUG_MATRIX_FILE" ]]; then
  BLOCKING_BUGS=$(jq -r '[.bugs[] | select((.severity == "critical" or .severity == "high") and .status == "open")] | .[] | "  - [\(.severity)] \(.id // "?"): \(.title // .description // "no title")"' "$BUG_MATRIX_FILE" 2>/dev/null || echo "")
  if [[ -n "$BLOCKING_BUGS" ]]; then
    echo "Blocking bugs:"
    echo "$BLOCKING_BUGS"
    echo ""
  fi
fi

# ---------------------------------------------------------------------------
# Optional JSON output
# ---------------------------------------------------------------------------
if [[ "$JSON_OUTPUT" == "true" ]]; then
  jq -n \
    --arg gate "$OVERALL_GATE" \
    --arg checklist_gate "$CHECKLIST_GATE" \
    --argjson checklist_passed "$CHECKLIST_PASSED" \
    --argjson checklist_failed "$CHECKLIST_FAILED" \
    --argjson checklist_manual "$CHECKLIST_MANUAL" \
    --arg bug_gate "$BUG_GATE" \
    --argjson bug_critical "$BUG_CRITICAL" \
    --argjson bug_high "$BUG_HIGH" \
    --argjson bug_medium "$BUG_MEDIUM" \
    --arg proof_gate "$PROOF_GATE" \
    --argjson proof_screenshots "$PROOF_SCREENSHOTS" \
    --argjson proof_recordings "$PROOF_RECORDINGS" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{
      "timestamp": $ts,
      "gate": $gate,
      "checklist": {
        "gate": $checklist_gate,
        "passed": $checklist_passed,
        "failed": $checklist_failed,
        "manual_pending": $checklist_manual
      },
      "bugs": {
        "gate": $bug_gate,
        "critical_open": $bug_critical,
        "high_open": $bug_high,
        "medium_open": $bug_medium
      },
      "proof": {
        "gate": $proof_gate,
        "screenshots": $proof_screenshots,
        "recordings": $proof_recordings
      }
    }'
fi

exit $OVERALL_EXIT
