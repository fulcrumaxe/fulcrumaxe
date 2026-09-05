#!/usr/bin/env bash
# tmux-sweep.sh — Layer B: real terminal TUI screenshot sweep.
#
# Starts a detached tmux session running 'python3 -m dashboard_tui' against
# read-only state, drives all 11 screens via 'tmux send-keys', captures
# per-screen .txt via 'tmux capture-pane -p'.  Optional .png via aha +
# wkhtmltoimage if both tools are present, else .txt only.
#
# Artifacts: STATE_DIR/tui-tester/<run-id>/<screen>.txt [and .png if tools available]
#
# Usage:
#   bash scripts/tui-tester/tmux-sweep.sh [--state-dir /path] [--update-baselines]
#
# Flags:
#   --state-dir DIR       Override AUTONOMOUS_TEAM_STATE_DIR
#   --update-baselines    Copy captures to tests/fixtures/tui-baseline/<screen>.txt
#   --timeout N           Max wall time in seconds (default: 30)
#   --session NAME        tmux session name (default: tui-sweep-$$)
#   --help                Print this message
#
# Exit codes:
#   0   — all 11 captures produced, total time < timeout
#   1   — one or more captures missing or timeout exceeded
#   2   — setup failure (tmux not found, state dir creation failed, etc.)
#
# Read-only guarantee: STATE_DIR is mounted by pointing AUTONOMOUS_TEAM_STATE_DIR
# at the existing path — no writes happen to it.  The run artifact dir is a NEW
# subdirectory STATE_DIR/tui-tester/<run-id>/ created at sweep time.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"
TIMEOUT=30
UPDATE_BASELINES=0
SESSION_NAME="tui-sweep-$$"
BASELINES_DIR="$REPO_ROOT/tests/fixtures/tui-baseline"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-dir)      STATE_DIR="$2"; shift 2 ;;
    --timeout)        TIMEOUT="$2"; shift 2 ;;
    --update-baselines) UPDATE_BASELINES=1; shift ;;
    --session)        SESSION_NAME="$2"; shift 2 ;;
    --help)
      grep '^#' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "[tmux-sweep] unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

if ! command -v tmux &>/dev/null; then
  echo "[tmux-sweep] ERROR: tmux not found — cannot run Layer B sweep" >&2
  exit 2
fi

HAS_AHA=0
HAS_WKHTML=0
if command -v aha &>/dev/null && command -v wkhtmltoimage &>/dev/null; then
  HAS_AHA=1
  HAS_WKHTML=1
fi

# ---------------------------------------------------------------------------
# Build artifact dir (new subdirectory — never mutates existing state)
# ---------------------------------------------------------------------------

RUN_ID="$(date +%Y%m%dT%H%M%S)-$$"
ARTIFACT_DIR="$STATE_DIR/tui-tester/$RUN_ID"
mkdir -p "$ARTIFACT_DIR"
chmod 700 "$ARTIFACT_DIR"
echo "[tmux-sweep] artifact dir: $ARTIFACT_DIR"

# ---------------------------------------------------------------------------
# 11 screens: (screen_name, key_to_press)
# Derived from DashboardTuiApp.BINDINGS in dashboard_tui/app.py
# ---------------------------------------------------------------------------

SCREENS=(
  "home:1"
  "prs:2"
  "discussions:3"
  "loop:4"
  "runs:5"
  "agent_feed:6"
  "stats:7"
  "pr_detail:8"
  "loop_controller:9"
  "ideas:0"
  "settings:s"
)

EXPECTED_COUNT="${#SCREENS[@]}"

# ---------------------------------------------------------------------------
# Start the TUI in a detached tmux session
# ---------------------------------------------------------------------------

SWEEP_START="$(date +%s)"

echo "[tmux-sweep] starting tmux session '$SESSION_NAME' …"
tmux new-session -d -s "$SESSION_NAME" -x 160 -y 50 \
  "AUTONOMOUS_TEAM_STATE_DIR=$STATE_DIR PYTHONPATH=$REPO_ROOT python3 -m dashboard_tui"

# Always tear down the session (and its dashboard_tui child) on ANY exit:
# success, timeout `exit 1`, `set -e` error, or SIGTERM/SIGINT. Idempotent.
trap 'tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true' EXIT INT TERM

# Give the app time to start and render the first screen
sleep 3

# ---------------------------------------------------------------------------
# Sweep each screen
# ---------------------------------------------------------------------------

