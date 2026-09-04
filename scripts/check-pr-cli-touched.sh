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
_REPO="$(_resolve_repo)"

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
