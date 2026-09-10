#!/usr/bin/env bash
# tests/test_agent_scratchpad_block.sh — verify the D#2360 Scratchpad Convention
# block reaches every role via scripts/pre-spawn-check.sh, and that the check
# itself is sensitive to the injection being removed (a mutation check — a
# test that still passes with the injection removed is testing nothing).
#
# What is tested:
#   1. scripts/lib/agent-scratchpad.sh's agent_scratchpad_block is directly
#      callable and prints a non-empty block with no hardcoded /tmp path.
#   2. Every role under .claude/agents/*.md gets the block in
#      scripts/pre-spawn-check.sh's --dry-run JSON output (item 4 — asserted
#      by iterating the real role list, not spot-checking).
#   3. Mutation check (item 5): with the agent_scratchpad_block call removed
#      from a scratch copy of pre-spawn-check.sh, the same role-coverage
#      check must FAIL — proving item 2's check actually depends on the
#      injection, then restoring and confirming it passes again.
#   4. Item 6: pre-spawn-check.sh's JSON reaching --dry-run is necessary but
#      not sufficient — the field still has to survive backend/spawn_payload.py
#      and backend/prompt_builder.py to reach a live agent. Item 6 runs the
#      real three-step pipeline scripts/spawn-agent.sh itself runs
#      (pre-spawn-check.sh --dry-run -> backend.spawn_payload ->
#      backend.prompt_builder render) and asserts the block is present in the
#      ASSEMBLED PROMPT TEXT, not just the intermediate JSON — then mutates
#      spawn_payload.py to drop the key and confirms the assembled prompt
#      loses the block, proving this check actually depends on that wiring.
#
# Never invokes claude, claude -p, spawn-agent.sh, or any real spawn path —
# scripts/pre-spawn-check.sh is exercised directly with --dry-run (no
# blackboard/circuit-breaker/budget/team-log writes) and a scratch
# AUTONOMOUS_TEAM_STATE_DIR per invocation. Item 6 calls backend.spawn_payload
# and backend.prompt_builder directly (the same two modules spawn-agent.sh
# invokes), never spawn-agent.sh itself.
#
# Usage: bash tests/test_agent_scratchpad_block.sh
# Exits 0 if all checks pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REAL_PSC="$REPO_ROOT/scripts/pre-spawn-check.sh"
AGENT_SCRATCHPAD_LIB="$REPO_ROOT/scripts/lib/agent-scratchpad.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

# ── Item 1: agent_scratchpad_block is directly callable ────────────────────────

echo "=== item 1: scripts/lib/agent-scratchpad.sh is directly callable ==="

if [[ ! -f "$AGENT_SCRATCHPAD_LIB" ]]; then
  fail "module exists" "$AGENT_SCRATCHPAD_LIB not found"
else
  BLOCK_OUTPUT=$(bash -c "source '$AGENT_SCRATCHPAD_LIB' && agent_scratchpad_block")
  BLOCK_RC=$?
  if [[ $BLOCK_RC -eq 0 && -n "$BLOCK_OUTPUT" ]]; then
    pass "agent_scratchpad_block sourced and called directly: exit 0, non-empty"
  else
    fail "direct invocation" "exit=$BLOCK_RC output_len=${#BLOCK_OUTPUT}"
  fi

  # Item 3: no hardcoded /tmp path in the emitted instruction, and it names a
  # derived (not fixed) directory.
  if echo "$BLOCK_OUTPUT" | grep -q '/tmp/'; then
    fail "no hardcoded /tmp path" "block text contains a literal /tmp/ path"
  else
    pass "block text contains no hardcoded /tmp path"
  fi
  if echo "$BLOCK_OUTPUT" | grep -qi "convention"; then
    pass "block plainly names itself a convention (not enforcement)"
  else
    fail "convention framing" "block does not mention 'convention'"
  fi
fi

# ── Helper: run pre-spawn-check.sh --dry-run for one role, extract the field ──

# check_role <script_path> <role>
# Prints "yes" or "no" on stdout: whether the JSON output's agent_scratchpad
# field contains the block's distinctive text.
check_role() {
  local script="$1" role="$2" state_dir out_file err_file
  state_dir=$(mktemp -d)
  out_file=$(mktemp)
  err_file=$(mktemp)
  AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash "$script" --role "$role" --dry-run \
    >"$out_file" 2>"$err_file"
  python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    block = d.get('agent_scratchpad', '') or ''
    print('yes' if 'Scratchpad Convention' in block and 'shared' in block else 'no')
except Exception:
    print('no')
" "$out_file"
  rm -rf "$state_dir"
  rm -f "$out_file" "$err_file"
}

