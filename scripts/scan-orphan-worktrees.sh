#!/usr/bin/env bash
# scan-orphan-worktrees.sh — DEPRECATED: back-compat shim for reap-worktrees.sh
#
# This script has been superseded by scripts/reap-worktrees.sh which uses the
# worktree lifecycle registry for deterministic cleanup. This shim delegates
# all work to reap-worktrees.sh and will be archived after one stable week.
#
# Original path: scripts/scan-orphan-worktrees.sh
# Replacement:   scripts/reap-worktrees.sh

echo "[scan-orphan-worktrees] DEPRECATED — use scripts/reap-worktrees.sh instead" >&2

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$SCRIPT_DIR/reap-worktrees.sh" "$@"
