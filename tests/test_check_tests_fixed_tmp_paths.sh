#!/usr/bin/env bash
# tests/test_check_tests_fixed_tmp_paths.sh — hermetic unit tests for
# scripts/check-tests-fixed-tmp-paths.sh (D#2254 criteria 4, 5, 6).
#
# Modelled on tests/test_no_hardcoded_checkout_paths_guard.sh: every fixture
# is a small synthetic git repo built under mktemp -d, with a COPY of the
# real check installed at the same relative path
# (scripts/check-tests-fixed-tmp-paths.sh) so its own `git ls-files
# tests/*.sh` and allowlist-path resolution both work inside the fixture,
# never against the live tests/ tree.
#
# Run: bash tests/test_check_tests_fixed_tmp_paths.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SRC="$REPO_ROOT/scripts/check-tests-fixed-tmp-paths.sh"
CHECK_REL="scripts/check-tests-fixed-tmp-paths.sh"
ALLOWLIST_REL="scripts/fixtures/allowed_fixed_tmp_literals.txt"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# new_fixture — empty synthetic repo with just the check installed.
new_fixture() {
  local dir
  dir=$(mktemp -d)
  mkdir -p "$dir/scripts/fixtures" "$dir/tests"
  cp "$CHECK_SRC" "$dir/$CHECK_REL"
  chmod +x "$dir/$CHECK_REL"
  : > "$dir/$ALLOWLIST_REL"
  git -C "$dir" init -q
  git -C "$dir" -c user.email=t@t.com -c user.name=Tester add -A
  printf '%s\n' "$dir"
}

# write_allowlist <dir> <lines...> — replace the fixture's allowlist content.
write_allowlist() {
  local dir="$1"
  shift
  : > "$dir/$ALLOWLIST_REL"
  local line
  for line in "$@"; do
    printf '%s\n' "$line" >> "$dir/$ALLOWLIST_REL"
  done
  git -C "$dir" -c user.email=t@t.com -c user.name=Tester add -A
}

# add_suite <dir> <name> <content> — write a test suite and track it.
add_suite() {
  local dir="$1" name="$2" content="$3"
  printf '%s\n' "$content" > "$dir/tests/$name"
  git -C "$dir" -c user.email=t@t.com -c user.name=Tester add -A
}

run_check() {
  local dir="$1"
  OUT="$(cd "$dir" && bash "$CHECK_REL" 2>&1)"
  RC=$?
}

echo "=== check-tests-fixed-tmp-paths.sh hermetic tests ==="
echo ""

# ── Test 1: empty fixture, no suites — passes with zero matches ────────────
echo "--- Test 1: empty tree ---"
D1=$(new_fixture)
run_check "$D1"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-position matches"; then
  pass "empty tree: exits 0 with 0 matches"
else
  fail "empty tree: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D1"

# ── Test 2: a real write-position match with no allowlist entry — FAILS ───
echo ""
echo "--- Test 2: unlisted fixed-path write ---"
D2=$(new_fixture)
add_suite "$D2" "test_foo.sh" '#!/usr/bin/env bash
echo hi > /tmp/foo-bar
'
run_check "$D2"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "tests/test_foo.sh:2 writes to fixed path '/tmp/foo-bar'"; then
  pass "unlisted write: fails and names file:line:literal"
else
  fail "unlisted write: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D2"

# ── Test 3: same suite, now allowlisted — passes ───────────────────────────
echo ""
echo "--- Test 3: allowlisted write passes ---"
D3=$(new_fixture)
add_suite "$D3" "test_foo.sh" '#!/usr/bin/env bash
echo hi > /tmp/foo-bar
'
write_allowlist "$D3" \
  "tests/test_foo.sh:/tmp/foo-bar:synthetic fixture literal for D#2254's own test suite."
run_check "$D3"
if [[ "$RC" -eq 0 ]]; then
  pass "allowlisted write: exits 0"
else
  fail "allowlisted write: expected exit 0, got rc=$RC out=$OUT"