CAPTURED=0
for entry in "${SCREENS[@]}"; do
  SCREEN_NAME="${entry%%:*}"
  SCREEN_KEY="${entry##*:}"

  # Navigate to the screen
  tmux send-keys -t "$SESSION_NAME" "$SCREEN_KEY" ""
  sleep 1

  # Capture the current pane content
  CAPTURE_FILE="$ARTIFACT_DIR/${SCREEN_NAME}.txt"
  tmux capture-pane -t "$SESSION_NAME" -p > "$CAPTURE_FILE"

  # Strip trailing blank lines for cleaner diffs
  if [[ -s "$CAPTURE_FILE" ]]; then
    CAPTURED=$((CAPTURED + 1))
    echo "[tmux-sweep] captured: $SCREEN_NAME (${CAPTURE_FILE})"
  else
    echo "[tmux-sweep] WARNING: empty capture for screen '$SCREEN_NAME'" >&2
  fi

  # Optional PNG: pipe capture through aha -> wkhtmltoimage
  if [[ $HAS_AHA -eq 1 ]] && [[ $HAS_WKHTML -eq 1 ]]; then
    PNG_FILE="$ARTIFACT_DIR/${SCREEN_NAME}.png"
    tmux capture-pane -t "$SESSION_NAME" -p \
      | aha --no-header \
      | wkhtmltoimage --quiet - "$PNG_FILE" 2>/dev/null || true
    if [[ -s "$PNG_FILE" ]]; then
      echo "[tmux-sweep] png: $SCREEN_NAME (${PNG_FILE})"
    fi
  fi

  # Check wall time
  NOW="$(date +%s)"
  ELAPSED=$((NOW - SWEEP_START))
  if [[ $ELAPSED -ge $TIMEOUT ]]; then
    echo "[tmux-sweep] WARNING: timeout after ${ELAPSED}s (limit=${TIMEOUT}s)" >&2
    break
  fi
done

# ---------------------------------------------------------------------------
# Quit the TUI
# ---------------------------------------------------------------------------

tmux send-keys -t "$SESSION_NAME" "q" "" 2>/dev/null || true
sleep 1
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Write manifest
# ---------------------------------------------------------------------------

ELAPSED=$(( $(date +%s) - SWEEP_START ))
MANIFEST="$ARTIFACT_DIR/manifest.json"
python3 - <<MANIFEST_PY
import json, os, pathlib

artifact_dir = "$ARTIFACT_DIR"
screens = [entry.split(':')[0] for entry in """${SCREENS[*]}""".split()]
files = sorted(os.listdir(artifact_dir))
txt_files = [f for f in files if f.endswith('.txt') and f != 'manifest.json']

manifest = {
    "run_id": "$RUN_ID",
    "elapsed_s": $ELAPSED,
    "screens_expected": $EXPECTED_COUNT,
    "screens_captured": $CAPTURED,
    "txt_files": txt_files,
    "artifact_dir": artifact_dir,
}
pathlib.Path("$MANIFEST").write_text(json.dumps(manifest, indent=2))
print(json.dumps(manifest))
MANIFEST_PY

# ---------------------------------------------------------------------------
# --update-baselines: copy to tests/fixtures/tui-baseline/
# ---------------------------------------------------------------------------

if [[ $UPDATE_BASELINES -eq 1 ]]; then
  mkdir -p "$BASELINES_DIR"
  for txt in "$ARTIFACT_DIR"/*.txt; do
    [[ -e "$txt" ]] || continue
    BASE="$(basename "$txt")"
    cp "$txt" "$BASELINES_DIR/$BASE"
    echo "[tmux-sweep] baseline updated: tests/fixtures/tui-baseline/$BASE"
  done
fi

# ---------------------------------------------------------------------------
# Exit status
# ---------------------------------------------------------------------------

ELAPSED_FINAL=$(( $(date +%s) - SWEEP_START ))
echo "[tmux-sweep] done: captured=$CAPTURED/${EXPECTED_COUNT} elapsed=${ELAPSED_FINAL}s"

if [[ $CAPTURED -lt $EXPECTED_COUNT ]]; then
  echo "[tmux-sweep] FAIL: expected $EXPECTED_COUNT captures, got $CAPTURED" >&2
  exit 1
fi

if [[ $ELAPSED_FINAL -ge $TIMEOUT ]]; then
  echo "[tmux-sweep] FAIL: exceeded timeout=${TIMEOUT}s" >&2
  exit 1
fi

exit 0
