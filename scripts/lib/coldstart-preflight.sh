#!/usr/bin/env bash
# scripts/lib/coldstart-preflight.sh — prerequisite checks for scripts/coldstart.sh
#
# Usage:
#   Sourced:  source scripts/lib/coldstart-preflight.sh; coldstart_preflight
#   Direct:   bash scripts/lib/coldstart-preflight.sh
#
# Checks gh (present + authenticated), node, python3. Prints one friendly
# "missing prerequisite: <name>" line per gap and returns/exits non-zero.
# No Python traceback, no bash stack trace — plain, human-readable output
# only, since a first-time operator is the audience (D#1526 AC#5).
#
# Runs before any mutation in the coldstart pipeline — this module is
# read-only, it never writes a file or calls a GitHub API mutation.

coldstart_preflight() {
  local missing=0

  if ! command -v gh >/dev/null 2>&1; then
    echo "[preflight] missing prerequisite: gh (GitHub CLI) — install from https://cli.github.com/"
    missing=1
  else
    if ! gh auth status >/dev/null 2>&1; then
      echo "[preflight] missing prerequisite: gh auth — run 'gh auth login' first"
      missing=1
    fi
  fi

  if ! command -v node >/dev/null 2>&1; then
    echo "[preflight] missing prerequisite: node — install from https://nodejs.org/"
    missing=1
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "[preflight] missing prerequisite: python3 — install Python 3 (https://www.python.org/)"
    missing=1
  fi

  if [[ "$missing" -eq 0 ]]; then
    echo "[preflight] all prerequisites present (gh, node, python3)"
    return 0
  fi

  echo "[preflight] one or more prerequisites are missing — install them and re-run." >&2
  return 1
}

# Allow direct execution for smoke-testing this module in isolation:
#   bash scripts/lib/coldstart-preflight.sh
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set +e
  coldstart_preflight
  exit $?
fi
