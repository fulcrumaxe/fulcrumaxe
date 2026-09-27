#!/usr/bin/env bash
# scripts/lib/run-analyst-sweep.sh — run the run-analyst morning sweep and
# make a crashed analyzer loud instead of silent (D#1753).
#
# The old one-liner in start-the-day.sh was:
#   python3 backend/run_analyst.py --since=12h 2>&1 | tail -3 | sed 's/^/    /' \
#     || echo "    (run-analyst failed)"
#
# Two problems with it beyond volume: `2>&1 | tail -3` means a crash prints
# the last three lines of a traceback, indented to look exactly like
# findings, and the failure text was one grey line in a wall of twenty
# sweeps. An empty findings section and a crashed analyzer read identically
# in the plan output.
#
# This captures output and exit code into variables first, then branches --
# a non-zero exit gets a loud `RUN-ANALYST FAILED` block naming the exit
# code, so a crash can be told apart from a clean run at a glance.
#
# The sweep itself stays non-fatal: run_analyst_sweep always returns 0 so
# the rest of the morning ritual keeps running even when the analyzer died.
#
# Usage:
#   source scripts/lib/run-analyst-sweep.sh
#   run_analyst_sweep
#
# Test seam: set RUN_ANALYST_CMD to override the real invocation, e.g.
#   RUN_ANALYST_CMD="./stub.sh" run_analyst_sweep
# Defaults to the real analyzer call.

run_analyst_sweep() {
  local cmd="${RUN_ANALYST_CMD:-python3 backend/run_analyst.py --since=12h}"
  local output exit_code

  output=$(eval "$cmd" 2>&1)
  exit_code=$?

  if [[ "$exit_code" -eq 0 ]]; then
    echo "$output" | tail -3 | sed 's/^/    /'
  else
    echo "    ============================================================"
    echo "    RUN-ANALYST FAILED (exit $exit_code)"
    echo "    The analyzer crashed. No findings above is not an all-clear --"
    echo "    it means the sweep did not run. Investigate before trusting it."
    echo "    ============================================================"
    echo "$output" | tail -5 | sed 's/^/    /'
  fi

  return 0
}
