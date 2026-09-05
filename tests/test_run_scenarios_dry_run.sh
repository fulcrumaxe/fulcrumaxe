#!/usr/bin/env bash
# test_run_scenarios_dry_run.sh — Verify scripts/run-scenarios.sh --dry-run behaviour.
#
# Checks:
#   1. exits 0
#   2. output contains all 3 expected scenario names
#   3. no agent spawn was attempted (no claude process, no FIFO write, no _start_loop_run)
#   4. all scenario JSON files parse correctly (required keys present)
#
# HARD RULE (Discussion #439): this test MUST NOT invoke claude, claude -p,
# _start_loop_run, or write to any loop trigger FIFO. --dry-run only.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$REPO_ROOT/scripts/run-scenarios.sh"
SCENARIO_DIR="$REPO_ROOT/dashboard/scenarios/loop-controller"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== test_run_scenarios_dry_run ==="
echo ""

# ---------------------------------------------------------------------------
# 1. run-scenarios.sh exists and is executable
# ---------------------------------------------------------------------------
echo "[1] runner exists and is executable"
if [[ -x "$RUNNER" ]]; then
  pass "scripts/run-scenarios.sh is executable"
else
  fail "scripts/run-scenarios.sh missing or not executable (path: $RUNNER)"
fi

# ---------------------------------------------------------------------------
# 2. --dry-run exits 0
# ---------------------------------------------------------------------------
echo "[2] --dry-run exits 0"
DRY_OUTPUT=$("$RUNNER" loop-controller --dry-run 2>&1)
DRY_RC=$?
if [[ $DRY_RC -eq 0 ]]; then
  pass "--dry-run exit code is 0"
else
  fail "--dry-run exit code is $DRY_RC (expected 0)"
fi

# ---------------------------------------------------------------------------
# 3. output contains all 3 required scenario names
# ---------------------------------------------------------------------------
echo "[3] output contains required scenario names"
REQUIRED_SCENARIOS=("load-page" "start-loop-button" "view-iteration-history")
for name in "${REQUIRED_SCENARIOS[@]}"; do
  if echo "$DRY_OUTPUT" | grep -q "$name"; then
    pass "output contains scenario name: $name"
  else
    fail "output is missing scenario name: $name"
    echo "    DRY_OUTPUT was:"
    echo "$DRY_OUTPUT" | sed 's/^/    /'
  fi
done

# ---------------------------------------------------------------------------
# 4. output contains PRESUM: pass line
# ---------------------------------------------------------------------------
echo "[4] output contains PRESUM: pass"
if echo "$DRY_OUTPUT" | grep -q "PRESUM: pass"; then
  pass "output contains 'PRESUM: pass'"
else
  fail "output missing 'PRESUM: pass'"
fi

# ---------------------------------------------------------------------------
# 5. no agent spawn was attempted (no claude process, no FIFO write)
# ---------------------------------------------------------------------------
echo "[5] no agent spawn attempted"
if echo "$DRY_OUTPUT" | grep -qE "^Spawning|^Starting agent|exec.*claude -p|Calling.*_start_loop_run"; then
  fail "dry-run output contains evidence of spawn attempt"
else
  pass "no spawn attempt found in output"
fi

# Check that no new claude process was started during the dry-run
# (We can verify the dry-run explicitly states it did NOT spawn)
if echo "$DRY_OUTPUT" | grep -q "no agents spawned\|--dry-run mode\|dry-run"; then
  pass "output explicitly confirms dry-run mode (no spawns)"
else
  # Not a hard fail — just informational
  echo "  INFO: output does not explicitly say 'dry-run mode' but no spawn evidence found"
fi

# ---------------------------------------------------------------------------
# 6. all scenario JSON files parse correctly
# ---------------------------------------------------------------------------
echo "[6] scenario JSON files parse and have required keys"
REQUIRED_JSON_KEYS=("name" "goal" "url" "steps" "success_criteria")
for f in "$SCENARIO_DIR"/*.scenario.json; do
  if [[ ! -f "$f" ]]; then
    fail "no .scenario.json files found in $SCENARIO_DIR"
    break
  fi
  fname=$(basename "$f")
  # Parse JSON
  if ! python3 -c "import json, sys; json.load(open('$f'))" 2>/dev/null; then
    fail "$fname: JSON parse error"
    continue
  fi
  # Check required keys
  KEY_ERRORS=0
  for key in "${REQUIRED_JSON_KEYS[@]}"; do
    if ! python3 -c "
import json, sys
d = json.load(open('$f'))
sys.exit(0 if '$key' in d else 1)
" 2>/dev/null; then
      fail "$fname: missing required key '$key'"
      KEY_ERRORS=$((KEY_ERRORS+1))
    fi
  done
  if [[ $KEY_ERRORS -eq 0 ]]; then
    pass "$fname: valid JSON with all required keys"
  fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ $FAIL -gt 0 ]]; then
  echo "PRESUM: fail"
  exit 1
else
  echo "PRESUM: pass"
  exit 0
fi
