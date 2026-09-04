#!/usr/bin/env bash
# run-scenarios.sh — Fan out MCP-driven Chrome scenario files to browser-tester agents.
#
# Usage:
#   scripts/run-scenarios.sh <area> [--scenario <name>] [--dry-run]
#
# Arguments:
#   <area>             Sub-directory under dashboard/scenarios/ (e.g. loop-controller)
#   --scenario <name>  Run one scenario only (match on .name field in JSON)
#   --dry-run          Validate scenario files and print spawn plan; DO NOT spawn agents,
#                      DO NOT call claude / claude -p / _start_loop_run / write to FIFO.
#
# Outputs:
#   .autonomous-team/browser-tours/run-<UTC-ISO>.json — aggregated run report (live mode only)
#
# HARD RULE (Discussion #439):
#   This script MUST NOT invoke `claude`, `claude -p`, `_start_loop_run`, or write to
#   any loop trigger FIFO during --dry-run. Those restrictions also apply to any subprocess
#   spawned from this script.
#
# Environment variables:
#   AF_MCP_TEST_ORIGIN=1   Set on the backend process to bypass _reject_test_origin_spawn
#                          for MCP-driven Chrome (HeadlessChrome UA + localhost origin).
#   AF_ALLOW_TEST_ORIGIN_SPAWNS=1  Legacy bypass (local human-driven dev).
#
# See: dashboard/scenarios/README.md

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
AREA=""
FILTER_NAME=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --scenario)
      shift
      FILTER_NAME="${1:-}"
      shift
      ;;
    -*)
      echo "Unknown flag: $1" >&2
      echo "Usage: $0 <area> [--scenario <name>] [--dry-run]" >&2
      exit 1
      ;;
    *)
      if [[ -z "$AREA" ]]; then
        AREA="$1"
      else
        echo "Unexpected positional argument: $1" >&2
        exit 1
      fi
      shift
      ;;
  esac
done

if [[ -z "$AREA" ]]; then
  echo "Error: <area> argument is required." >&2
  echo "Usage: $0 <area> [--scenario <name>] [--dry-run]" >&2
  exit 1
fi

SCENARIO_DIR="$REPO_ROOT/dashboard/scenarios/$AREA"

if [[ ! -d "$SCENARIO_DIR" ]]; then
  echo "Error: scenario directory not found: $SCENARIO_DIR" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Discover scenario files
# ---------------------------------------------------------------------------
mapfile -t ALL_FILES < <(find "$SCENARIO_DIR" -maxdepth 1 -name '*.scenario.json' | sort)

if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "Error: no *.scenario.json files found in $SCENARIO_DIR" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Validate all scenario JSON files (always — even in live mode)
# ---------------------------------------------------------------------------
VALIDATION_ERRORS=0
VALID_FILES=()

for f in "${ALL_FILES[@]}"; do
  # Parse with python3 to validate JSON syntax
  if ! python3 -c "import json, sys; d=json.load(open('$f')); \
    missing=[k for k in ['name','goal','url','steps','success_criteria'] if k not in d]; \
    sys.exit(1) if missing else None" 2>/dev/null; then
    echo "INVALID: $f (missing required keys or JSON parse error)" >&2
    VALIDATION_ERRORS=$((VALIDATION_ERRORS + 1))
  else
    # Extract name for filtering
    SCENARIO_NAME=$(python3 -c "import json; print(json.load(open('$f'))['name'])")
    if [[ -n "$FILTER_NAME" && "$SCENARIO_NAME" != "$FILTER_NAME" ]]; then
      continue
    fi
    VALID_FILES+=("$f")
  fi
done

if [[ $VALIDATION_ERRORS -gt 0 ]]; then
  echo "Aborting: $VALIDATION_ERRORS scenario file(s) failed validation." >&2
  exit 1
fi

if [[ ${#VALID_FILES[@]} -eq 0 ]]; then
  echo "Error: no scenarios matched filter '${FILTER_NAME}'." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Dry-run: print spawn plan and exit
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == "true" ]]; then
  echo "=== Scenario runner dry-run for area: $AREA ==="
  echo ""
  echo "Scenario directory: $SCENARIO_DIR"
  echo "Scenarios to run (${#VALID_FILES[@]}):"
  echo ""
  for f in "${VALID_FILES[@]}"; do
    SNAME=$(python3 -c "import json; d=json.load(open('$f')); print(d['name'])")
    SGOAL=$(python3 -c "import json; d=json.load(open('$f')); print(d['goal'])")
    NSTEPS=$(python3 -c "import json; d=json.load(open('$f')); print(len(d['steps']))")
    NCRITERIA=$(python3 -c "import json; d=json.load(open('$f')); print(len(d['success_criteria']))")
    echo "  [scenario] $SNAME"
    echo "    goal:     $SGOAL"
    echo "    steps:    $NSTEPS"
    echo "    criteria: $NCRITERIA"
    echo "    file:     $f"
    echo ""
  done
  echo "Spawn plan:"
  for f in "${VALID_FILES[@]}"; do
    SNAME=$(python3 -c "import json; d=json.load(open('$f')); print(d['name'])")
    echo "  Would spawn: browser-tester agent with scenario '$SNAME'"
    echo "    Input: $f"
    echo "    Output: .autonomous-team/browser-tours/<run-id>-$SNAME.json"
    echo ""
  done
  echo "NOTE: --dry-run mode — no agents spawned, no claude/claude -p/_start_loop_run called."
  echo "PRESUM: pass"
  exit 0
fi

# ---------------------------------------------------------------------------
# Live mode: spawn browser-tester agents (one per scenario)
# ---------------------------------------------------------------------------
# NOTE: Live spawning requires the MCP browser-tester contract from Discussion #467
# to be finalized. Until then, live mode exits with an informative message.
# Wiring real spawning is a follow-up after #467 lands.
echo "Live scenario execution is not yet wired (pending Discussion #467 MCP browser-tester contract)."
echo "Use --dry-run to validate scenario files and print the spawn plan."
echo ""
echo "To run scenarios manually once #467 is finalized:"
echo "  bash scripts/run-scenarios.sh $AREA --dry-run   # validate first"
echo "  bash scripts/run-scenarios.sh $AREA             # then run live"
exit 1
