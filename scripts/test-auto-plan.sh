#!/usr/bin/env bash
# scripts/test-auto-plan.sh — run auto-plan.sh against a fixture date and
# assert that all required section headings are present in the output file.
#
# Cleans up the fixture file after the run.
#
# Exit 0 on pass, exit 1 on any missing section or other failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DATE="2026-05-14"
FIXTURE_YESTERDAY="2026-05-13"
OUTPUT_FILE="$REPO_ROOT/.autonomous-team/PLAN-${FIXTURE_DATE}.md"
PASS=true

cleanup() {
  rm -f "$OUTPUT_FILE"
}
trap cleanup EXIT

# Remove any leftover fixture from a previous run
rm -f "$OUTPUT_FILE"

echo "test-auto-plan.sh: running auto-plan.sh --date ${FIXTURE_DATE} ..."
bash "$REPO_ROOT/scripts/auto-plan.sh" --date "$FIXTURE_DATE"

if [[ ! -f "$OUTPUT_FILE" ]]; then
  echo "FAIL: output file $OUTPUT_FILE was not created" >&2
  exit 1
fi

if [[ ! -s "$OUTPUT_FILE" ]]; then
  echo "FAIL: output file $OUTPUT_FILE is empty" >&2
  exit 1
fi

# Required section headings
REQUIRED_SECTIONS=(
  "## Yesterday's Results (${FIXTURE_YESTERDAY})"
  "## Carryover"
  "## P0"
  "## P1"
  "## P2"
  "## P3"
  "## P4"
  "## Today's mistakes-to-avoid"
  "## End-of-day target"
)

for section in "${REQUIRED_SECTIONS[@]}"; do
  if grep -qF "$section" "$OUTPUT_FILE"; then
    echo "  PASS: $section"
  else
    echo "  FAIL: missing section: $section" >&2
    PASS=false
  fi
done

# Idempotency: run again — must NOT overwrite
MTIME_BEFORE=$(stat -c '%Y' "$OUTPUT_FILE" 2>/dev/null || stat -f '%m' "$OUTPUT_FILE" 2>/dev/null)
bash "$REPO_ROOT/scripts/auto-plan.sh" --date "$FIXTURE_DATE" 2>&1 | grep -q "already exists"
RC=$?
MTIME_AFTER=$(stat -c '%Y' "$OUTPUT_FILE" 2>/dev/null || stat -f '%m' "$OUTPUT_FILE" 2>/dev/null)
if [[ "$RC" -eq 0 && "$MTIME_BEFORE" == "$MTIME_AFTER" ]]; then
  echo "  PASS: idempotency (second run skipped)"
else
  echo "  FAIL: second run did not skip or modified the file (rc=$RC, mtime $MTIME_BEFORE -> $MTIME_AFTER)" >&2
  PASS=false
fi

if [[ "$PASS" == "true" ]]; then
  echo ""
  echo "test-auto-plan.sh: ALL CHECKS PASSED"
  exit 0
else
  echo ""
  echo "test-auto-plan.sh: SOME CHECKS FAILED" >&2
  exit 1
fi
