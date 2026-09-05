#!/usr/bin/env bash
# tests/test_identifier_gate_allowlist_anchor.sh — regression test for the
# ALLOWLIST anchor parser in open-source/checks/identifier-gate.sh (D#2186
# fix round).
#
# Code review on the original D#2186 PR found rule 1 ("reject a colon in
# the anchor") was unreachable dead code: the check ran on $anchor AFTER
# it had already been split out with `${rest%%:*}`, which by construction
# can never contain a colon. Built a synthetic entry
# `fixture2.txt:owner: "badword":reason` — where the author's intended
# anchor was `owner: "badword"` — and showed it silently mis-parsed to
# anchor="owner", which happened to uniquely match the exact line the
# forbidden pattern also hit, so the gate reported PASS with rc=0 instead
# of rejecting the malformed entry. The fix moved the check to count
# colons on the raw entry before any split, so a 3rd colon anywhere in the
# line (not just "in the anchor" specifically — the gate can't tell which
# field it belongs to without already knowing the boundary) is a hard
# failure.
#
# This test runs the REAL script (a plain copy, not a reimplementation)
# against synthetic rules/fixtures in an isolated harness, because
# identifier-gate.sh hardcodes its rules-file path relative to its own
# location ($SCRIPT_DIR/../IDENTIFIER-RULES.txt) rather than taking one as
# an argument.
#
# Run: bash tests/test_identifier_gate_allowlist_anchor.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATE_SRC="$REPO_ROOT/open-source/checks/identifier-gate.sh"

PASS=0
FAIL=0

ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

assert_true() {
  local label="$1" rc="$2"
  [[ "$rc" -eq 0 ]] && ok "$label" || bad "$label" "expected rc 0, got $rc"
}

assert_false() {
  local label="$1" rc="$2"
  [[ "$rc" -ne 0 ]] && ok "$label" || bad "$label" "expected non-zero rc, got 0"
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  [[ "$haystack" == *"$needle"* ]] && ok "$label" || bad "$label" "did not find '$needle'"
}

# Builds an isolated harness: <harness>/checks/identifier-gate.sh (a copy
# of the real script, optionally mutated) plus <harness>/IDENTIFIER-RULES.txt
# written from the caller's here-string. Echoes the harness dir.
make_harness() {
  local rules_content="$1" mutate="${2:-0}"
  local harness
  harness="$(mktemp -d)"
  mkdir -p "$harness/checks"
  cp "$GATE_SRC" "$harness/checks/identifier-gate.sh"
  if [[ "$mutate" -eq 1 ]]; then
    # Cut the RULE1_CHECK block out entirely, reproducing the pre-fix
    # behavior the review found: the colon check ran on the (always
    # colon-free) post-split $anchor instead of the raw entry.
    sed -i '/# RULE1_CHECK_START/,/# RULE1_CHECK_END/d' "$harness/checks/identifier-gate.sh"
  fi
  chmod +x "$harness/checks/identifier-gate.sh"
  printf '%s\n' "$rules_content" > "$harness/IDENTIFIER-RULES.txt"
  echo "$harness"
}

run_gate() {
  local harness="$1" target="$2"
  OUT="$(bash "$harness/checks/identifier-gate.sh" "$target" 2>&1)"
  RC=$?
}

BASE_RULES='=== IDENTITIES_START ===
=== IDENTITIES_END ===

=== FORBIDDEN_PATTERNS_START ===
badword
=== FORBIDDEN_PATTERNS_END ===

=== ALLOWLIST_START ===
%ALLOWLIST_ENTRY%
=== ALLOWLIST_END ===
'

echo "== Control: a well-formed, colon-free anchor allowlists cleanly =="

CONTROL_DIR="$(mktemp -d)"
{
  echo "nothing forbidden on this line"
  echo "this line has badword right here"
  echo "trailer line"
} > "$CONTROL_DIR/fixture1.txt"

control_entry='fixture1.txt:this line has badword:covers the intentional badword fixture with a single well-formed anchor'
control_rules="${BASE_RULES//%ALLOWLIST_ENTRY%/$control_entry}"
CONTROL_HARNESS="$(make_harness "$control_rules" 0)"
run_gate "$CONTROL_HARNESS" "$CONTROL_DIR"
assert_true "control entry (exactly 2 colons) passes cleanly" "$RC"
assert_contains "control run reports PASS" "PASS (" "$OUT"

echo ""
echo "== Exploit: a colon-in-anchor entry must be rejected, not silently mis-parsed =="

EXPLOIT_DIR="$(mktemp -d)"
{
  echo "nothing here"
  printf 'owner: "badword" appears here\n'
  echo "nothing else"
} >> "$EXPLOIT_DIR/fixture2.txt"

# The reviewer's exact shape: intended anchor is `owner: "badword"` (a
# real colon-bearing substring on line 2), but a naive parser truncates it
# to "owner" at the first colon -- which still happens to be a unique,
# hit-covering match, so the old code accepted it.
exploit_entry='fixture2.txt:owner: "badword":reason'
exploit_rules="${BASE_RULES//%ALLOWLIST_ENTRY%/$exploit_entry}"
EXPLOIT_HARNESS="$(make_harness "$exploit_rules" 0)"
run_gate "$EXPLOIT_HARNESS" "$EXPLOIT_DIR"
assert_false "fixed gate rejects the colon-in-anchor entry" "$RC"
assert_contains "rejection names the colon-count rule" "need exactly 2" "$OUT"

echo ""
echo "== Mutation sanity: reverting to the post-split check lets the exploit through =="
echo "  (proves this test would actually have caught the original bug)"

MUTANT_HARNESS="$(make_harness "$exploit_rules" 1)"
run_gate "$MUTANT_HARNESS" "$EXPLOIT_DIR"
assert_true "mutant (RULE1_CHECK removed) wrongly PASSes the same exploit entry" "$RC"
assert_contains "mutant run wrongly reports PASS" "PASS (" "$OUT"

rm -rf "$CONTROL_DIR" "$EXPLOIT_DIR" "$CONTROL_HARNESS" "$EXPLOIT_HARNESS" "$MUTANT_HARNESS"

echo ""
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
