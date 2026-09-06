#!/usr/bin/env bash
# scripts/lib/coldstart-preflight.sh — prerequisite checks for scripts/coldstart.sh
#
# Usage:
#   Sourced:  source scripts/lib/coldstart-preflight.sh; coldstart_preflight
#   Direct:   bash scripts/lib/coldstart-preflight.sh
#
# Checks gh (present + authenticated), node, python3, GNU coreutils. Prints
# one friendly "missing prerequisite: <name>" line per gap and returns/exits
# non-zero — every check feeds the same `missing` counter, so a run reports
# all of its gaps at once instead of stopping at the first.
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

  # GNU coreutils. The supported environment is Linux with GNU coreutils
  # (README, "Prerequisites"); this is where that contract stops being a
  # sentence in a file and starts being a check.
  #
  # scripts/lib/platform-compat.sh already gives GNU/BSD fallbacks for the
  # mtime, size and date-offset call sites, so this is not asserting that
  # nothing works elsewhere. It probes the three constructs still used in
  # shipped scripts with no fallback at the call site:
  #
  #   realpath -m  — scripts/lib/hook-event.sh's containment gate. Traced on
  #                  a stubbed non-GNU realpath: both sides of the comparison
  #                  collapse to the empty string, the `"$_hed_real"/*` glob
  #                  does not match it, and hook_event_init rejects a benign
  #                  event id and exits 1. Fail-closed, so every hook that
  #                  uses it simply stops running.
  #   date -d      — relative date arithmetic (scripts/sweep-stale-state-dirs.sh)
  #   readlink -f  — path canonicalisation (scripts/lib/verify-tree.sh,
  #                  scripts/lib/pr-tree.sh), which degrade to an unresolved
  #                  path rather than saying anything
  #
  # Probed by running each one for real, never by reading `uname` — a macOS
  # host with GNU coreutils first on PATH passes, and a Linux host with them
  # stripped out fails. Same rule platform-compat.sh follows.
  local gnu_gaps=""
  date -d '1970-01-02 -1 day' '+%Y-%m-%d' >/dev/null 2>&1 || gnu_gaps="${gnu_gaps}date -d, "
  realpath -m . >/dev/null 2>&1 || gnu_gaps="${gnu_gaps}realpath -m, "
  readlink -f . >/dev/null 2>&1 || gnu_gaps="${gnu_gaps}readlink -f, "
  if [[ -n "$gnu_gaps" ]]; then
    echo "[preflight] missing prerequisite: GNU coreutils — this host rejected ${gnu_gaps%, }. Linux with GNU coreutils is the supported environment; macOS/BSD is not, and parts of the team degrade or refuse there rather than failing at install time. On macOS: 'brew install coreutils gnu-sed' and put the gnubin directories first on PATH."
    missing=1
  fi

  if [[ "$missing" -eq 0 ]]; then
    echo "[preflight] all prerequisites present (gh, node, python3, GNU coreutils)"
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
