#!/usr/bin/env bash
# tests/test_commands_twin_divergence.sh — hermetic unit tests for
# scripts/ci/commands-twin-divergence-guard.sh (D#2486, extended D#2598 for
# the agents/scripts/memories families and the allowlist).
#
# Modelled on tests/test_state_dir_resolver_guard.sh: every fixture is a
# small synthetic tree built under mktemp -d, with a COPY of the real guard
# installed at the same relative path (scripts/ci/commands-twin-divergence-guard.sh)
# so its own REPO_ROOT resolution (BASH_SOURCE-derived) works inside the
# fixture, never against the live repo tree.
#
# Run: bash tests/test_commands_twin_divergence.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD_SRC="$REPO_ROOT/scripts/ci/commands-twin-divergence-guard.sh"
GUARD_REL="scripts/ci/commands-twin-divergence-guard.sh"
ALLOWLIST_REL="scripts/ci/twin-divergence-allowlist.json"
# The guard `source`s this to generate the agents/ mirror (D#2598 fix-round
# 2 item 2) — every fixture needs a copy at the same relative path, or the
# source fails, generate_agents_plugin_mirror is undefined, and every
# "agents" family assertion below would pass or fail for the WRONG reason
# (an empty generated file, not a real generation-based comparison) instead
# of erroring loudly. Caught exactly this way while writing this suite.
AGENTS_MIRROR_LIB_SRC="$REPO_ROOT/scripts/lib/agents-plugin-mirror.sh"
AGENTS_MIRROR_LIB_REL="scripts/lib/agents-plugin-mirror.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# new_fixture — synthetic tree with just the guard installed and both
# commands/ and agents/ directory pairs present (empty). loop-bootstrap/
# scripts and loop-bootstrap/memories are intentionally NOT created here —
# their absence (the normal post-D#2598 state) is asserted by dedicated
# tests below; individual tests that need them create them explicitly.
new_fixture() {
  local dir
  dir=$(mktemp -d)
  mkdir -p "$dir/scripts/ci" "$dir/scripts/lib" "$dir/.claude/commands" "$dir/commands" \
           "$dir/.claude/agents" "$dir/agents"
  cp "$GUARD_SRC" "$dir/$GUARD_REL"
  cp "$AGENTS_MIRROR_LIB_SRC" "$dir/$AGENTS_MIRROR_LIB_REL"
  printf '%s\n' "$dir"
}

write_pair() {
  # write_pair <dir> <family-subpath-a> <family-subpath-b> <name> <content>
  # writes the SAME content to both sides of a pair.
  local dir="$1" side_a="$2" side_b="$3" name="$4" content="$5"
  printf '%s' "$content" > "$dir/$side_a/$name"
  printf '%s' "$content" > "$dir/$side_b/$name"
}

write_allowlist() {
  # write_allowlist <dir> <json-body>
  local dir="$1" body="$2"
  printf '%s' "$body" > "$dir/$ALLOWLIST_REL"
}

run_guard() {
  local dir="$1"
  OUT="$(cd "$dir" && bash "$GUARD_REL" 2>&1)"
  RC=$?
}

echo "=== commands-twin-divergence-guard.sh hermetic tests ==="

# ═══════════════════════════════════════════════════════════════════════════
# commands family (D#2486, unchanged behavior)
# ═══════════════════════════════════════════════════════════════════════════

# ── Test 1: two identical pairs — passes, names both matched ──────────────
echo ""
echo "--- Test 1: identical pairs pass ---"
D1=$(new_fixture)
write_pair "$D1" ".claude/commands" "commands" "coldstart.md" $'# Coldstart\nstep one\n'
write_pair "$D1" ".claude/commands" "commands" "update.md" $'# Update\nstep one\n'
run_guard "$D1"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "PASS coldstart.md" && echo "$OUT" | grep -qF "PASS update.md"; then
  pass "two identical pairs: exit 0, both named PASS"
else
  fail "two identical pairs: expected exit 0 with both PASS, got rc=$RC out=$OUT"
fi
rm -rf "$D1"

# ── Test 2: one-character divergence — FAILS, names that pair only ────────
echo ""
echo "--- Test 2: one-character divergence fails, names the pair ---"
D2=$(new_fixture)
write_pair "$D2" ".claude/commands" "commands" "coldstart.md" $'# Coldstart\nstep one\n'
write_pair "$D2" ".claude/commands" "commands" "update.md" $'# Update\nstep one\n'
# Introduce a one-character difference in the .claude side only.
printf '%s' $'# Coldstart\nstep TWO\n' > "$D2/.claude/commands/coldstart.md"
run_guard "$D2"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "FAIL coldstart.md" && echo "$OUT" | grep -qF "PASS update.md"; then
  pass "one-character divergence: fails, names coldstart.md, update.md still PASS"
