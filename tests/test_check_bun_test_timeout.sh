#!/usr/bin/env bash
# tests/test_check_bun_test_timeout.sh — hermetic unit tests for
# scripts/check-bun-test-timeout.sh (D#2276 acceptance items 11-12).
#
# Modelled on tests/test_check_tests_fixed_tmp_paths.sh: every fixture is a
# small synthetic git repo built under mktemp -d, with a COPY of the real
# check installed at the same relative path (scripts/check-bun-test-timeout.sh)
# so its own `git ls-files` and package.json-path resolution both work
# inside the fixture, never against the live tree.
#
# Run: bash tests/test_check_bun_test_timeout.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SRC="$REPO_ROOT/scripts/check-bun-test-timeout.sh"
CHECK_REL="scripts/check-bun-test-timeout.sh"
PKG_REL="ts-backend/package.json"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# new_fixture [test_script] — synthetic repo with the check installed and a
# ts-backend/package.json carrying the given "test" script value (default:
# the repo's own configured invocation).
new_fixture() {
  local test_script="${1:-bun test tests/ --timeout 30000}"
  local dir
  dir=$(mktemp -d)
  mkdir -p "$dir/scripts" "$dir/ts-backend" "$dir/tests"
  cp "$CHECK_SRC" "$dir/$CHECK_REL"
  chmod +x "$dir/$CHECK_REL"
  cat > "$dir/$PKG_REL" <<EOF
{
  "name": "fixture-ts-backend",
  "scripts": {
    "test": "$test_script"
  }
}
EOF
  git -C "$dir" init -q
  git -C "$dir" -c user.email=t@t.com -c user.name=Tester add -A
  printf '%s\n' "$dir"
}

# add_file <dir> <relpath> <content> — write a tracked file.
add_file() {
  local dir="$1" relpath="$2" content="$3"
  mkdir -p "$dir/$(dirname "$relpath")"
  printf '%s\n' "$content" > "$dir/$relpath"
  git -C "$dir" -c user.email=t@t.com -c user.name=Tester add -A
}

run_check() {
  local dir="$1"
  OUT="$(cd "$dir" && bash "$CHECK_REL" 2>&1)"
  RC=$?
}

echo "=== check-bun-test-timeout.sh hermetic tests ==="
echo ""

# ── Test 1: clean fixture, no other invocation sites — passes ─────────────
echo "--- Test 1: clean fixture ---"
D1=$(new_fixture)
run_check "$D1"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "OK: no tracked"; then
  pass "clean fixture: exits 0"
else
  fail "clean fixture: expected exit 0, got rc=$RC out=$OUT"
fi
rm -rf "$D1"

# ── Test 2: bare 'bun test tests/' invocation elsewhere — FAILS ───────────
echo ""
echo "--- Test 2: bare whole-suite invocation bypassing the script ---"
D2=$(new_fixture)
add_file "$D2" "scripts/run-something.sh" '#!/usr/bin/env bash
cd ts-backend && bun test tests/
'
run_check "$D2"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "scripts/run-something.sh:2 invokes bare 'bun test tests/'"; then
  pass "bare invocation: fails and names file:line"
else
  fail "bare invocation: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D2"

# ── Test 3: 'bun run test' invocation — passes (routes through the script) ─
echo ""
echo "--- Test 3: bun run test is not flagged ---"
D3=$(new_fixture)
add_file "$D3" "scripts/run-something.sh" '#!/usr/bin/env bash
cd ts-backend && bun run test
'
run_check "$D3"
if [[ "$RC" -eq 0 ]]; then
  pass "bun run test: exits 0"
else
  fail "bun run test: expected exit 0, got rc=$RC out=$OUT"
fi
rm -rf "$D3"

# ── Test 4: single-file / subdir invocation is NOT flagged ────────────────
# Mirrors the ~20 real "* Run: bun test tests/<file>.test.ts --timeout N"
# header comments this check must never flag (they are correct single-file
# debugging instructions, out of scope by design).
echo ""
echo "--- Test 4: scoped subpath invocation is not a whole-suite bypass ---"
D4=$(new_fixture)
add_file "$D4" "tests/foo.test.ts" '/**
 * Run: bun test tests/foo.test.ts --timeout 60000
 */
'
add_file "$D4" "scripts/run-subset.sh" '#!/usr/bin/env bash
bun test tests/spawn/
'
run_check "$D4"
if [[ "$RC" -eq 0 ]]; then
  pass "scoped subpath: not flagged, exits 0"
else
  fail "scoped subpath: expected exit 0, got rc=$RC out=$OUT"
fi
rm -rf "$D4"

# ── Test 5: reintroducing the bypass after a clean baseline goes red ──────
# Hermetic form of D#2276 acceptance item 11: rewrite a tracked invocation
# to bare 'bun test tests/', run the check, assert non-zero, then restore.
echo ""
echo "--- Test 5: reintroducing a bare invocation flips a clean tree to red ---"
D5=$(new_fixture)
add_file "$D5" "scripts/run-something.sh" '#!/usr/bin/env bash
cd ts-backend && bun run test
'
run_check "$D5"
BEFORE_RC=$RC
add_file "$D5" "scripts/run-something.sh" '#!/usr/bin/env bash
cd ts-backend && bun test tests/
'
run_check "$D5"
AFTER_RC=$RC
AFTER_OUT="$OUT"
add_file "$D5" "scripts/run-something.sh" '#!/usr/bin/env bash
cd ts-backend && bun run test
'
run_check "$D5"
RESTORED_RC=$RC
if [[ "$BEFORE_RC" -eq 0 ]] && [[ "$AFTER_RC" -ne 0 ]] && echo "$AFTER_OUT" | grep -qF "bun test tests/" && [[ "$RESTORED_RC" -eq 0 ]]; then
  pass "reintroduced bypass: clean (rc=0) -> red (rc=$AFTER_RC) -> restored (rc=0)"
else
  fail "reintroduced bypass: expected 0 -> nonzero -> 0, got before=$BEFORE_RC after=$AFTER_RC restored=$RESTORED_RC"
fi
rm -rf "$D5"

# ── Test 6: package.json 'test' script missing --timeout — FAILS ─────────
echo ""
echo "--- Test 6: test script with no --timeout ---"
D6=$(new_fixture "bun test tests/")
run_check "$D6"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "has no --timeout"; then
  pass "missing --timeout: fails"
else
  fail "missing --timeout: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D6"

# ── Test 7: no ts-backend/package.json at all — treated as nothing to govern
echo ""
echo "--- Test 7: no ts-backend/package.json ---"
D7=$(mktemp -d)
mkdir -p "$D7/scripts" "$D7/tests"
cp "$CHECK_SRC" "$D7/$CHECK_REL"
chmod +x "$D7/$CHECK_REL"
git -C "$D7" init -q
git -C "$D7" -c user.email=t@t.com -c user.name=Tester add -A
run_check "$D7"
if [[ "$RC" -eq 0 ]]; then
  pass "no package.json: exits 0 (nothing to govern)"
else
  fail "no package.json: expected exit 0, got rc=$RC out=$OUT"
fi
rm -rf "$D7"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
