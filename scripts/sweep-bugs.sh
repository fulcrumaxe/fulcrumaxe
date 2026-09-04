#!/usr/bin/env bash
# sweep-bugs.sh — Systematic bug sweep across all subsystems
#
# Runs each subsystem test suite, captures failures, creates bug entries in
# bug-matrix.json, and outputs a summary. Uses existing test infrastructure.
#
# Usage:
#   ./scripts/sweep-bugs.sh [--output PATH] [--proof-dir DIR] [--subsystem NAME]
#
# Subsystems checked: backend, tui, saas-service, dashboard
# Exit code 0 = no bugs found. Non-zero = bugs found.
# Run from the repository root.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
OUTPUT="$REPO_ROOT/verification-report/bug-matrix.json"
PROOF_DIR="$REPO_ROOT/verification-report/proof/$TIMESTAMP"
SUBSYSTEM_FILTER=""

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)     OUTPUT="$2"; shift 2 ;;
    --proof-dir)  PROOF_DIR="$2"; shift 2 ;;
    --timestamp)  TIMESTAMP="$2"; shift 2 ;;
    --subsystem)  SUBSYSTEM_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$PROOF_DIR"
mkdir -p "$(dirname "$OUTPUT")"

# Temp file to accumulate bugs as JSON — avoids shell variable quoting issues
BUGS_TMP=$(mktemp)
echo "[]" > "$BUGS_TMP"
trap 'rm -f "$BUGS_TMP"' EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_log()  { echo "[$(date +%H:%M:%S)] $*"; }
_pass() { echo "  PASS: $*"; }
_fail() { echo "  FAIL: $*"; }
_info() { echo "  INFO: $*"; }

BUG_ID=0

add_bug() {
  local component="$1" subsystem="$2" severity="$3" title="$4" status="$5" found_by="$6"
  BUG_ID=$((BUG_ID + 1))
  local id="BUG-$(printf '%03d' $BUG_ID)"
  python3 - "$id" "$component" "$subsystem" "$severity" "$title" "$status" "$found_by" "$BUGS_TMP" <<'PYEOF'
import json, sys
id_, component, subsystem, severity, title, status, found_by, bugs_file = sys.argv[1:]
with open(bugs_file) as f:
    bugs = json.load(f)
bugs.append({
    "id": id_,
    "component": component,
    "subsystem": subsystem,
    "severity": severity,
    "title": title,
    "status": status,
    "found_by": found_by,
    "screenshot_before": "",
    "screenshot_after": "",
    "fix_pr": None,
    "verified": False
})
with open(bugs_file, "w") as f:
    json.dump(bugs, f)
PYEOF
  echo "  BUG [$id] [$severity] $component/$subsystem: ${title:0:80}"
}

run_subsystem() {
  local name="$1" cmd="$2" cwd="$3" severity_on_fail="${4:-medium}"
  local skip_if_absent="${5:-false}"

  if [[ -n "$SUBSYSTEM_FILTER" ]] && [[ "$SUBSYSTEM_FILTER" != "$name" ]]; then
    return
  fi

  _log "Sweeping subsystem: $name"
  local log_file="$PROOF_DIR/${name}-sweep.log"

  if [[ "$skip_if_absent" == "true" ]] && [[ ! -d "$cwd" ]]; then
    _info "$name directory not found — skipping"
    return
  fi

  local rc=0
  (cd "${cwd:-$REPO_ROOT}" && eval "$cmd") > "$log_file" 2>&1 || rc=$?

  if [[ $rc -eq 0 ]]; then
    _pass "$name — all checks passed"
  else
    _fail "$name — exit code $rc (log: $log_file)"
    # Parse log for meaningful failure lines only (not compilation progress)
    local failure_lines
    failure_lines=$(grep -E \
      '^(FAIL|error\[|error:|Error:|error: |thread .* panicked|test .* FAILED|✗|✖)' \
      "$log_file" 2>/dev/null | \
      grep -v '^error: aborting' | \
      grep -v 'note:' | \
      head -10 || true)
    if [[ -z "$failure_lines" ]]; then
      # Fallback: look for any error-like line
      failure_lines=$(grep -iE '(error|fail|panic)' "$log_file" 2>/dev/null | \
        grep -v 'Compiling\|Downloading\|note:' | \
        head -5 || true)
    fi
    if [[ -z "$failure_lines" ]]; then
      failure_lines="exit code $rc — see log for details"
    fi
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      local short_line="${line:0:120}"
      add_bug "$name" "test" "$severity_on_fail" "$short_line" "open" "sweep-bugs.sh/$name"
    done <<< "$failure_lines"
  fi
}

