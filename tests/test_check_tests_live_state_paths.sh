#!/usr/bin/env bash
# tests/test_check_tests_live_state_paths.sh — hermetic unit tests for
# scripts/check-tests-live-state-paths.sh (D#2267 Spec item 6).
#
# Modelled on tests/test_check_tests_fixed_tmp_paths.sh: every fixture is a
# small synthetic git repo built under mktemp -d, with a COPY of the real
# check installed at the same relative path
# (scripts/check-tests-live-state-paths.sh) so its own `git ls-files
# tests/*.sh` and allowlist-path resolution both work inside the fixture,
# never against the live tests/ tree.
#
# Run: bash tests/test_check_tests_live_state_paths.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SRC="$REPO_ROOT/scripts/check-tests-live-state-paths.sh"
CHECK_REL="scripts/check-tests-live-state-paths.sh"
ALLOWLIST_REL="scripts/fixtures/allowed_live_state_literals.txt"

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

echo "=== check-tests-live-state-paths.sh hermetic tests ==="
echo ""

# ── Test 1: empty fixture, no suites — passes with zero matches ────────────
echo "--- Test 1: empty tree ---"
D1=$(new_fixture)
run_check "$D1"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-or-assert-position matches"; then
  pass "empty tree: exits 0 with 0 matches"
else
  fail "empty tree: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D1"

# ── Test 2: a real live-state write with no allowlist entry — FAILS ───────
echo ""
echo "--- Test 2: unlisted live-state write ---"
D2=$(new_fixture)
add_suite "$D2" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
echo hi >> "$FEED"
'
run_check "$D2"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "tests/test_foo.sh:3 touches live-state path '\$REPO_ROOT/.autonomous-team/agent-feed.jsonl'"; then
  pass "unlisted write: fails and names file:line:literal"
else
  fail "unlisted write: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D2"

# ── Test 3: same suite, now allowlisted — passes ───────────────────────────
echo ""
echo "--- Test 3: allowlisted touch passes ---"
D3=$(new_fixture)
add_suite "$D3" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
echo hi >> "$FEED"
'
write_allowlist "$D3" \
  "tests/test_foo.sh:\$REPO_ROOT/.autonomous-team/agent-feed.jsonl:synthetic fixture literal for this lint's own test suite."
run_check "$D3"
if [[ "$RC" -eq 0 ]]; then
  pass "allowlisted touch: exits 0"
else
  fail "allowlisted touch: expected exit 0, got rc=$RC out=$OUT"
fi
rm -rf "$D3"

# ── Test 4: a suite that never references the checked-out tree is clean ──
echo ""
echo "--- Test 4: scratch-rooted suite is not flagged ---"
D4=$(new_fixture)
add_suite "$D4" "test_foo.sh" '#!/usr/bin/env bash
FIXTURE_ROOT=$(mktemp -d)
FEED="$FIXTURE_ROOT/.autonomous-team/agent-feed.jsonl"
mkdir -p "$(dirname "$FEED")"
echo hi >> "$FEED"
rm -rf "$FIXTURE_ROOT"
'
run_check "$D4"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-or-assert-position matches"; then
  pass "scratch-rooted suite: not flagged, no allowlist entry required"
else
  fail "scratch-rooted suite: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D4"

# ── Test 5: JSON test-payload literal is not write-or-assert position ─────
echo ""
echo "--- Test 5: JSON test-payload literal is not flagged ---"
D5=$(new_fixture)
add_suite "$D5" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAYLOAD="{\"path\": \"$REPO_ROOT/.autonomous-team/hook-events/x\"}"
echo "$PAYLOAD"
'
run_check "$D5"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-or-assert-position matches"; then
  pass "JSON payload literal: not flagged, no allowlist entry required"
else
  fail "JSON payload literal: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D5"

# ── Test 6: a $(date ...)-suffixed literal is STILL flagged ───────────────
echo ""
echo "--- Test 6: \$(date)-suffixed live path is still flagged ---"
D6=$(new_fixture)
add_suite "$D6" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BLOCKS_FILE="$REPO_ROOT/.autonomous-team/hook-events/blocks-$(date +%F).jsonl"
echo hi >> "$BLOCKS_FILE"
'
run_check "$D6"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF ".autonomous-team/hook-events/blocks-"; then
  pass "\$(date)-suffixed literal: still flagged (dynamic suffix is not treated as safe)"
