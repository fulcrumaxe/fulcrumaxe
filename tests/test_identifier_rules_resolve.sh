#!/usr/bin/env bash
# tests/test_identifier_rules_resolve.sh
#
# Covers scripts/lib/identifier-rules-resolve.sh, a shared resolver that
# turns "does a usable identifier-rules source exist" into one of three
# enumerated states (present / declared-none / missing) instead of each
# caller writing its own `[[ -f ]]`.
#
# Two harnesses:
#   A. The resolver's three states, each a separate synthetic case, plus
#      the two edges a review round found: an IDENTITIES marker line must
#      match EXACTLY (not as a substring of a longer prose line), and a
#      NO_IDENTITIES=declared value must trim leading whitespace too, not
#      just trailing.
#   B. scripts/check-forbidden-identifiers.sh, invoked directly with the
#      rules source genuinely absent, proving it fails loudly rather than
#      silently — never a clean exit on a source it could not resolve.
#      Includes the specific regression a review round found: deleting
#      scripts/lib/identifier-rules-resolve.sh out from under this script
#      must still hard-fail at the state check itself, not fall through to
#      the vacuous-pass guard further down because an undefined function
#      and a silently-failed `source` (no `set -e` here) left $RULES_STATE
#      empty, which an `if == missing` would never catch but a `case` with
#      a hard-fail default does.
#
# Run: bash tests/test_identifier_rules_resolve.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESOLVER="$REPO_ROOT/scripts/lib/identifier-rules-resolve.sh"

PASS=0
FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

assert_eq() {
  local label="$1" want="$2" got="$3"
  if [[ "$got" == "$want" ]]; then ok "$label"; else bad "$label" "expected '$want', got '$got'"; fi
}

assert_rc_nonzero() {
  local label="$1" got="$2" out="$3"
  if [[ "$got" -ne 0 ]]; then ok "$label"; else bad "$label" "expected non-zero rc, got 0 | output: $out"; fi
}

assert_contains() {
  local label="$1" needle="$2" hay="$3"
  if [[ "$hay" == *"$needle"* ]]; then ok "$label"; else bad "$label" "expected output to contain '$needle', got: $hay"; fi
}

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "=== A. resolver states ==="

# Sourced once, directly (no subshell): the function's stdout is captured
# below with a plain redirect, which does not fork a subshell in bash, so
# its convenience global ($IDENTIFIER_RULES_RESOLVED_PATH) stays visible.
# A `$(...)` capture forks and would lose it -- every real caller instead
# reads BOTH fields off stdout with `IFS=$'\t' read -r STATE PATH <<< ...`.
# shellcheck source=scripts/lib/identifier-rules-resolve.sh
source "$RESOLVER"

read_state() { IFS=$'\t' read -r STATE RPATH <<< "$1"; }

R1="$SCRATCH/a-present"
mkdir -p "$R1"
{
  echo "=== IDENTITIES_START ==="
  echo "OLD_OWNER=zzsynthowner"
  echo "=== IDENTITIES_END ==="
} > "$R1/rules.txt"
_resolve_identifier_rules_state "$R1/rules.txt" > "$SCRATCH/a1.out"
read_state "$(cat "$SCRATCH/a1.out")"
assert_eq "file with IDENTITIES block -> present" "present" "$STATE"
assert_eq "present state resolves the matched path" "$R1/rules.txt" "$RPATH"

R2="$SCRATCH/a-declared-none"
mkdir -p "$R2"
echo "NO_IDENTITIES=declared" > "$R2/rules.txt"
_resolve_identifier_rules_state "$R2/rules.txt" > "$SCRATCH/a2.out"
read_state "$(cat "$SCRATCH/a2.out")"
assert_eq "no IDENTITIES block, NO_IDENTITIES=declared -> declared-none" "declared-none" "$STATE"

R3="$SCRATCH/a-missing-nofile"
mkdir -p "$R3"
_resolve_identifier_rules_state "$R3/rules.txt" > "$SCRATCH/a3.out"
read_state "$(cat "$SCRATCH/a3.out")"
assert_eq "no file at all -> missing" "missing" "$STATE"

R4="$SCRATCH/a-missing-undeclared"
mkdir -p "$R4"
echo "# a comment, no markers, no declaration" > "$R4/rules.txt"
_resolve_identifier_rules_state "$R4/rules.txt" > "$SCRATCH/a4.out"
read_state "$(cat "$SCRATCH/a4.out")"
assert_eq "file present, no IDENTITIES block, no declaration -> missing, not a soft skip" "missing" "$STATE"

