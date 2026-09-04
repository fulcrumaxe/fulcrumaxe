#!/usr/bin/env bash
# Start the TUI with GH_CONFIG_DIR/GH_REPO/PATH set via env-bootstrap.sh
# Usage: bash scripts/start-tui.sh

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Source gh/env setup
source "$REPO_DIR/scripts/env-bootstrap.sh" || exit 1

# TUI-specific: no request timeout (interactive mode)
export AF_REQUEST_TIMEOUT=0
export AF_MODEL="${AF_MODEL:-kimi-k2.5}"

cd "$REPO_DIR/tui"
exec node dist/index.js
