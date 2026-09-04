#!/usr/bin/env bash
# Kill chrome processes started by puppeteer (gitignored profile dirs) that
# aren't part of today's MCP supervisor tree.
#
# Puppeteer creates a temp profile dir matching 'puppeteer_dev_chrome_profile'.
# The chrome-devtools-mcp supervisor does NOT use this profile path, so this
# pattern is safe to target exclusively.
#
# Usage:
#   bash scripts/reap-zombie-chromes.sh
# Exit: always 0 (non-fatal — zombie reaping is best-effort).

pkill -9 -f 'puppeteer_dev_chrome_profile' 2>/dev/null || true
echo "reaped puppeteer chromes"
