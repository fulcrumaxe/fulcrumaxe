#!/usr/bin/env bash
# tests/test_pre_spawn_engine_events.sh: pre-spawn-check.sh emits a spawn_blocked engine event
# (role + closed reason, nothing else) on each refusal, emits nothing under --dry-run, and an
# emitter that cannot write (ENGINE_EMIT_FORCE_FAIL=1) changes neither its exit code nor its output.
# Runs the REAL script in a sandboxed copy (tests/lib/pre-spawn-check-fixture.sh); never claude.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"
PASS=0
FAIL=0
ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }
check() { if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1: expected '$3', got '$2'"; fi; }

TMPDIR_BASE=$(mktemp -d)
trap 'rm -rf "$TMPDIR_BASE"' EXIT
source "$SCRIPT_DIR/lib/pre-spawn-check-fixture.sh"

# A sandbox that also carries the emitter and its schema.
make_sandbox() {
  local ws
  ws=$(make_workspace)
  install_sandbox "$ws"
  mkdir -p "$ws/schemas"
  cp "$SCRIPTS_DIR/engine-emit.sh" "$ws/scripts/engine-emit.sh"
  cp "$SCRIPTS_DIR/lib/engine_emit.py" "$SCRIPTS_DIR/lib/engine_agent_events.py" "$ws/scripts/lib/"
  cp "$SCRIPTS_DIR/lib/subagent_payload.py" "$ws/scripts/lib/"
  cp "$REPO_ROOT/schemas/engine-events.v1.json" "$ws/schemas/"
  echo "$ws"
}

mock_budget() {  # $1 ws, $2 allowed (True|False)
  cat > "$1/backend/budget.py" << PYEOF
import json, sys
print(json.dumps({"allowed": $2, "remaining": 0}))
sys.exit(0 if $2 else 1)
PYEOF
}
mock_circuit() {  # $1 ws, $2 failures
  printf 'import sys\nprint("%s")\n' "$2" > "$1/backend/circuit_breaker.py"
}

run() {  # $1 ws, rest = pre-spawn args; extra env passes through the caller's environment
  local ws="$1"; shift
  AUTONOMOUS_TEAM_STATE_DIR="$ws/state" run_psc_sandboxed "$ws" "$@"
}
events() { cat "$1/state/engine-events.jsonl" 2>/dev/null || true; }
field() {  # $1 ws, $2 event index, $3 key
  events "$1" | python3 -c "import sys,json; l=[json.loads(x) for x in sys.stdin if x.strip()]; print(l[$2].get('$3','<absent>'))"
}
count() { events "$1" | grep -c . || true; }

echo "--- budget refusal -> reason budget"
ws=$(make_sandbox); mock_budget "$ws" False
run "$ws" --role executor --discussion 999
check "exit code stays 1" "$(psc_exit "$ws")" "1"
check "one event" "$(count "$ws")" "1"
check "kind" "$(field "$ws" 0 kind)" "spawn_blocked"
check "reason" "$(field "$ws" 0 reason)" "budget"
check "role" "$(field "$ws" 0 role)" "executor"
check "discussion" "$(field "$ws" 0 discussion)" "999"
KEYS=$(events "$ws" | python3 -c "import sys,json; print(sorted(set(json.loads(sys.stdin.readline()))-{'v','seq','ts','engine_instance_id','kind'}))")
check "only role, discussion, reason" "$KEYS" "['discussion', 'reason', 'role']"
norm() { sed -E 's/evt-[0-9]+-[0-9]+/EVT/g' "$1"; }
STDOUT_OK=$(norm "$ws/psc.stdout"); STDERR_OK=$(norm "$ws/psc.stderr")

echo "--- same refusal with a failing emitter: identical exit and output, no event"
ws2=$(make_sandbox); mock_budget "$ws2" False
ENGINE_EMIT_FORCE_FAIL=1 run "$ws2" --role executor --discussion 999
check "exit code stays 1" "$(psc_exit "$ws2")" "1"
check "no event written" "$(count "$ws2")" "0"
check "stdout identical" "$(norm "$ws2/psc.stdout")" "$STDOUT_OK"
check "stderr identical" "$(norm "$ws2/psc.stderr")" "$STDERR_OK"

echo "--- circuit breaker -> circuit_breaker"
ws=$(make_sandbox); mock_circuit "$ws" 5
run "$ws" --role code-reviewer --discussion 12
check "exit 1" "$(psc_exit "$ws")" "1"
check "reason" "$(field "$ws" 0 reason)" "circuit_breaker"
check "role" "$(field "$ws" 0 role)" "code-reviewer"

echo "--- unknown role maps to other; general-purpose type is refused with exit 2"
ws=$(make_sandbox)
run "$ws" --role mystery-role --subagent-type general-purpose
check "exit 2" "$(psc_exit "$ws")" "2"
check "reason other" "$(field "$ws" 0 reason)" "other"
check "role other" "$(field "$ws" 0 role)" "other"
check "no discussion key" "$(field "$ws" 0 discussion)" "<absent>"

echo "--- --dry-run emits nothing"
ws=$(make_sandbox); mock_budget "$ws" False
run "$ws" --role executor --discussion 999 --dry-run
check "no event" "$(count "$ws")" "0"

echo "--- an allowed spawn emits nothing"
ws=$(make_sandbox); mock_budget "$ws" True
run "$ws" --role executor --discussion 999
check "no event" "$(count "$ws")" "0"

echo ""
echo "passed=$PASS failed=$FAIL"
[[ "$FAIL" -eq 0 ]]