R5="$SCRATCH/a-multi-candidate"
mkdir -p "$R5"
echo "OLD_OWNER=zzsynthowner" > "$R5/first.txt"   # no markers -> would be "missing" alone
{
  echo "=== IDENTITIES_START ==="
  echo "OLD_OWNER=zzsynthowner"
  echo "=== IDENTITIES_END ==="
} > "$R5/second.txt"
_resolve_identifier_rules_state "$R5/absent.txt" "$R5/second.txt" > "$SCRATCH/a5.out"
read_state "$(cat "$SCRATCH/a5.out")"
assert_eq "multi-candidate search skips an absent first candidate" "present" "$STATE"
assert_eq "multi-candidate search resolves the second, existing candidate" "$R5/second.txt" "$RPATH"

R6="$SCRATCH/a-marker-prefix-not-exact"
mkdir -p "$R6"
# A line that STARTS WITH the marker but continues with prose must not
# resolve "present" -- parse_block's own exact-equality test on the same
# marker would find no block here, and the resolver must not be the
# looser of the two.
echo "=== IDENTITIES_START === (this line explains the marker, it is not one)" > "$R6/rules.txt"
_resolve_identifier_rules_state "$R6/rules.txt" > "$SCRATCH/a6.out"
read_state "$(cat "$SCRATCH/a6.out")"
assert_eq "a marker-prefixed prose line does not count as the marker itself" "missing" "$STATE"

R7="$SCRATCH/a-leading-space-declaration"
mkdir -p "$R7"
printf 'NO_IDENTITIES= declared\n' > "$R7/rules.txt"
_resolve_identifier_rules_state "$R7/rules.txt" > "$SCRATCH/a7.out"
read_state "$(cat "$SCRATCH/a7.out")"
assert_eq "a declaration value with a leading space still trims to 'declared'" "declared-none" "$STATE"

echo "=== B. check-forbidden-identifiers.sh fails loudly with the rules source absent ==="

B1="$SCRATCH/b1"
mkdir -p "$B1/scripts/lib" "$B1/open-source"
cp "$REPO_ROOT/scripts/check-forbidden-identifiers.sh" "$B1/scripts/"
cp "$REPO_ROOT/scripts/lib/identity-resolve.sh" "$B1/scripts/lib/"
cp "$RESOLVER" "$B1/scripts/lib/"
OUT="$(bash "$B1/scripts/check-forbidden-identifiers.sh" --list-patterns 2>&1)"; RC=$?
assert_rc_nonzero "check-forbidden-identifiers.sh fails loudly on absent rules" "$RC" "$OUT"
assert_contains "it names the missing source" "not found or undeclared" "$OUT"

# The specific regression: the resolver library is genuinely gone (not
# just the rules file), so `source` fails with no `set -e` to stop the
# script, the resolver function is undefined, and $RULES_STATE comes back
# empty from the failed command substitution. Must still hard-fail AT THE
# STATE CHECK -- never fall through to the vacuous-pass guard ~300 lines
# further down and be rescued by that instead.
B2="$SCRATCH/b2"
mkdir -p "$B2/scripts/lib" "$B2/open-source"
cp "$REPO_ROOT/scripts/check-forbidden-identifiers.sh" "$B2/scripts/"
cp "$REPO_ROOT/scripts/lib/identity-resolve.sh" "$B2/scripts/lib/"
# Deliberately NOT copying identifier-rules-resolve.sh here.
{
  echo "=== IDENTITIES_START ==="
  echo "OLD_OWNER=zzsynthowner"
  echo "=== IDENTITIES_END ==="
  echo "=== FORBIDDEN_PATTERNS_START ==="
  echo "zzsynthpattern"
  echo "=== FORBIDDEN_PATTERNS_END ==="
} > "$B2/open-source/IDENTIFIER-RULES.txt"
OUT="$(bash "$B2/scripts/check-forbidden-identifiers.sh" --list-patterns 2>&1)"; RC=$?
assert_rc_nonzero "deleting the resolver library itself still hard-fails (not rescued downstream)" "$RC" "$OUT"
assert_contains "the failure is reported at the state check, naming the unrecognized state" "unrecognized state" "$OUT"

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
