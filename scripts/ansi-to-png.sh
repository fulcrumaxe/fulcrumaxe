#!/usr/bin/env bash
# ansi-to-png.sh — Convert an ANSI text file to a PNG image.
#
# Usage:
#   scripts/ansi-to-png.sh <input.ansi> <output.png>
#
# Dependencies (no sudo required):
#   - ansi2html  (~/.local/bin/ansi2html, installed via pip)
#   - convert    (ImageMagick)
#
# Falls back to plain-text rendering via ImageMagick if ansi2html is unavailable.

set -euo pipefail

INPUT="${1:-}"
OUTPUT="${2:-}"

if [[ -z "$INPUT" || -z "$OUTPUT" ]]; then
  echo "Usage: $0 <input.ansi> <output.png>" >&2
  exit 1
fi

if [[ ! -f "$INPUT" ]]; then
  echo "Error: input file '$INPUT' not found" >&2
  exit 1
fi

# Ensure ~/.local/bin is on PATH so ansi2html is found
export PATH="$HOME/.local/bin:$PATH"

TMPDIR_LOCAL="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

TMP_HTML="$TMPDIR_LOCAL/screen.html"
TMP_STRIPPED="$TMPDIR_LOCAL/screen.txt"

# ── Primary path: ansi2html → ImageMagick ──────────────────────────────────
if command -v ansi2html &>/dev/null; then
  # ansi2html --inline produces self-contained HTML with inline styles.
  ansi2html --inline < "$INPUT" > "$TMP_HTML" 2>/dev/null || {
    echo "[ansi-to-png] ansi2html failed — falling back to plain text" >&2
    rm -f "$TMP_HTML"
  }
fi

if [[ -f "$TMP_HTML" && -s "$TMP_HTML" ]]; then
  # Try wkhtmltoimage first (produces best rendering of HTML).
  if command -v wkhtmltoimage &>/dev/null; then
    wkhtmltoimage --quiet --width 960 "$TMP_HTML" "$OUTPUT" 2>/dev/null && {
      echo "$OUTPUT"
      exit 0
    }
  fi

  # Try xvfb-run + chromium/google-chrome headless rendering.
  CHROME_BIN=""
  for candidate in chromium-browser chromium google-chrome google-chrome-stable; do
    if command -v "$candidate" &>/dev/null; then
      CHROME_BIN="$candidate"
      break
    fi
  done

  if [[ -n "$CHROME_BIN" ]] && command -v xvfb-run &>/dev/null; then
    xvfb-run --auto-servernum "$CHROME_BIN" \
      --headless --disable-gpu --screenshot="$OUTPUT" \
      --window-size=960,600 "file://$TMP_HTML" 2>/dev/null && {
      echo "$OUTPUT"
      exit 0
    }
  fi

  # ImageMagick can read HTML directly in some builds — try it.
  if convert -density 96 "$TMP_HTML" -resize 960x "$OUTPUT" 2>/dev/null; then
    echo "$OUTPUT"
    exit 0
  fi
fi

# ── Fallback: strip ANSI escapes and render plain text via ImageMagick ──────
# sed removes common ANSI escape sequences, leaving readable plain text.
sed 's/\x1B\[[0-9;]*[mKHJABCDsu]//g; s/\x1B\[[0-9;]*[a-zA-Z]//g; s/\r//g' \
  "$INPUT" > "$TMP_STRIPPED"

convert \
  -size 960x600 \
  xc:#1e1e1e \
  -font "Courier" \
  -pointsize 13 \
  -fill "#d4d4d4" \
  -annotate +10+20 "@$TMP_STRIPPED" \
  "$OUTPUT" 2>/dev/null || {
    # Last resort: minimal label image
    convert \
      -size 960x600 xc:#1e1e1e \
      -font "Courier" -pointsize 13 -fill "#d4d4d4" \
      -annotate +10+20 "$(head -40 "$TMP_STRIPPED")" \
      "$OUTPUT"
  }

echo "$OUTPUT"