fi
rm -rf "$D3"

# ── Test 4: mktemp's own template argument is never flagged ───────────────
echo ""
echo "--- Test 4: mktemp template argument is not a defect ---"
D4=$(new_fixture)
add_suite "$D4" "test_foo.sh" '#!/usr/bin/env bash
TMP=$(mktemp -d /tmp/test-foo-XXXXXX)
rm -rf "$TMP"
'
run_check "$D4"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-position matches"; then
  pass "mktemp template: not flagged, no allowlist entry required"
else
  fail "mktemp template: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D4"

# ── Test 5: Class C data (JSON payload) is never flagged ───────────────────
# This is the false-positive check from D#2254 criterion 5: a literal that
# reads as data (preceded by a colon, not a write verb or assignment) must
# never require an allowlist entry, with no per-file blanket exclusion.
echo ""
echo "--- Test 5: JSON test-payload literal is not write-position ---"
D5=$(new_fixture)
add_suite "$D5" "test_foo.sh" '#!/usr/bin/env bash
PAYLOAD="{\"cwd\": \"/tmp/some-fake-cwd\"}"
echo "$PAYLOAD"
'
run_check "$D5"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-position matches"; then
  pass "JSON payload literal: not flagged, no allowlist entry required"
else
  fail "JSON payload literal: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D5"

# ── Test 6: a $$-suffixed literal is STILL flagged, not treated as safe ────
# D#2254 deliberately does NOT exempt this shape: several suites named in
# the Discussion used exactly this pattern and needed the mktemp-dir fix
# anyway (a shared prefix distinguished only by PID isn't judged safe).
echo ""
echo "--- Test 6: \$\$-suffixed fixed prefix is still flagged ---"
D6=$(new_fixture)
add_suite "$D6" "test_foo.sh" '#!/usr/bin/env bash
OUT="/tmp/foo-out-$$.log"
echo hi > "$OUT"
'
run_check "$D6"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "/tmp/foo-out-"; then
  pass "\$\$-suffixed literal: still flagged (PID suffix is not treated as safe)"
else
  fail "\$\$-suffixed literal: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D6"