else
  fail "one-character divergence: expected named failure for coldstart.md only, got rc=$RC out=$OUT"
fi

# ── Test 3: revert the divergence — passes again (same fixture, D#2486 item 3) ─
echo ""
echo "--- Test 3: reverting the divergence passes again ---"
write_pair "$D2" ".claude/commands" "commands" "coldstart.md" $'# Coldstart\nstep one\n'
run_guard "$D2"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "PASS coldstart.md"; then
  pass "revert: exit 0, coldstart.md PASS again"
else
  fail "revert: expected exit 0 with coldstart.md PASS, got rc=$RC out=$OUT"
fi
rm -rf "$D2"

# ── Test 4: .claude/commands file with no top-level twin — FAILS, names it ─
echo ""
echo "--- Test 4: missing top-level twin fails ---"
D4=$(new_fixture)
write_pair "$D4" ".claude/commands" "commands" "update.md" $'# Update\nstep one\n'
printf '%s' $'# New command\n' > "$D4/.claude/commands/newcmd.md"
run_guard "$D4"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "FAIL newcmd.md" && echo "$OUT" | grep -qF "no top-level twin"; then
  pass "missing top-level twin: fails and names newcmd.md"
else
  fail "missing top-level twin: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D4"

# ── Test 5: commands/-only file with no .claude twin — NOTED, never fails ─
echo ""
echo "--- Test 5: top-level-only file is noted, not failed ---"
D5=$(new_fixture)
write_pair "$D5" ".claude/commands" "commands" "update.md" $'# Update\nstep one\n'
printf '%s' $'# Adopter-only doc\n' > "$D5/commands/adopteronly.md"
run_guard "$D5"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "NOTE adopteronly.md" && echo "$OUT" | grep -qF "not flagged"; then
  pass "top-level-only file: exit 0, printed as NOTE"
else
  fail "top-level-only file: expected exit 0 with a NOTE line, got rc=$RC out=$OUT"
fi
rm -rf "$D5"

# ── Test 6: .claude/commands/ itself missing — FAILS loudly ────────────────
echo ""
echo "--- Test 6: missing .claude/commands directory fails ---"
D6=$(mktemp -d)
mkdir -p "$D6/scripts/ci" "$D6/scripts/lib" "$D6/commands" "$D6/.claude/agents" "$D6/agents"
cp "$GUARD_SRC" "$D6/$GUARD_REL"
cp "$AGENTS_MIRROR_LIB_SRC" "$D6/$AGENTS_MIRROR_LIB_REL"
run_guard "$D6"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "is not a directory"; then
  pass "missing .claude/commands: fails loudly"
else
  fail "missing .claude/commands: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D6"

# ═══════════════════════════════════════════════════════════════════════════
# agents family (D#2598 item 12) — same asymmetric shape as commands
# ═══════════════════════════════════════════════════════════════════════════

# ── Test 7: agents divergence fails, names the pair ────────────────────────
echo ""
echo "--- Test 7: agents pair divergence fails ---"
D7=$(new_fixture)
write_pair "$D7" ".claude/commands" "commands" "update.md" $'# Update\n'
write_pair "$D7" ".claude/agents" "agents" "executor.md" $'# Executor\nrole card\n'
printf '%s' $'# Executor\nSTALE role card\n' > "$D7/agents/executor.md"
run_guard "$D7"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "FAIL executor.md" && echo "$OUT" | grep -qF "(agents)"; then
  pass "agents pair divergence: fails, names executor.md"
else
  fail "agents pair divergence: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D7"

# ── Test 8: agents file present only in .claude/agents — FAILS (no top-level twin) ─
echo ""
echo "--- Test 8: .claude/agents file missing its top-level twin fails ---"
D8=$(new_fixture)
write_pair "$D8" ".claude/commands" "commands" "update.md" $'# Update\n'
printf '%s' $'# New role\n' > "$D8/.claude/agents/newrole.md"
run_guard "$D8"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "FAIL newrole.md" && echo "$OUT" | grep -qF "no top-level twin"; then
  pass "agents missing top-level twin: fails and names newrole.md"
else
  fail "agents missing top-level twin: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D8"

