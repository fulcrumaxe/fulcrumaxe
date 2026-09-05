#!/usr/bin/env bash
# check-pr-cli-touched.sh <PR_NUMBER>
#
# Exit 0 if the PR touches any file that triggers the backend real-world
# verification gate; exit 1 otherwise.
#
# Triggers (any one match → exit 0):
#   - backend/api.py
#   - backend/server.py
#   - backend/rpc/**
#   - backend/*.py  (any Python file directly under backend/)
#   - scripts/*.sh  (any shell script directly under scripts/)
#   - .autonomous-team/schemas/**
#
# Used by code-reviewer to decide whether to classify the Spec's verification
# substance (backend/spec_verification_substance.py) and emit
# `verification_substance` in AGENT_OUTPUT — classification only as of
# D#2008's sixth review round; nothing here triggers command execution
# (see archive/run-backend-verification-2026-08-20/README.md).
#
# Usage:
#   bash scripts/check-pr-cli-touched.sh 123 && echo "backend touched"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# The code plane — this reads a PR diff, which lives with the code.
#
# An unresolved plane must stop before `gh` runs: `gh pr diff --repo ""` exits 0
# against whatever the checkout's origin remote points at, so it would answer
# this predicate from the wrong repo instead of failing. It exits 0 ("assume
# backend touched") rather than 1, because every caller collapses non-zero to "no" and
# would otherwise silently skip the backend verification gate.
_REPO="$(_resolve_code_repo 2>/dev/null || true)"
if [ -z "${_REPO}" ]; then
  echo "[check-pr-cli-touched] ERROR: could not resolve the code repo — reporting \"backend touched\" so the backend verification gate is not silently skipped. Add a \"code_repo\" (or \"repo\") field to .autonomous-team/config.json, or set AUTONOMOUS_TEAM_REPO." >&2
  exit 0
fi

PR="${1:?Usage: check-pr-cli-touched.sh <PR_NUMBER>}"

CHANGED=$(gh pr diff --name-only "$PR" --repo "$_REPO")

# Direct backend entry points
if echo "$CHANGED" | grep -qE '^backend/(api|server)\.py$'; then
  exit 0
fi

# All Python files directly under backend/ (CLI entry points, argparse, click)
if echo "$CHANGED" | grep -qE '^backend/[^/]+\.py$'; then
  exit 0
fi

# RPC handlers
if echo "$CHANGED" | grep -qE '^backend/rpc/'; then
  exit 0
fi

# Shell scripts with CLI args directly under scripts/
if echo "$CHANGED" | grep -qE '^scripts/[^/]+\.sh$'; then
  exit 0
fi

# Schema files
if echo "$CHANGED" | grep -qE '^\.autonomous-team/schemas/'; then
  exit 0
fi

exit 1
