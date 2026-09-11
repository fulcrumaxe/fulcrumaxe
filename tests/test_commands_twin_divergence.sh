#!/usr/bin/env bash
# tests/test_commands_twin_divergence.sh — hermetic unit tests for
# scripts/ci/commands-twin-divergence-guard.sh (D#2486).
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

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# new_fixture — empty synthetic tree with just the guard installed and both
# command directories present (empty).
new_fixture() {
  local dir
  dir=$(mktemp -d)
  mkdir -p "$dir/scripts/ci" "$dir/.claude/commands" "$dir/commands"
  cp "$GUARD_SRC" "$dir/$GUARD_REL"
  printf '%s\n' "$dir"
}

write_pair() {
  # write_pair <dir> <name> <content> — writes the SAME content to both sides.
  local dir="$1" name="$2" content="$3"
  printf '%s' "$content" > "$dir/.claude/commands/$name"
  printf '%s' "$content" > "$dir/commands/$name"
}

run_guard() {
  local dir="$1"
  OUT="$(cd "$dir" && bash "$GUARD_REL" 2>&1)"
  RC=$?
}

echo "=== commands-twin-divergence-guard.sh hermetic tests ==="

# ── Test 1: two identical pairs — passes, names both matched ──────────────
echo ""
echo "--- Test 1: identical pairs pass ---"
D1=$(new_fixture)
write_pair "$D1" "coldstart.md" $'# Coldstart\nstep one\n'
write_pair "$D1" "update.md" $'# Update\nstep one\n'
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
write_pair "$D2" "coldstart.md" $'# Coldstart\nstep one\n'
write_pair "$D2" "update.md" $'# Update\nstep one\n'
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
write_pair "$D2" "coldstart.md" $'# Coldstart\nstep one\n'
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
write_pair "$D4" "update.md" $'# Update\nstep one\n'
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
write_pair "$D5" "update.md" $'# Update\nstep one\n'
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
mkdir -p "$D6/scripts/ci" "$D6/commands"
cp "$GUARD_SRC" "$D6/$GUARD_REL"
run_guard "$D6"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "is not a directory"; then
  pass "missing .claude/commands: fails loudly"
else
  fail "missing .claude/commands: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D6"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
