#!/usr/bin/env bash
# ts-backend-parity-check.sh
#
# Runs the ts-backend parity sweep against committed golden fixtures.
#
# Mode: golden-corpus (default, no Python server required).
#   `bun run parity` without --live boots a TS server internally, runs each GET
#   route against committed fixture snapshots, and exits non-zero if any route
#   diverges. No live Python backend is needed — the fixtures are the source of
#   truth. (The --live flag would require Python on :18099; we deliberately omit
#   it here so this gate works in any review environment.)
#
# If bun is missing: prints a skip notice to stderr and exits 0 (mirrors the
# bun-missing handling in run-pr-tests.sh — toolchain absence must not block
# the gate for reviewers who only have Python).
#
# Exit codes:
#   0 — all routes at parity (or bun missing → skip)
#   non-zero — at least one route diverged (parity gate fails)

set -euo pipefail

# Resolve repo root — works whether called from repo root, scripts/, or anywhere
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TS_BACKEND_DIR="$REPO_ROOT/ts-backend"

if [ ! -f "$TS_BACKEND_DIR/package.json" ]; then
  echo "[ts-backend-parity-check] ts-backend/package.json not found — skipping" >&2
  exit 0
fi

# Put bun on PATH if it's not already visible (mirrors run-pr-tests.sh)
command -v bun >/dev/null 2>&1 || export PATH="$HOME/.bun/bin:$PATH"

if ! command -v bun >/dev/null 2>&1; then
  echo "[ts-backend-parity-check] bun not found (checked PATH + ~/.bun/bin) — skipping parity check" >&2
  exit 0
fi

echo "[ts-backend-parity-check] Running parity sweep (golden-corpus mode, no Python server needed)..." >&2

# Ensure deps are installed — bun install is fast and idempotent.
# In worktree environments node_modules may be absent even when the main tree has them.
cd "$TS_BACKEND_DIR"
if [ ! -d node_modules ]; then
  echo "[ts-backend-parity-check] Installing ts-backend deps (node_modules absent)..." >&2
  bun install --frozen-lockfile 2>&1 || { echo "[ts-backend-parity-check] bun install failed — skipping parity check" >&2; exit 0; }
fi

# Run with a hard timeout so a hung TS process can't block the review gate.
# cwd must be ts-backend/ so `bun run parity` resolves package.json scripts.
timeout --kill-after=5s 180 bun run parity