# ── Test 9: top-level agents/-only file — NOTED, never fails ───────────────
echo ""
echo "--- Test 9: top-level-only agent file is noted, not failed ---"
D9=$(new_fixture)
write_pair "$D9" ".claude/commands" "commands" "update.md" $'# Update\n'
printf '%s' $'# Adopter-only role\n' > "$D9/agents/adopteronly.md"
run_guard "$D9"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "NOTE adopteronly.md" && echo "$OUT" | grep -qF "(agents)"; then
  pass "top-level-only agent file: exit 0, printed as NOTE"
else
  fail "top-level-only agent file: expected exit 0 with a NOTE line, got rc=$RC out=$OUT"
fi
rm -rf "$D9"

# ── Test 9b: agents/ is GENERATED, not byte-identical (D#2598 fix-round 2
# item 2) — a top-level file that differs from .claude/agents/ ONLY in the
# way generate_agents_plugin_mirror transforms it must PASS, not fail on a
# raw byte-diff.
echo ""
echo "--- Test 9b: correctly-generated agents/ file passes despite not being byte-identical ---"
D9B=$(new_fixture)
write_pair "$D9B" ".claude/commands" "commands" "update.md" $'# Update\n'
printf '%s' $'# Executor\nYou ONLY interact with `autonomous-agent-7/fulcrumaxe`.\n' > "$D9B/.claude/agents/executor.md"
printf '%s' $'# Executor\nYou ONLY interact with `$(source scripts/lib/repo-resolve.sh && _resolve_discussion_repo)`.\n' > "$D9B/agents/executor.md"
run_guard "$D9B"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "PASS executor.md (agents)"; then
  pass "correctly-generated (non-identical) agents/executor.md passes"
else
  fail "correctly-generated agents/executor.md should pass, got rc=$RC out=$OUT"
fi
rm -rf "$D9B"

# ── Test 9c: a literal identity leak in agents/ fails even if it happens to
# match itself byte-for-byte (the direct scan, independent of generation-match) ─
echo ""
echo "--- Test 9c: literal autonomous-agent-7 mention in agents/ fails directly ---"
D9C=$(new_fixture)
write_pair "$D9C" ".claude/commands" "commands" "update.md" $'# Update\n'
# Deliberately identical on both sides -- generation-match alone would NOT
# catch this if .claude/agents/ itself carried a spelling the generator
# doesn't know about; the direct scan must catch it regardless.
printf '%s' $'# Executor\nautonomous-agent-7 leaked here somehow\n' > "$D9C/.claude/agents/executor.md"
printf '%s' $'# Executor\nautonomous-agent-7 leaked here somehow\n' > "$D9C/agents/executor.md"
run_guard "$D9C"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "still contains a literal 'autonomous-agent-7' mention"; then
  pass "literal autonomous-agent-7 mention in agents/ fails via the direct scan"
else
  fail "literal autonomous-agent-7 mention in agents/ should fail via the direct scan, got rc=$RC out=$OUT"
fi
rm -rf "$D9C"

# ═══════════════════════════════════════════════════════════════════════════
# scripts family (D#2598 item 5) — driven by loop-bootstrap/scripts/, missing
# live twin is a NOTE not a FAIL (bootstrap-only residue scripts are expected)
# ═══════════════════════════════════════════════════════════════════════════

# ── Test 10: loop-bootstrap/scripts/ absent entirely — 0 pairs, exit 0 ─────
echo ""
echo "--- Test 10: absent loop-bootstrap/scripts/ is fine, not a failure ---"
D10=$(new_fixture)
write_pair "$D10" ".claude/commands" "commands" "update.md" $'# Update\n'
run_guard "$D10"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "does not exist — nothing to pair"; then
  pass "absent loop-bootstrap/scripts/: exit 0, noted"
else
  fail "absent loop-bootstrap/scripts/: expected exit 0 with a note, got rc=$RC out=$OUT"
fi
rm -rf "$D10"

# ── Test 11: bootstrap-only residue script with no live twin — NOTED ───────
echo ""
echo "--- Test 11: bootstrap-only residue script is noted, not failed ---"
D11=$(new_fixture)
write_pair "$D11" ".claude/commands" "commands" "update.md" $'# Update\n'
mkdir -p "$D11/loop-bootstrap/scripts"
printf '%s' $'#!/usr/bin/env bash\necho residue\n' > "$D11/loop-bootstrap/scripts/setup-deps.sh"
run_guard "$D11"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "NOTE setup-deps.sh" && echo "$OUT" | grep -qF "residue script, expected"; then
  pass "bootstrap-only residue script: exit 0, printed as NOTE"