else
  fail "\$(date)-suffixed literal: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D6"

# ── Test 7: reintroducing a live-state touch flips a clean tree to red ────
echo ""
echo "--- Test 7: reintroducing a live touch flips clean tree to red ---"
D7=$(new_fixture)
add_suite "$D7" "test_foo.sh" '#!/usr/bin/env bash
FIXTURE_ROOT=$(mktemp -d)
rm -rf "$FIXTURE_ROOT"
'
run_check "$D7"
BEFORE_RC=$RC
add_suite "$D7" "test_foo.sh" '#!/usr/bin/env bash
FIXTURE_ROOT=$(mktemp -d)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REINTRODUCED="$REPO_ROOT/.autonomous-team/stats/reintroduced.jsonl"
echo probe >> "$REINTRODUCED"
rm -rf "$FIXTURE_ROOT"
'
run_check "$D7"
if [[ "$BEFORE_RC" -eq 0 ]] && [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF ".autonomous-team/stats/reintroduced.jsonl"; then
  pass "reintroduced live touch: clean tree (rc=0) flips to red (rc=$RC), names the path"
else
  fail "reintroduced live touch: expected 0 -> nonzero flip naming the path, got before=$BEFORE_RC after=$RC out=$OUT"
fi
rm -rf "$D7"

# ── Test 8: stale allowlist entry (literal no longer present) — FAILS ─────
echo ""
echo "--- Test 8: stale allowlist entry ---"
D8=$(new_fixture)
add_suite "$D8" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
echo hi >> "$FEED"
'
write_allowlist "$D8" \
  "tests/test_foo.sh:\$REPO_ROOT/.autonomous-team/agent-feed.jsonl:live entry, still matched." \
  "tests/test_foo.sh:\$REPO_ROOT/.autonomous-team/no-longer-here.jsonl:PROBE — this literal does not exist in the fixture."
run_check "$D8"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "stale allowlist entry 'tests/test_foo.sh:\$REPO_ROOT/.autonomous-team/no-longer-here.jsonl'"; then
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
  "tests/test_never_existed.sh:\$REPO_ROOT/.autonomous-team/agent-feed.jsonl:PROBE — this file was never created in the fixture."
run_check "$D9"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "dangling allowlist entry 'tests/test_never_existed.sh:\$REPO_ROOT/.autonomous-team/agent-feed.jsonl'"; then
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
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
echo hi >> "$FEED"
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
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
echo hi >> "$FEED"
'
write_allowlist "$D11" \
  "tests/test_foo.sh:\$REPO_ROOT/.autonomous-team/agent-feed.jsonl:parked for later cleanup."
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
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
echo hi >> "$FEED"
'
write_allowlist "$D12" \
  "tests/test_foo.sh:\$REPO_ROOT/.autonomous-team/agent-feed.jsonl:first entry." \
  "tests/test_foo.sh:\$REPO_ROOT/.autonomous-team/agent-feed.jsonl:duplicate of the entry above."
run_check "$D12"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "duplicate allowlist entry"; then
  pass "duplicate entry: fails and names it duplicate"
else
  fail "duplicate entry: expected a duplicate-entry failure, got rc=$RC out=$OUT"
fi
rm -rf "$D12"

# ── Test 13: no allowlist file at all — hard FAIL, not a silent pass ──────
echo ""
echo "--- Test 13: missing allowlist file ---"
D13=$(new_fixture)
rm -f "$D13/$ALLOWLIST_REL"
git -C "$D13" -c user.email=t@t.com -c user.name=Tester add -A
run_check "$D13"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -q "allowlist file .* not found"; then
  pass "missing allowlist: fails loudly, not a silent pass"
else
  fail "missing allowlist: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D13"

# ── Test 14: D#2283's blackboard/audit.jsonl subpaths are excluded
# structurally, never via allowlist ─────────────────────────────────────────
echo ""
echo "--- Test 14: blackboard/ and audit.jsonl are structurally excluded ---"
D14=$(new_fixture)
add_suite "$D14" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUDIT_LOG="$REPO_ROOT/.autonomous-team/audit.jsonl"
PR_STATE="$REPO_ROOT/.autonomous-team/blackboard/pr_state/42.json"
wc -l < "$AUDIT_LOG"
cat "$PR_STATE"
'
run_check "$D14"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-or-assert-position matches"; then
  pass "blackboard/audit.jsonl: excluded structurally, no allowlist entry needed"
else
  fail "blackboard/audit.jsonl: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D14"

# ── Test 15: a root variable reassigned to a fixture path is not flagged ──
echo ""
echo "--- Test 15: shadowed root variable is not flagged ---"
D15=$(new_fixture)
add_suite "$D15" "test_foo.sh" '#!/usr/bin/env bash
MAIN_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_ROOT=$(mktemp -d)
MAIN_REPO_ROOT="$FIXTURE_ROOT"
BLOCKS_FILE="$MAIN_REPO_ROOT/.autonomous-team/hook-events/blocks-x.jsonl"
mkdir -p "$(dirname "$BLOCKS_FILE")"
echo hi >> "$BLOCKS_FILE"
rm -rf "$FIXTURE_ROOT"
'
run_check "$D15"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-or-assert-position matches"; then
  pass "shadowed root var: not flagged once reassigned to a recognizable fixture root"
else
  fail "shadowed root var: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D15"

# ── Test 16: the SAME variable name, used live BEFORE being shadowed, IS
# flagged for the live use ──────────────────────────────────────────────────
echo ""
echo "--- Test 16: live use before shadowing is still flagged ---"
D16=$(new_fixture)
add_suite "$D16" "test_foo.sh" '#!/usr/bin/env bash
MAIN_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_TOUCH="$MAIN_REPO_ROOT/.autonomous-team/stats/premature.jsonl"
echo hi >> "$LIVE_TOUCH"
FIXTURE_ROOT=$(mktemp -d)
MAIN_REPO_ROOT="$FIXTURE_ROOT"
rm -rf "$FIXTURE_ROOT"
'
run_check "$D16"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF ".autonomous-team/stats/premature.jsonl"; then
  pass "live use before shadowing: flagged (shadowing only protects USES AFTER the reassignment)"
else
  fail "live use before shadowing: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D16"

# ── Test 17: an indirect but still-live derivation (via an intermediate
# SCRIPT_DIR variable, mentioning neither dirname nor BASH_SOURCE on the
# REPO_ROOT= line itself) is still flagged, not accidentally shadowed ─────
echo ""
echo "--- Test 17: indirect live derivation is still flagged ---"
D17=$(new_fixture)
add_suite "$D17" "test_foo.sh" '#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIVE_TOUCH="$REPO_ROOT/.autonomous-team/state-symlinks.json"
cat "$LIVE_TOUCH"
'
run_check "$D17"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF ".autonomous-team/state-symlinks.json"; then
  pass "indirect live derivation: still flagged, not mistaken for a shadow"
else
  fail "indirect live derivation: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D17"

# ── Test 18 (REGRESSION — D#2267 PR #2320 code review): a comment
# containing "live-state guard" (or any other comment) must NEVER exempt a
# real WRITE to the live tree. An earlier version of this script had a
# comment-marker exemption that was checked before, not as part of, the
# write/assert classification, so it silenced a genuine `>>` append as long
# as a "live-state guard"-labelled comment sat nearby — exactly the defect
# this lint exists to catch, made invisible by a comment. This test asserts
# the CURRENT behaviour directly and must keep failing if that mechanism,
# or anything shaped like it, is ever reintroduced. ─────────────────────────
echo ""
echo "--- Test 18: a marked WRITE is still flagged, never exempted by a comment ---"
D18=$(new_fixture)
add_suite "$D18" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# live-state guard
FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
echo hi >> "$FEED"
'
run_check "$D18"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF ".autonomous-team/agent-feed.jsonl"; then
  pass "marked write: still flagged — no comment exempts a real write"
else
  fail "marked write: expected a named failure regardless of the comment, got rc=$RC out=$OUT"
fi
rm -rf "$D18"

# ── Test 19: a deliberate, READ-only reference to a live path is not
# silently exempt by any mechanism — it must be allowlisted like any other
# match, same as the real AC8 case in
# scripts/fixtures/allowed_live_state_literals.txt ─────────────────────────
echo ""
echo "--- Test 19: a deliberate read still requires an explicit allowlist entry ---"
D19=$(new_fixture)
add_suite "$D19" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# live-state guard setup -- capture the real feed count before running, so
# we can prove afterward that this suite did not write to it.
REAL_FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
'
run_check "$D19"
UNLISTED_RC=$RC
write_allowlist "$D19" \
  "tests/test_foo.sh:\$REPO_ROOT/.autonomous-team/agent-feed.jsonl:deliberate read, disclosed via allowlist entry, not a comment marker."
run_check "$D19"
if [[ "$UNLISTED_RC" -ne 0 ]] && [[ "$RC" -eq 0 ]]; then
  pass "deliberate read: flagged until explicitly allowlisted, then passes — no comment shortcut"
else
  fail "deliberate read: expected unlisted-fail then allowlisted-pass, got unlisted_rc=$UNLISTED_RC allowlisted_rc=$RC out=$OUT"
fi
rm -rf "$D19"

# ── Test 20: a live path referenced only inside a multi-line quoted block
# variable (embedded script DATA, evaluated elsewhere with a different
# environment) is not flagged ───────────────────────────────────────────────
echo ""
echo "--- Test 20: embedded quoted-block script text is not flagged ---"
D20=$(new_fixture)
add_suite "$D20" "test_foo.sh" '#!/usr/bin/env bash
BLOCK='"'"'
_dir="${REPO_ROOT}/.autonomous-team/hook-events"
mkdir -p "$_dir"
'"'"'
REPO_ROOT="/some/scratch/dir" bash -c "$BLOCK"
'
run_check "$D20"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-or-assert-position matches"; then
  pass "embedded quoted-block text: not flagged, evaluated elsewhere with a different REPO_ROOT"
else
  fail "embedded quoted-block text: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D20"

# ── Test 21: a live path on the block's OPENING line itself (not inside
# the body) is still flagged normally ──────────────────────────────────────
echo ""
echo "--- Test 21: a live path on the block-open line itself is still flagged ---"
D21=$(new_fixture)
add_suite "$D21" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cat > "$REPO_ROOT/.autonomous-team/stats/opened-on-this-line.jsonl" <<EOF
some content
EOF
'
run_check "$D21"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF ".autonomous-team/stats/opened-on-this-line.jsonl"; then
  pass "live path on block-open line: still flagged (only the BODY is exempt)"
else
  fail "live path on block-open line: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D21"

# ── Test 22: a live path referenced only in a full-line comment
# (documenting what the code UNDER TEST does, not this suite) is not
# flagged ───────────────────────────────────────────────────────────────────
echo ""
echo "--- Test 22: full-line comment referencing a live path is not flagged ---"
D22=$(new_fixture)
add_suite "$D22" "test_foo.sh" '#!/usr/bin/env bash
# The hook computes: STATS_FILE="$REPO_ROOT/.autonomous-team/stats/x.jsonl"
echo "just documentation above, nothing executed"
'
run_check "$D22"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 write-or-assert-position matches"; then
  pass "full-line comment: not flagged, documents the code under test rather than this suite"
else
  fail "full-line comment: expected exit 0 / 0 matches, got rc=$RC out=$OUT"
fi
rm -rf "$D22"

# ── Test 23: a direct [[ == ]] comparison against a live path (assert
# position, not write position) is flagged ─────────────────────────────────
echo ""
echo "--- Test 23: [[ == ]] comparison against a live path is flagged ---"
D23=$(new_fixture)
add_suite "$D23" "test_foo.sh" '#!/usr/bin/env bash
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANDIDATE="/some/other/thing"
if [[ "$CANDIDATE" == "$REPO_ROOT/.autonomous-team/loop-metrics.jsonl" ]]; then
  echo match
fi
'
run_check "$D23"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF ".autonomous-team/loop-metrics.jsonl"; then
  pass "[[ == ]] comparison: flagged as an assert-position touch"
else
  fail "[[ == ]] comparison: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D23"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