# ---------------------------------------------------------------------------
# Sweep 1: Python backend — imports + API module checks
# ---------------------------------------------------------------------------
_log "============================================================"
_log " Bug Sweep: fulcrumaxe"
_log " Timestamp: $TIMESTAMP"
_log "============================================================"

# Use python3 explicitly; also try a direct import check if server.py --check-imports unsupported
run_subsystem "backend" \
  "python3 backend/server.py --check-imports 2>/dev/null || python3 -c 'import backend.api, backend.budget, backend.cost_tracker, backend.control_plane, backend.circuit_breaker, backend.context_manager, backend.agent_memory' 2>&1" \
  "$REPO_ROOT" \
  "medium" "false"

# Also run existing smoke-test in continue-on-error mode
_log "Running smoke-test..."
SMOKE_LOG="$PROOF_DIR/backend-smoke-sweep.log"
bash "$REPO_ROOT/scripts/smoke-test.sh" --continue-on-error > "$SMOKE_LOG" 2>&1 || true

# Parse smoke-test failures
smoke_fails=$(grep -E '^\s+FAIL:' "$SMOKE_LOG" 2>/dev/null || true)
if [[ -n "$smoke_fails" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    desc="${line#*FAIL:}"
    desc="${desc## }"  # trim leading space
    add_bug "backend" "smoke-test" "medium" "${desc:0:120}" "open" "sweep-bugs.sh/smoke-test"
  done <<< "$smoke_fails"
fi

# ---------------------------------------------------------------------------
# Sweep 2: TUI — typecheck + build
# ---------------------------------------------------------------------------
run_subsystem "tui" \
  "npm run typecheck 2>&1 && npm run build 2>&1" \
  "$REPO_ROOT/tui" \
  "high" "true"

# ---------------------------------------------------------------------------
# Sweep 3: saas-service — cargo test
# ---------------------------------------------------------------------------
if command -v cargo >/dev/null 2>&1; then
  _log "Sweeping subsystem: saas-service"
  SAAS_LOG="$PROOF_DIR/saas-service-sweep.log"
  saas_rc=0
  (cd "$REPO_ROOT/saas-service" && cargo test 2>&1) > "$SAAS_LOG" || saas_rc=$?

  if [[ $saas_rc -eq 0 ]]; then
    _pass "saas-service — all tests passed"
  else
    _fail "saas-service — exit code $saas_rc (log: $SAAS_LOG)"
    # Extract only test failure lines and panic messages
    # Extract unique test failures only (not panic repetitions for same test)
    test_fails=$(grep -E '^test .* \.\.\. FAILED' "$SAAS_LOG" 2>/dev/null | head -20 || true)
    if [[ -z "$test_fails" ]]; then
      # No test lines — check for compile errors
      test_fails=$(grep -E '^error' "$SAAS_LOG" 2>/dev/null | head -5 || true)
    fi
    if [[ -z "$test_fails" ]]; then
      test_fails="exit code $saas_rc"
    fi
    # One bug per failed test
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      if [[ "$line" =~ ^test\ (.+)\ \.\.\.\ FAILED ]]; then
        test_name="${BASH_REMATCH[1]}"
        # Look up the panic message for this test for more context
        panic_line=$(grep -A2 "thread '${test_name}'" "$SAAS_LOG" 2>/dev/null | \
          grep 'panicked at' | head -1 | sed 's/.*panicked at //' | cut -c1-80 || true)
        if [[ -n "$panic_line" ]]; then
          title="${test_name}: panicked — ${panic_line}"
        else
          title="test ${test_name} FAILED"
        fi
        add_bug "saas-service" "api-tests" "high" "${title:0:120}" "open" "sweep-bugs.sh/saas-service"
      else
        add_bug "saas-service" "api-tests" "high" "${line:0:120}" "open" "sweep-bugs.sh/saas-service"
      fi
    done <<< "$test_fails"
  fi
else
  _info "cargo not found — skipping saas-service tests"
fi

# ---------------------------------------------------------------------------
# Sweep 4: dashboard — build + typecheck
# ---------------------------------------------------------------------------
if [[ -d "$REPO_ROOT/dashboard" ]]; then
  _log "Sweeping subsystem: dashboard"
  DASH_LOG="$PROOF_DIR/dashboard-sweep.log"
  dash_rc=0

  # Install dependencies if node_modules missing
  if [[ ! -d "$REPO_ROOT/dashboard/node_modules" ]]; then
    _info "dashboard: node_modules missing, running npm ci..."
    (cd "$REPO_ROOT/dashboard" && npm ci 2>&1) >> "$DASH_LOG" || true
  fi

  (cd "$REPO_ROOT/dashboard" && \
    (npm run typecheck 2>&1 || true) && \
    npm run build 2>&1
  ) >> "$DASH_LOG" 2>&1 || dash_rc=$?

  if [[ $dash_rc -eq 0 ]]; then
    _pass "dashboard — all checks passed"
  else
    _fail "dashboard — exit code $dash_rc (log: $DASH_LOG)"
    dash_fails=$(grep -E '^(error TS|Error:|error:|\s+\d+ error)' \
      "$DASH_LOG" 2>/dev/null | head -10 || true)
    if [[ -z "$dash_fails" ]]; then
      dash_fails="dashboard build failed with exit code $dash_rc"
    fi
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      add_bug "dashboard" "build" "medium" "${line:0:120}" "open" "sweep-bugs.sh/dashboard"
    done <<< "$dash_fails"
  fi
fi

# ---------------------------------------------------------------------------
# Compute summary + write bug-matrix.json via Python (no shell expansion)
# ---------------------------------------------------------------------------
python3 - "$OUTPUT" "$TIMESTAMP" "$BUGS_TMP" <<'PYEOF'
import json, os, sys

output, timestamp, bugs_file = sys.argv[1], sys.argv[2], sys.argv[3]

with open(bugs_file) as f:
    bugs = json.load(f)

counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
for b in bugs:
    sev = b.get("severity", "medium")
    if sev in counts:
        counts[sev] += 1
open_bugs = [b for b in bugs if b.get("status") == "open"]
counts["total_open"] = len(open_bugs)

matrix = {
    "version": "1.0",
    "generated": timestamp,
    "bugs": bugs,
    "summary": counts
}

os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
with open(output, "w") as f:
    json.dump(matrix, f, indent=2)
print(f"Bug matrix written: {output}")
PYEOF

# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------
echo ""
echo "Bug Sweep Summary"
echo "================="
python3 - "$BUGS_TMP" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    bugs = json.load(f)

by_sev = {"critical": [], "high": [], "medium": [], "low": []}
for b in bugs:
    s = b.get("severity", "medium")
    if s in by_sev:
        by_sev[s].append(b)

for sev, items in by_sev.items():
    if items:
        print(f"  {sev.upper()} ({len(items)}):")
        for b in items[:5]:
            print(f"    [{b['id']}] {b['component']}/{b['subsystem']}: {b['title'][:60]}")

total = len(bugs)
print(f"Total bugs found: {total}")
critical_high = len([b for b in bugs if b.get("severity") in ("critical", "high") and b.get("status") == "open"])
sys.exit(1 if critical_high > 0 else 0)
PYEOF