else
  fail "bootstrap-only residue script: expected exit 0 with a NOTE line, got rc=$RC out=$OUT"
fi
rm -rf "$D11"

# ── Test 12: scripts pair divergence, unlisted — FAILS ─────────────────────
echo ""
echo "--- Test 12: unlisted scripts pair divergence fails ---"
D12=$(new_fixture)
write_pair "$D12" ".claude/commands" "commands" "update.md" $'# Update\n'
mkdir -p "$D12/loop-bootstrap/scripts" "$D12/scripts"
printf '%s' $'#!/usr/bin/env bash\necho live\n' > "$D12/scripts/start-dashboard.sh"
printf '%s' $'#!/usr/bin/env bash\necho variant\n' > "$D12/loop-bootstrap/scripts/start-dashboard.sh"
run_guard "$D12"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "FAIL start-dashboard.sh" && echo "$OUT" | grep -qF "(scripts)"; then
  pass "unlisted scripts divergence: fails, names start-dashboard.sh"
else
  fail "unlisted scripts divergence: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D12"

# ═══════════════════════════════════════════════════════════════════════════
# memories family (D#2598 item 13/14) — driven by loop-bootstrap/memories/,
# expected absent entirely post-D#2598 (0 pairs, exit 0)
# ═══════════════════════════════════════════════════════════════════════════

# ── Test 13: loop-bootstrap/memories/ absent entirely — 0 pairs, exit 0 ────
echo ""
echo "--- Test 13: absent loop-bootstrap/memories/ is fine, not a failure ---"
D13=$(new_fixture)
write_pair "$D13" ".claude/commands" "commands" "update.md" $'# Update\n'
run_guard "$D13"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "derived from scripts/memory-triage by tier"; then
  pass "absent loop-bootstrap/memories/: exit 0, noted"
else
  fail "absent loop-bootstrap/memories/: expected exit 0 with a note, got rc=$RC out=$OUT"
fi
rm -rf "$D13"

# ── Test 14: memories pair divergence, unlisted — FAILS ────────────────────
echo ""
echo "--- Test 14: unlisted memories pair divergence fails ---"
D14=$(new_fixture)
write_pair "$D14" ".claude/commands" "commands" "update.md" $'# Update\n'
mkdir -p "$D14/loop-bootstrap/memories" "$D14/scripts/memory-triage"
printf '%s' $'live content\n' > "$D14/scripts/memory-triage/feedback_x.md"
printf '%s' $'stale content\n' > "$D14/loop-bootstrap/memories/feedback_x.md"
run_guard "$D14"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "FAIL feedback_x.md" && echo "$OUT" | grep -qF "(memories)"; then
  pass "unlisted memories divergence: fails, names feedback_x.md"
else
  fail "unlisted memories divergence: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D14"

# ═══════════════════════════════════════════════════════════════════════════
# allowlist behavior (D#2598 item 7/8)
# ═══════════════════════════════════════════════════════════════════════════

# ── Test 15: divergent listed pair — exit 0, names the allowlist reason ────
echo ""
echo "--- Test 15: allowlisted divergent pair passes ---"
D15=$(new_fixture)
write_pair "$D15" ".claude/commands" "commands" "update.md" $'# Update\n'
mkdir -p "$D15/loop-bootstrap/scripts" "$D15/scripts"
printf '%s' $'#!/usr/bin/env bash\necho live\n' > "$D15/scripts/start-dashboard.sh"
printf '%s' $'#!/usr/bin/env bash\necho variant\n' > "$D15/loop-bootstrap/scripts/start-dashboard.sh"
write_allowlist "$D15" '{"entries":[{"pair":"scripts:start-dashboard.sh","date":"2026-09-16","reason":"deliberate project-agnostic variant, delegates via AF_ROOT"}]}'
run_guard "$D15"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -qF "PASS start-dashboard.sh" && echo "$OUT" | grep -qF "allowlisted deliberate variant"; then
  pass "allowlisted divergent pair: exit 0, reason named"
else
  fail "allowlisted divergent pair: expected exit 0 with the reason named, got rc=$RC out=$OUT"
fi
rm -rf "$D15"

