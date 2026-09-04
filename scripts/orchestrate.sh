#!/usr/bin/env bash
# Launch a 3-pane tmux session for the autonomous team.
#
# Layout:
#   ┌────────────────────────┬──────────────────┐
#   │ Main: TUI (70%)        │ Debug: stderr    │
#   │ (start-tui.sh)         │ (tail -f logfile)│
#   ├────────────────────────┴──────────────────┤
#   │ Task Queue: watch now.md (20% height)     │
#   └───────────────────────────────────────────┘
#
# Usage: bash scripts/orchestrate.sh
# Idempotent: attaches if session "af" already exists.

set -euo pipefail

SESSION="af"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="/tmp/af-stderr.log"

# Check tmux is available
if ! command -v tmux &>/dev/null; then
  echo "ERROR: tmux is not installed. Install it and retry." >&2
  exit 1
fi

# If session already exists, just attach and exit
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already exists — attaching."
  exec tmux attach-session -t "$SESSION"
fi

# Ensure the log file exists so tail -f doesn't fail on startup
touch "$LOG_FILE"

# Source env so the pane commands can inherit the environment if needed
# (each pane re-sources env-bootstrap.sh via start-tui.sh, but this sets
#  REPO_DIR for the watch pane's relative path)
export REPO_DIR

# Create session detached, starting in REPO_DIR
tmux new-session -d -s "$SESSION" -c "$REPO_DIR"

# --- Main pane (pane 0): will be the left/top section ---
# Split bottom strip (20% of total height) for task queue pane
tmux split-window -v -p 20 -t "$SESSION:0.0" -c "$REPO_DIR"
# Pane 0 = top, Pane 1 = bottom strip

# Split the top pane vertically: left 70%, right 30% for debug
tmux split-window -h -p 30 -t "$SESSION:0.0" -c "$REPO_DIR"
# Pane 0 = top-left (main TUI), Pane 1 = top-right (debug), Pane 2 = bottom (task)

# --- Send commands to each pane ---

# Debug pane (top-right, pane 1): tail the stderr log
tmux send-keys -t "$SESSION:0.1" "tail -f $LOG_FILE" Enter

# Task pane (bottom, pane 2): watch now.md every 5 seconds
tmux send-keys -t "$SESSION:0.2" \
  "watch -n 5 'cat $REPO_DIR/.autonomous-team/now.md 2>/dev/null || echo \"(no now.md yet)\"'" Enter

# Main pane (top-left, pane 0): run TUI, redirect stderr to log file
tmux send-keys -t "$SESSION:0.0" \
  "bash $REPO_DIR/scripts/start-tui.sh 2>$LOG_FILE" Enter

# Focus the main (TUI) pane
tmux select-pane -t "$SESSION:0.0"

# Attach to the session
exec tmux attach-session -t "$SESSION"
