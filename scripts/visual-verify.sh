#!/usr/bin/env bash
# visual-verify.sh — Run a process in tmux, capture screenshots at defined
#                    checkpoints, and emit a JSON manifest of PNG/ANSI paths.
#
# Usage:
#   scripts/visual-verify.sh [target-command]
#
# Defaults:
#   target-command = "cd tui && node dist/index.js"
#
# Output:
#   JSON manifest written to stdout:
#   { "session": "...", "checkpoints": [{ "name":"...", "png":"...", "ansi":"...", "status":"ok|timeout|error" }] }
#
# Environment:
#   ANTHROPIC_API_KEY  — passed into the tmux environment when set
#   VERIFY_OUT_DIR     — override output directory (default: /tmp/verify-checkpoints)
#   CHECKPOINTS_FILE   — override checkpoint definitions file
#
# Dependencies (no sudo required):
#   tmux, jq, ansi2html (~/.local/bin), convert (ImageMagick)

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TARGET_CMD="${1:-cd tui && node dist/index.js}"
SESSION="tui-verify"
OUT_DIR="${VERIFY_OUT_DIR:-/tmp/verify-checkpoints}"
CHECKPOINTS_FILE="${CHECKPOINTS_FILE:-$SCRIPT_DIR/checkpoints.json}"

export PATH="$HOME/.local/bin:$PATH"

# ── Helpers ───────────────────────────────────────────────────────────────────
log()  { echo "[visual-verify] $*" >&2; }
die()  { log "ERROR: $*"; exit 1; }

# Emit a raw tmux pane capture (with ANSI escapes).
capture_ansi() {
  tmux capture-pane -t "$SESSION" -ep 2>/dev/null || true
}

# Emit a plain-text tmux pane capture.
capture_plain() {
  tmux capture-pane -t "$SESSION" -p 2>/dev/null || true
}

# Wait for a regex pattern to appear in the pane (polls every 1 s).
# Returns 0 on match, 1 on timeout.
wait_for_pattern() {
  local pattern="$1"
  local timeout="${2:-30}"
  local elapsed=0
  while (( elapsed < timeout )); do
    if capture_ansi | grep -qE "$pattern"; then
      return 0
    fi
    sleep 1
    (( elapsed++ )) || true
  done
  return 1
}

