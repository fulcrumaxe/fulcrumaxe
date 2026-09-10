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
#
# Never invokes claude, claude -p, spawn-agent.sh, or any real spawn path —
# scripts/pre-spawn-check.sh is exercised directly with --dry-run (no
# blackboard/circuit-breaker/budget/team-log writes) and a scratch
# AUTONOMOUS_TEAM_STATE_DIR per invocation.
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