# ── Test 16: allowlist entry missing a reason — guard FAILS (malformed allowlist) ─
echo ""
echo "--- Test 16: allowlist entry missing reason fails the guard ---"
D16=$(new_fixture)
write_pair "$D16" ".claude/commands" "commands" "update.md" $'# Update\n'
mkdir -p "$D16/loop-bootstrap/scripts" "$D16/scripts"
printf '%s' $'#!/usr/bin/env bash\necho live\n' > "$D16/scripts/start-dashboard.sh"
printf '%s' $'#!/usr/bin/env bash\necho variant\n' > "$D16/loop-bootstrap/scripts/start-dashboard.sh"
write_allowlist "$D16" '{"entries":[{"pair":"scripts:start-dashboard.sh","date":"2026-09-16","reason":""}]}'
run_guard "$D16"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "ALLOWLIST INVALID"; then
  pass "allowlist entry missing reason: guard fails, names allowlist invalid"
else
  fail "allowlist entry missing reason: expected a failure naming the allowlist, got rc=$RC out=$OUT"
fi
rm -rf "$D16"

# ── Test 17: allowlist entry with a "pending"-shaped reason — guard FAILS ──
echo ""
echo "--- Test 17: a 'pending reconciliation' reason fails the guard ---"
D17=$(new_fixture)
write_pair "$D17" ".claude/commands" "commands" "update.md" $'# Update\n'
mkdir -p "$D17/loop-bootstrap/scripts" "$D17/scripts"
printf '%s' $'#!/usr/bin/env bash\necho live\n' > "$D17/scripts/start-dashboard.sh"
printf '%s' $'#!/usr/bin/env bash\necho variant\n' > "$D17/loop-bootstrap/scripts/start-dashboard.sh"
write_allowlist "$D17" '{"entries":[{"pair":"scripts:start-dashboard.sh","date":"2026-09-16","reason":"pending reconciliation"}]}'
run_guard "$D17"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "ALLOWLIST INVALID"; then
  pass "'pending reconciliation' reason: guard fails, names allowlist invalid"
else
  fail "'pending reconciliation' reason: expected a failure naming the allowlist, got rc=$RC out=$OUT"
fi
rm -rf "$D17"

# ── Test 18: allowlist entry with a non-ISO date — guard FAILS (D#2598 fix-round item 5) ─
echo ""
echo "--- Test 18: a non-ISO-8601 date fails the guard ---"
D18=$(new_fixture)
write_pair "$D18" ".claude/commands" "commands" "update.md" $'# Update\n'
mkdir -p "$D18/loop-bootstrap/scripts" "$D18/scripts"
printf '%s' $'#!/usr/bin/env bash\necho live\n' > "$D18/scripts/start-dashboard.sh"
printf '%s' $'#!/usr/bin/env bash\necho variant\n' > "$D18/loop-bootstrap/scripts/start-dashboard.sh"
# "09/16/2026" is a real calendar date but not ISO 8601 shape; a regex-only
# check would also accept "2026-13-40" (wrong shape passes, wrong value
# doesn't) — this case and the next one each catch a different half.
write_allowlist "$D18" '{"entries":[{"pair":"scripts:start-dashboard.sh","date":"09/16/2026","reason":"deliberate project-agnostic variant"}]}'
run_guard "$D18"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "ALLOWLIST INVALID" && echo "$OUT" | grep -qiF "not ISO 8601"; then
  pass "non-ISO date '09/16/2026': guard fails, names the bad date"
else
  fail "non-ISO date '09/16/2026': expected a failure naming the bad date, got rc=$RC out=$OUT"
fi
rm -rf "$D18"

# ── Test 19: allowlist entry with an out-of-range calendar date — guard FAILS ─
echo ""
echo "--- Test 19: an ISO-shaped but impossible calendar date fails the guard ---"
D19=$(new_fixture)
write_pair "$D19" ".claude/commands" "commands" "update.md" $'# Update\n'
mkdir -p "$D19/loop-bootstrap/scripts" "$D19/scripts"
printf '%s' $'#!/usr/bin/env bash\necho live\n' > "$D19/scripts/start-dashboard.sh"
printf '%s' $'#!/usr/bin/env bash\necho variant\n' > "$D19/loop-bootstrap/scripts/start-dashboard.sh"
write_allowlist "$D19" '{"entries":[{"pair":"scripts:start-dashboard.sh","date":"2026-13-40","reason":"deliberate project-agnostic variant"}]}'
run_guard "$D19"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "ALLOWLIST INVALID" && echo "$OUT" | grep -qiF "not a real calendar date"; then
  pass "impossible calendar date '2026-13-40': guard fails, names it"
else
  fail "impossible calendar date '2026-13-40': expected a failure naming it, got rc=$RC out=$OUT"
fi
rm -rf "$D19"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
