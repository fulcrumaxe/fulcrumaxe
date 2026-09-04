#!/usr/bin/env bash
# annotate-proof.sh — ImageMagick annotation for verification screenshots
#
# Adds a colored banner (PASS=green, FAIL=red, WARN=yellow) with label and
# timestamp overlay to a screenshot image.
#
# Usage:
#   ./scripts/annotate-proof.sh --input PATH --output PATH --status PASS|FAIL|WARN
#                                --label "text" [--timestamp ISO8601]
#
# Requirements: ImageMagick (convert) must be installed.
# Run from the repository root.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
INPUT=""
OUTPUT=""
STATUS="PASS"
LABEL=""
TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)     INPUT="$2"; shift 2 ;;
    --output)    OUTPUT="$2"; shift 2 ;;
    --status)    STATUS="${2^^}"; shift 2 ;;   # uppercase
    --label)     LABEL="$2"; shift 2 ;;
    --timestamp) TIMESTAMP="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$INPUT" ]] || [[ -z "$OUTPUT" ]]; then
  echo "Usage: annotate-proof.sh --input PATH --output PATH --status PASS|FAIL|WARN --label TEXT"
  exit 1
fi

if [[ ! -f "$INPUT" ]]; then
  echo "ERROR: Input file not found: $INPUT"
  exit 1
fi

# ---------------------------------------------------------------------------
# Color selection
# ---------------------------------------------------------------------------
case "$STATUS" in
  PASS) COLOR='#3fb950' ;;
  FAIL) COLOR='#f85149' ;;
  WARN) COLOR='#d29922' ;;
  *)    COLOR='#58a6ff' ;;   # blue for INFO / unknown
esac

# ---------------------------------------------------------------------------
# Build annotation text
# ---------------------------------------------------------------------------
ANNOTATION="${STATUS}: ${LABEL} | ${TIMESTAMP}"

# ---------------------------------------------------------------------------
# Ensure output directory exists
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$OUTPUT")"

# ---------------------------------------------------------------------------
# Apply annotation via ImageMagick
# ---------------------------------------------------------------------------
if ! command -v convert >/dev/null 2>&1; then
  echo "WARNING: ImageMagick 'convert' not found — copying input to output unmodified"
  cp "$INPUT" "$OUTPUT"
  exit 0
fi

convert "$INPUT" \
  -gravity North \
  -background "$COLOR" \
  -splice 0x40 \
  -font Courier \
  -pointsize 16 \
  -fill white \
  -annotate +0+12 "$ANNOTATION" \
  "$OUTPUT" 2>/dev/null

echo "Annotated: $OUTPUT (status=$STATUS)"
