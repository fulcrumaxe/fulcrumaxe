#!/usr/bin/env bash
# scripts/bootstrap-deps.sh — install Node + Python runtime dependencies for
# a freshly coldstarted project.
#
# Usage:
#   Sourced:  source scripts/bootstrap-deps.sh; bootstrap_deps_main
#   Direct:   bash scripts/bootstrap-deps.sh [--dry-run]
#
# Neither scripts/coldstart.sh nor loop-bootstrap/bootstrap.sh ever installs
# actual dependencies — a fresh checkout ends up with none of the runtime
# deps present (D#1637). This module is the single place that does the
# installing; coldstart.sh only sources it and calls it (module-per-feature).
#
# What it does, per component:
#   - dashboard/, ts-backend/, tui/ (presence-based — skip any dir that
#     doesn't exist or has no package.json): `bun install` for ts-backend/
#     when bun.lock is present, else `npm ci` when package-lock.json is
#     present, else `npm install`.
#   - Python backend: create (or reuse) .venv/ at the repo root and
#     `pip install -r requirements.txt`, using the root requirements.txt
#     and falling back to backend/requirements.txt if the root file is
#     absent.
#
# Idempotent: a component whose node_modules/.bin already has real binaries
# in it, or a repo that already has .venv/, is reported as a fast skip
# rather than reinstalled from scratch.
#
# --dry-run prints the same per-component plan and exits 0 WITHOUT running
# any install command or creating a venv.
#
# Plain-English `[bootstrap-deps] ...` messages only — no Python traceback,
# no bash stack trace, matching scripts/lib/coldstart-preflight.sh convention.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE_COMPONENTS=(dashboard ts-backend tui)

# ---------------------------------------------------------------------------
# Node components
# ---------------------------------------------------------------------------

_bootstrap_deps_node_cmd() {
  local dir="$1"
  if [[ "$dir" == "ts-backend" && -f "$REPO_ROOT/$dir/bun.lock" ]]; then
    echo "bun install"
  elif [[ -f "$REPO_ROOT/$dir/package-lock.json" ]]; then
    echo "npm ci"
  else
    echo "npm install"
  fi
}

_bootstrap_deps_node_populated() {
  local bindir="$REPO_ROOT/$1/node_modules/.bin"
  [[ -d "$bindir" ]] && [[ -n "$(ls -A "$bindir" 2>/dev/null)" ]]
}

bootstrap_deps_node() {
  local dir="$1"
  local dry_run="$2"
  local path="$REPO_ROOT/$dir"

  if [[ ! -f "$path/package.json" ]]; then
    return 0
  fi

  local cmd
  cmd="$(_bootstrap_deps_node_cmd "$dir")"

  if _bootstrap_deps_node_populated "$dir"; then
    echo "[bootstrap-deps] $dir/: node_modules/.bin already populated — skip"
    return 0
  fi

  if [[ "$dry_run" -eq 1 ]]; then
    echo "[bootstrap-deps] $dir/: would run '$cmd' (dry-run, no mutation)"
    return 0
  fi

  echo "[bootstrap-deps] $dir/: running '$cmd'..."
  if ! (cd "$path" && $cmd); then
    echo "[bootstrap-deps] ERROR: '$cmd' failed in $dir/ — fix the error above and re-run." >&2
    return 1
  fi
  echo "[bootstrap-deps] $dir/: done"
}

# ---------------------------------------------------------------------------
# Python backend
# ---------------------------------------------------------------------------

bootstrap_deps_python() {
  local dry_run="$1"
  local venv="$REPO_ROOT/.venv"
  local req="$REPO_ROOT/requirements.txt"

  if [[ ! -f "$req" ]]; then
    req="$REPO_ROOT/backend/requirements.txt"
  fi

  if [[ ! -f "$req" ]]; then
    echo "[bootstrap-deps] python: no requirements.txt found at repo root or backend/ — skipping"
    return 0
  fi

  if [[ -x "$venv/bin/python3" ]]; then
    echo "[bootstrap-deps] python: .venv/ already present — skip (remove .venv/ first to force a reinstall)"
    return 0
  fi

  if [[ "$dry_run" -eq 1 ]]; then
    echo "[bootstrap-deps] python: would create $venv and run 'pip install -r $req' (dry-run, no mutation)"
    return 0
  fi

  echo "[bootstrap-deps] python: creating venv at $venv..."
  if ! python3 -m venv "$venv"; then
    echo "[bootstrap-deps] ERROR: failed to create venv at $venv — is the python3 venv module installed?" >&2
    return 1
  fi

  echo "[bootstrap-deps] python: installing from $req..."
  if ! "$venv/bin/pip" install -r "$req"; then
    echo "[bootstrap-deps] ERROR: 'pip install -r $req' failed — see output above." >&2
    return 1
  fi
  echo "[bootstrap-deps] python: done"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

bootstrap_deps_main() {
  local dry_run=0
  for arg in "$@"; do
    case "$arg" in
      --dry-run) dry_run=1 ;;
      *)
        echo "[bootstrap-deps] unknown flag: $arg" >&2
        return 1
        ;;
    esac
  done

  local status=0
  local c
  for c in "${NODE_COMPONENTS[@]}"; do
    bootstrap_deps_node "$c" "$dry_run" || status=1
  done
  bootstrap_deps_python "$dry_run" || status=1

  if [[ "$status" -eq 0 ]]; then
    if [[ "$dry_run" -eq 1 ]]; then
      echo "[bootstrap-deps] dry-run complete — nothing was installed."
    else
      echo "[bootstrap-deps] all dependencies present."
    fi
  fi
  return "$status"
}

# Allow direct execution for smoke-testing this module in isolation:
#   bash scripts/bootstrap-deps.sh [--dry-run]
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set +e
  bootstrap_deps_main "$@"
  exit $?
fi
