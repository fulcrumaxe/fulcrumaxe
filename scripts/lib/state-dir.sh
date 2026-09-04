#!/usr/bin/env bash
# scripts/lib/state-dir.sh — export AUTONOMOUS_TEAM_STATE_DIR from project.json.
#
# Source this file early in any hook or spawn script that invokes Python code
# that depends on AUTONOMOUS_TEAM_STATE_DIR (e.g. agent_run_tracker.py,
# stats_writer.py).  Without this export, those modules fall through to the
# hardcoded ~/.fulcrumaxe-state/ fallback, which causes all telemetry
# from forked projects to land in the wrong database.
#
# Safe to re-source: the if-guard is a no-op when the var is already exported.
#
# Usage:
#   source scripts/lib/state-dir.sh
#
# Behaviour:
#   1. If AUTONOMOUS_TEAM_STATE_DIR is already set — do nothing.
#   2. Read state_dir from <repo-root>/.autonomous-team/project.json.
#   3. If found and non-empty, export AUTONOMOUS_TEAM_STATE_DIR.
#   4. If not found (no project.json, or key absent), leave var unset —
#      the Python fallback chain still works for fulcrumaxe itself.
#
# No external dependencies beyond bash 4+ and python3 (both always available).

if [[ -z "${AUTONOMOUS_TEAM_STATE_DIR:-}" ]]; then
  _STATE_DIR_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  _STATE_DIR_REPO_ROOT="$(cd "$_STATE_DIR_SCRIPT_DIR/../.." && pwd)"
  _STATE_DIR_PROJECT_JSON="$_STATE_DIR_REPO_ROOT/.autonomous-team/project.json"

  if [[ -f "$_STATE_DIR_PROJECT_JSON" ]]; then
    _STATE_DIR_VAL=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    v = d.get('state_dir', '')
    print(v if v else '')
except Exception:
    print('')
" "$_STATE_DIR_PROJECT_JSON" 2>/dev/null || true)

    if [[ -n "$_STATE_DIR_VAL" ]]; then
      # Must be absolute. A relative value resolves against whatever cwd the
      # caller happens to have, which is how state.db / stats.duckdb /
      # audit.jsonl / blackboard/ ended up written into the repo root (D#1967).
      # Refuse here, naming project.json, so the failure points at where the
      # bad value actually came from instead of surfacing three layers later.
      case "$_STATE_DIR_VAL" in
        "~"|"~/"*) _STATE_DIR_VAL="${HOME}${_STATE_DIR_VAL#\~}" ;;
      esac
      if [[ "$_STATE_DIR_VAL" != /* ]]; then
        echo "state-dir.sh: state_dir in $_STATE_DIR_PROJECT_JSON is not an absolute path: '$_STATE_DIR_VAL'" >&2
        echo "state-dir.sh: refusing to export AUTONOMOUS_TEAM_STATE_DIR — fix state_dir in that file." >&2
        unset _STATE_DIR_VAL
        return 1 2>/dev/null || exit 1
      fi
      export AUTONOMOUS_TEAM_STATE_DIR="$_STATE_DIR_VAL"
    fi

    unset _STATE_DIR_VAL
  fi

  unset _STATE_DIR_SCRIPT_DIR _STATE_DIR_REPO_ROOT _STATE_DIR_PROJECT_JSON
fi