# ── Cleanup ──────────────────────────────────────────────────────────────────
cleanup() {
  log "Cleaning up tmux session '$SESSION'..."
  tmux kill-session -t "$SESSION" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Setup ─────────────────────────────────────────────────────────────────────
[[ -f "$CHECKPOINTS_FILE" ]] || die "Checkpoint definitions not found: $CHECKPOINTS_FILE"
command -v tmux &>/dev/null  || die "tmux is required but not installed"
command -v jq   &>/dev/null  || die "jq is required but not installed"

# Kill any leftover session from a previous run.
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Fresh output directory.
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

# ── Launch target process in a dedicated tmux session ────────────────────────
log "Creating tmux session '$SESSION' (120x40)"
tmux new-session -d -s "$SESSION" -x 120 -y 40

# Pass API key into the tmux environment if available.
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  tmux setenv -t "$SESSION" ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
fi

log "Starting target: $TARGET_CMD"
# Run from the repo root so relative paths (e.g. 'cd tui') resolve correctly.
tmux send-keys -t "$SESSION" "cd $REPO_ROOT && $TARGET_CMD" Enter

# ── Process checkpoints ────────────────────────────────────────────────────────
CHECKPOINT_COUNT=$(jq '.checkpoints | length' "$CHECKPOINTS_FILE")
log "Running $CHECKPOINT_COUNT checkpoints from $CHECKPOINTS_FILE"

# Build manifest incrementally.
MANIFEST_CHECKPOINTS="[]"

for (( i=0; i<CHECKPOINT_COUNT; i++ )); do
  CP_JSON=$(jq ".checkpoints[$i]" "$CHECKPOINTS_FILE")
  CP_NAME=$(echo "$CP_JSON" | jq -r '.name')
  WAIT_TYPE=$(echo "$CP_JSON" | jq -r '.wait.type')

  log "--- Checkpoint: $CP_NAME (wait type: $WAIT_TYPE) ---"

  CP_STATUS="ok"

  case "$WAIT_TYPE" in
    delay)
      SECS=$(echo "$CP_JSON" | jq -r '.wait.seconds // 3')
      log "  Waiting ${SECS}s..."
      sleep "$SECS"
      ;;

    grep)
      PATTERN=$(echo "$CP_JSON" | jq -r '.wait.pattern')
      TIMEOUT=$(echo "$CP_JSON" | jq -r '.wait.timeout // 30')
      log "  Polling for pattern /$PATTERN/ (timeout ${TIMEOUT}s)..."
      if ! wait_for_pattern "$PATTERN" "$TIMEOUT"; then
        log "  WARNING: pattern /$PATTERN/ not found within ${TIMEOUT}s — capturing anyway"
        CP_STATUS="timeout"
      fi
      ;;

    send-then-delay)
      KEYS=$(echo "$CP_JSON" | jq -r '.wait.keys')
      SECS=$(echo "$CP_JSON" | jq -r '.wait.seconds // 2')
      log "  Sending keys: '$KEYS', then waiting ${SECS}s..."
      tmux send-keys -t "$SESSION" "$KEYS" ""
      sleep "$SECS"
      ;;

    send-then-grep)
      KEYS=$(echo "$CP_JSON" | jq -r '.wait.keys')
      PATTERN=$(echo "$CP_JSON" | jq -r '.wait.pattern')
      TIMEOUT=$(echo "$CP_JSON" | jq -r '.wait.timeout // 30')
      log "  Sending keys: '$KEYS', then polling for /$PATTERN/ (timeout ${TIMEOUT}s)..."
      tmux send-keys -t "$SESSION" "$KEYS" ""
      if ! wait_for_pattern "$PATTERN" "$TIMEOUT"; then
        log "  WARNING: pattern /$PATTERN/ not found within ${TIMEOUT}s — capturing anyway"
        CP_STATUS="timeout"
      fi
      ;;

    *)
      log "  Unknown wait type '$WAIT_TYPE' — using 2s delay"
      sleep 2
      ;;
  esac

  # Capture the pane.
  ANSI_FILE="$OUT_DIR/${CP_NAME}.ansi"
  PNG_FILE="$OUT_DIR/${CP_NAME}.png"
  TXT_FILE="$OUT_DIR/${CP_NAME}.txt"

  log "  Capturing pane → $ANSI_FILE"
  capture_ansi > "$ANSI_FILE"
  capture_plain > "$TXT_FILE"

  # Convert to PNG.
  log "  Converting → $PNG_FILE"
  if "$SCRIPT_DIR/ansi-to-png.sh" "$ANSI_FILE" "$PNG_FILE" >/dev/null 2>&1; then
    log "  PNG created: $PNG_FILE"
  else
    CP_STATUS="error"
    log "  WARNING: PNG conversion failed for checkpoint '$CP_NAME'"
    PNG_FILE=""
  fi

  # Append to manifest.
  CP_ENTRY=$(jq -n \
    --arg name    "$CP_NAME" \
    --arg ansi    "$ANSI_FILE" \
    --arg txt     "$TXT_FILE" \
    --arg png     "${PNG_FILE:-}" \
    --arg status  "$CP_STATUS" \
    --argjson cp  "$CP_JSON" \
    '{
      name:     $name,
      ansi:     $ansi,
      txt:      $txt,
      png:      $png,
      status:   $status,
      expected: ($cp.expected // "")
    }')

  MANIFEST_CHECKPOINTS=$(echo "$MANIFEST_CHECKPOINTS" | jq ". + [$CP_ENTRY]")
done

# ── Emit JSON manifest ─────────────────────────────────────────────────────────
MANIFEST=$(jq -n \
  --arg session "$SESSION" \
  --arg out_dir "$OUT_DIR" \
  --argjson checkpoints "$MANIFEST_CHECKPOINTS" \
  '{
    session:     $session,
    out_dir:     $out_dir,
    checkpoints: $checkpoints
  }')

echo "$MANIFEST"

log "Done. Manifest written to stdout. PNGs in $OUT_DIR"