# ── Enumerate roles from .claude/agents/*.md — the real role list, not a
#    hand-picked spot-check subset (item 4's explicit requirement). ───────────

ROLES=()
for f in "$REPO_ROOT"/.claude/agents/*.md; do
  [[ -f "$f" ]] || continue
  ROLES+=("$(basename "$f" .md)")
done

if [[ ${#ROLES[@]} -eq 0 ]]; then
  fail "role enumeration" "no role files found under .claude/agents/*.md"
fi

# ── Item 4: every role reached, against the real script ────────────────────────

echo ""
echo "=== item 4: every role in .claude/agents/*.md receives the block (${#ROLES[@]} roles) ==="

MISSING_ROLES=()
for role in "${ROLES[@]}"; do
  if [[ "$(check_role "$REAL_PSC" "$role")" != "yes" ]]; then
    MISSING_ROLES+=("$role")
  fi
done

if [[ ${#MISSING_ROLES[@]} -eq 0 ]]; then
  pass "all ${#ROLES[@]} roles receive the Scratchpad Convention block via pre-spawn-check.sh --dry-run"
else
  fail "role coverage" "roles missing the block: ${MISSING_ROLES[*]}"
fi

# ── Item 5: mutation check ──────────────────────────────────────────────────────
#
# Build a scratch harness: scripts/lib symlinked to the real lib/ (so sourcing
# still resolves every sibling helper unchanged) but pre-spawn-check.sh itself
# is a real, mutable copy. Never touches the real file under test.

echo ""
echo "=== item 5: mutation check -- injection removal must break item 4's own check ==="

HARNESS_DIR=$(mktemp -d)
mkdir -p "$HARNESS_DIR/scripts"
ln -s "$REPO_ROOT/scripts/lib" "$HARNESS_DIR/scripts/lib"
cp "$REAL_PSC" "$HARNESS_DIR/scripts/pre-spawn-check.sh"
HARNESS_PSC="$HARNESS_DIR/scripts/pre-spawn-check.sh"

PROBE_ROLE="${ROLES[0]}"

# Harness sanity: the pristine copy must still emit the block before we trust
# a later "no block" result as meaning anything.
if [[ "$(check_role "$HARNESS_PSC" "$PROBE_ROLE")" == "yes" ]]; then
  pass "harness sanity: pristine copy still emits the block for $PROBE_ROLE"
else
  fail "harness sanity" "pristine copy of pre-spawn-check.sh does not emit the block for $PROBE_ROLE -- harness is broken, this is not a real finding"
fi

# Remove the agent_scratchpad_block call (neuter the one line that invokes it).
if grep -q 'AGENT_SCRATCHPAD=\$(agent_scratchpad_block' "$HARNESS_PSC"; then
  sed -i 's/AGENT_SCRATCHPAD=\$(agent_scratchpad_block 2>\/dev\/null || true)/AGENT_SCRATCHPAD=""/' "$HARNESS_PSC"
else
  fail "mutation setup" "expected agent_scratchpad_block call line not found in pre-spawn-check.sh -- injection point moved without updating this test"
fi

MUTATION_MISSING_ROLE=""
for role in "${ROLES[@]}"; do
  if [[ "$(check_role "$HARNESS_PSC" "$role")" != "yes" ]]; then
    MUTATION_MISSING_ROLE="$role"
    break
  fi
done

if [[ -n "$MUTATION_MISSING_ROLE" ]]; then
  pass "mutation check: with the call removed, $MUTATION_MISSING_ROLE's prompt correctly lacks the block"
else
  fail "mutation check" "removing the agent_scratchpad_block call did not make ANY role's prompt lose the block -- item 4's check is testing nothing"
fi

# Restore: re-copy the pristine real file and confirm item 4 passes again.
cp "$REAL_PSC" "$HARNESS_PSC"
if [[ "$(check_role "$HARNESS_PSC" "$PROBE_ROLE")" == "yes" ]]; then
  pass "restore: after restoring the call, $PROBE_ROLE's prompt contains the block again"
else
  fail "restore" "after restoring the call, $PROBE_ROLE's prompt still lacks the block"
fi

rm -rf "$HARNESS_DIR"

# ── Item 6: the block must reach the ASSEMBLED PROMPT, not just PSC's JSON ─────
#
# check_role() above only proves pre-spawn-check.sh --dry-run's own JSON
# carries the field. backend/spawn_payload.py and backend/prompt_builder.py
# forward named PSC keys one at a time (there is no generic pass-through), so
# a key can reach --dry-run's JSON and still never reach a spawned agent if
# either of those two files doesn't name it. This is exactly what happened
# here (D#2360 review round 1): the field was in the JSON, 26/26 roles, and
# still reached zero live agents.
#
# assemble_prompt runs the real three-step pipeline scripts/spawn-agent.sh
# runs for an actual spawn: pre-spawn-check.sh --dry-run -> backend.spawn_payload
# -> backend.prompt_builder render. Prints the assembled prompt text on stdout,
# or nothing on any pipeline-stage failure.

echo ""
echo "=== item 6: the block reaches the real ASSEMBLED PROMPT (not just PSC JSON) ==="

assemble_prompt() {
  local role="$1" state_dir psc_raw payload_err builder_err spawn_json assembled psc_exit payload_exit builder_exit
  state_dir=$(mktemp -d)
  psc_raw=$(AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash "$REAL_PSC" --role "$role" --dry-run 2>/dev/null)
  psc_exit=$?
  rm -rf "$state_dir"
  if [[ $psc_exit -ne 0 ]]; then
    return 1
  fi

  # cd into REPO_ROOT before invoking python -m: `-m` always puts CWD first on
  # sys.path, ahead of PYTHONPATH -- run from anywhere else and Python silently
  # imports whatever backend/ package happens to sit under CWD instead of the
  # one PYTHONPATH points at. This bit exactly once while writing this test.
  payload_err=$(mktemp)
  spawn_json=$(cd "$REPO_ROOT" && PSC_JSON_INPUT="$psc_raw" _ROLE="$role" _DISC="" _TASK="test task" \
    AUTONOMOUS_TEAM_REPO="${AUTONOMOUS_TEAM_REPO:-fulcrumaxe/fulcrumaxe}" \
    PYTHONPATH="$REPO_ROOT" python3 -m backend.spawn_payload 2>"$payload_err")
  payload_exit=$?
  rm -f "$payload_err"
  if [[ $payload_exit -ne 0 || -z "$spawn_json" ]]; then
    return 1
  fi

  builder_err=$(mktemp)
  assembled=$(cd "$REPO_ROOT" && SPAWN_PROMPT_JSON="$spawn_json" \
    AUTONOMOUS_TEAM_REPO="${AUTONOMOUS_TEAM_REPO:-fulcrumaxe/fulcrumaxe}" \
    PYTHONPATH="$REPO_ROOT" python3 -m backend.prompt_builder render 2>"$builder_err")
  builder_exit=$?
  rm -f "$builder_err"
  if [[ $builder_exit -ne 0 || -z "$assembled" ]]; then
    return 1
  fi
  printf '%s' "$assembled"
  return 0
}

PROBE_ROLE_6="executor"
ASSEMBLED_REAL=$(assemble_prompt "$PROBE_ROLE_6")
if [[ -z "$ASSEMBLED_REAL" ]]; then
  fail "item 6 pipeline" "the real pre-spawn-check.sh -> spawn_payload -> prompt_builder pipeline failed for role $PROBE_ROLE_6 -- cannot evaluate the assembled prompt"
elif echo "$ASSEMBLED_REAL" | grep -q "Scratchpad Convention"; then
  pass "real assembled prompt for $PROBE_ROLE_6 contains the Scratchpad Convention block (pre-spawn-check.sh --dry-run -> spawn_payload -> prompt_builder render)"
else
  fail "item 6 block reaches assembled prompt" "role $PROBE_ROLE_6's real assembled prompt (${#ASSEMBLED_REAL} bytes) does not contain 'Scratchpad Convention' -- the field is not surviving spawn_payload.py/prompt_builder.py"
fi

# Mutation check: neuter spawn_payload.py's forwarding of agent_scratchpad in a
# scratch copy of backend/ and confirm the assembled prompt loses the block --
# proving the check above actually depends on that wiring, not on PSC alone.
# The whole backend/ tree is copied (not cherry-picked files) so prompt_builder's
# lazy `from backend.spawn_templates import ...` and friends resolve exactly as
# they do for real -- only spawn_payload.py in the copy is then mutated.
PAYLOAD_HARNESS_DIR=$(mktemp -d)
cp -r "$REPO_ROOT/backend" "$PAYLOAD_HARNESS_DIR/backend"

assemble_prompt_harness() {
  local role="$1" state_dir psc_raw payload_err builder_err spawn_json assembled psc_exit payload_exit builder_exit
  state_dir=$(mktemp -d)
  psc_raw=$(AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash "$REAL_PSC" --role "$role" --dry-run 2>/dev/null)
  psc_exit=$?
  rm -rf "$state_dir"
  if [[ $psc_exit -ne 0 ]]; then
    return 1
  fi

  # cd into PAYLOAD_HARNESS_DIR for the same reason as assemble_prompt() above:
  # `-m` puts CWD ahead of PYTHONPATH on sys.path.
  payload_err=$(mktemp)
  spawn_json=$(cd "$PAYLOAD_HARNESS_DIR" && PSC_JSON_INPUT="$psc_raw" _ROLE="$role" _DISC="" _TASK="test task" \
    AUTONOMOUS_TEAM_REPO="${AUTONOMOUS_TEAM_REPO:-fulcrumaxe/fulcrumaxe}" \
    PYTHONPATH="$PAYLOAD_HARNESS_DIR" python3 -m backend.spawn_payload 2>"$payload_err")
  payload_exit=$?
  rm -f "$payload_err"
  if [[ $payload_exit -ne 0 || -z "$spawn_json" ]]; then
    return 1
  fi

  builder_err=$(mktemp)
  assembled=$(cd "$PAYLOAD_HARNESS_DIR" && SPAWN_PROMPT_JSON="$spawn_json" \
    AUTONOMOUS_TEAM_REPO="${AUTONOMOUS_TEAM_REPO:-fulcrumaxe/fulcrumaxe}" \
    PYTHONPATH="$PAYLOAD_HARNESS_DIR" python3 -m backend.prompt_builder render 2>"$builder_err")
  builder_exit=$?
  rm -f "$builder_err"
  if [[ $builder_exit -ne 0 || -z "$assembled" ]]; then
    return 1
  fi
  printf '%s' "$assembled"
  return 0
}

# Harness sanity: pristine copy must still produce the block before a later
# "no block" result means anything.
ASSEMBLED_HARNESS_SANITY=$(assemble_prompt_harness "$PROBE_ROLE_6")
if echo "$ASSEMBLED_HARNESS_SANITY" | grep -q "Scratchpad Convention"; then
  pass "item 6 harness sanity: pristine backend/ copy still assembles the block for $PROBE_ROLE_6"
else
  fail "item 6 harness sanity" "pristine copy of backend/spawn_payload.py + backend/prompt_builder.py does not assemble the block for $PROBE_ROLE_6 -- harness is broken, this is not a real finding"
fi

# Neuter the forwarding line in the harness copy of spawn_payload.py.
if grep -q '"agent_scratchpad":' "$PAYLOAD_HARNESS_DIR/backend/spawn_payload.py"; then
  sed -i '/"agent_scratchpad":.*psc\.get("agent_scratchpad"/d' "$PAYLOAD_HARNESS_DIR/backend/spawn_payload.py"
else
  fail "item 6 mutation setup" "expected agent_scratchpad forwarding line not found in backend/spawn_payload.py -- wiring moved without updating this test"
fi

ASSEMBLED_MUTATED=$(assemble_prompt_harness "$PROBE_ROLE_6")
if [[ -n "$ASSEMBLED_MUTATED" ]] && ! echo "$ASSEMBLED_MUTATED" | grep -q "Scratchpad Convention"; then
  pass "item 6 mutation check: with spawn_payload.py's forwarding removed, $PROBE_ROLE_6's assembled prompt correctly lacks the block"
else
  fail "item 6 mutation check" "removing spawn_payload.py's agent_scratchpad forwarding did not make the assembled prompt lose the block -- item 6's check is testing nothing"
fi

rm -rf "$PAYLOAD_HARNESS_DIR"

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
exit 0