# ── Test 7: reintroducing a fixed path after a clean baseline goes red ─────
# Mirrors D#2254 acceptance criterion 4 directly, hermetically.
echo ""
echo "--- Test 7: reintroducing a fixed write flips a clean tree to red ---"
D7=$(new_fixture)
add_suite "$D7" "test_foo.sh" '#!/usr/bin/env bash
TMP=$(mktemp -d /tmp/test-foo-XXXXXX)
rm -rf "$TMP"
'
run_check "$D7"
BEFORE_RC=$RC
add_suite "$D7" "test_foo.sh" '#!/usr/bin/env bash
TMP=$(mktemp -d /tmp/test-foo-XXXXXX)
echo probe > /tmp/reintroduced-fixed-path
rm -rf "$TMP"
'
run_check "$D7"
if [[ "$BEFORE_RC" -eq 0 ]] && [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "/tmp/reintroduced-fixed-path"; then
  pass "reintroduced fixed path: clean tree (rc=0) flips to red (rc=$RC), names the path"
else
  fail "reintroduced fixed path: expected 0 -> nonzero flip naming the path, got before=$BEFORE_RC after=$RC out=$OUT"
fi
rm -rf "$D7"

# ── Test 8: stale allowlist entry (literal no longer present) — FAILS ─────
echo ""
echo "--- Test 8: stale allowlist entry ---"
D8=$(new_fixture)
add_suite "$D8" "test_foo.sh" '#!/usr/bin/env bash
echo hi > /tmp/foo-bar
'
write_allowlist "$D8" \
  "tests/test_foo.sh:/tmp/foo-bar:live entry, still matched." \
  "tests/test_foo.sh:/tmp/no-longer-here:PROBE — this literal does not exist in the fixture."
run_check "$D8"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "stale allowlist entry 'tests/test_foo.sh:/tmp/no-longer-here'"; then
  pass "stale entry: fails and names the stale key"
else
  fail "stale entry: expected a named stale failure, got rc=$RC out=$OUT"
fi
rm -rf "$D8"

# ── Test 9: dangling allowlist entry (path not in git ls-files) — FAILS ───
echo ""
echo "--- Test 9: dangling allowlist entry ---"
D9=$(new_fixture)
write_allowlist "$D9" \
  "tests/test_never_existed.sh:/tmp/foo-bar:PROBE — this file was never created in the fixture."
run_check "$D9"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "dangling allowlist entry 'tests/test_never_existed.sh:/tmp/foo-bar'"; then
  pass "dangling entry: fails and names the dangling key"
else
  fail "dangling entry: expected a named dangling failure, got rc=$RC out=$OUT"
fi
rm -rf "$D9"

# ── Test 10: malformed allowlist line — FAILS ──────────────────────────────
echo ""
echo "--- Test 10: malformed allowlist entry ---"
D10=$(new_fixture)
add_suite "$D10" "test_foo.sh" '#!/usr/bin/env bash
echo hi > /tmp/foo-bar
'
write_allowlist "$D10" "this-line-has-no-colons-at-all"
run_check "$D10"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "malformed allowlist entry"; then
  pass "malformed entry: fails and names it malformed"
else
  fail "malformed entry: expected a malformed-entry failure, got rc=$RC out=$OUT"
fi
rm -rf "$D10"

# ── Test 11: banned reason substring — FAILS ───────────────────────────────
echo ""
echo "--- Test 11: banned reason substring ---"
D11=$(new_fixture)
add_suite "$D11" "test_foo.sh" '#!/usr/bin/env bash
echo hi > /tmp/foo-bar
'
write_allowlist "$D11" \
  "tests/test_foo.sh:/tmp/foo-bar:parked for later cleanup."
run_check "$D11"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "banned reason"; then
  pass "banned reason: fails and names it banned"
else
  fail "banned reason: expected a banned-reason failure, got rc=$RC out=$OUT"
fi
rm -rf "$D11"

# ── Test 12: duplicate allowlist entry — FAILS ─────────────────────────────
echo ""
echo "--- Test 12: duplicate allowlist entry ---"
D12=$(new_fixture)
add_suite "$D12" "test_foo.sh" '#!/usr/bin/env bash
echo hi > /tmp/foo-bar
'
write_allowlist "$D12" \
  "tests/test_foo.sh:/tmp/foo-bar:first entry." \
  "tests/test_foo.sh:/tmp/foo-bar:duplicate of the entry above."
run_check "$D12"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "duplicate allowlist entry"; then
  pass "duplicate entry: fails and names it duplicate"
else
  fail "duplicate entry: expected a duplicate-entry failure, got rc=$RC out=$OUT"
fi
rm -rf "$D12"

# ── Test 13: a variable assignment RHS is write position too ──────────────
# The primary Class A shape from D#2254's own incidents: a fixed literal
# assigned to a variable, later used for the real write elsewhere.
echo ""
echo "--- Test 13: bare variable assignment is write position ---"
D13=$(new_fixture)
add_suite "$D13" "test_foo.sh" '#!/usr/bin/env bash
TARGET="/tmp/test-cold-start"
rm -rf "$TARGET"
mkdir -p "$TARGET"
'
run_check "$D13"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "/tmp/test-cold-start"; then
  pass "assignment RHS: flagged even though the write happens via the variable on a later line"
else
  fail "assignment RHS: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D13"

# ── Test 14: no allowlist file at all — hard FAIL, not a silent pass ──────
echo ""
echo "--- Test 14: missing allowlist file ---"
D14=$(new_fixture)
rm -f "$D14/$ALLOWLIST_REL"
git -C "$D14" -c user.email=t@t.com -c user.name=Tester add -A
run_check "$D14"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "allowlist file .* not found"; then
  pass "missing allowlist: fails loudly, not a silent pass"
else
  fail "missing allowlist: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D14"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
