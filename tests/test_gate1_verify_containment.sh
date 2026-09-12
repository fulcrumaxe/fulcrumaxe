#!/usr/bin/env bash
# tests/test_gate1_verify_containment.sh
#
# Unit tests for scripts/gate1-verify-containment.sh (D#2560 PR-A, security
# review fix). Fixture-based: no real credential/network/filesystem denial
# is required to exercise any of these paths.
#
# Filed after security review found the original two-outcome design could
# print gate1_containment_verdict=CONTAINED at the same uid, with full
# credential/network/filesystem access, simply by making `gh`/`curl`
# unreachable and pointing the state-dir/checkout-dir targets somewhere
# that doesn't exist — "the probe never ran" was indistinguishable from
# "the probe ran and found it denied". These tests prove the fix: a probe
# whose precondition fails reports INDETERMINATE, and INDETERMINATE can
# never aggregate into CONTAINED.
#
# Covers:
#   1. Baseline: all four probes NOT-DENIED, verdict UNCONTAINED (today's
#      real reading, same-uid, everything reachable).
#   2. Missing gh/curl (tools absent from PATH, same uid, nothing actually
#      denied) -> INDETERMINATE, never NOT-DENIED and never DENIED, and the
#      overall verdict is INDETERMINATE, not CONTAINED. This is the exact
#      attack the security reviewer reproduced against the old script.
#   3. GATE1_VERIFY_STATE_DIR pointing at a path that doesn't exist ->
#      INDETERMINATE for that probe alone, verdict INDETERMINATE.
#   4. GATE1_VERIFY_CHECKOUT_DIR pointing at a directory with no .git ->
#      INDETERMINATE for that probe alone (this is also exactly what used
#      to happen for real inside any worktree, since a worktree's .git is a
#      pointer file, not a directory).
#   5. A genuine four-way denial (tools present but failing, targets
#      present but unreadable/unwritable) still produces DENIED (not
#      INDETERMINATE) and the verdict CONTAINED — proving INDETERMINATE and
#      DENIED are not the same thing collapsed back together, and that
#      CONTAINED is still reachable when every probe actually ran and
#      actually failed.
#
# Usage: bash tests/test_gate1_verify_containment.sh — exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERIFIER_SRC="$REPO_ROOT/scripts/gate1-verify-containment.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

if [ ! -f "$VERIFIER_SRC" ]; then
  fail "setup" "scripts/gate1-verify-containment.sh not found at $VERIFIER_SRC"
  echo ""
  echo "Results: $PASS passed, $FAIL failed"
  exit 1
fi

FIXTURE=$(mktemp -d)

# ── test 1: baseline — real run, real credentials, real network ──────────

OUT_1=$(bash "$VERIFIER_SRC" 2>&1)

if echo "$OUT_1" | grep -q "gate1_containment_verdict=UNCONTAINED"; then
  pass "baseline-verdict-uncontained"
else
  fail "baseline-verdict-uncontained" "expected UNCONTAINED on this host, got: $OUT_1"
fi

if echo "$OUT_1" | grep -qc "NOT-DENIED"; then
  pass "baseline-has-not-denied-probes"
else
  fail "baseline-has-not-denied-probes" "expected at least one NOT-DENIED line, got: $OUT_1"
fi

# ── test 2: missing gh/curl, same uid, nothing actually denied ────────────
# Build a PATH with bash/coreutils/git present but no directory that
# contains gh or curl, by filtering the current PATH down to entries that
# do NOT resolve either binary.

FIXTURE_BIN_LIST=""
IFS=':' read -ra _PATH_DIRS <<< "$PATH"
for d in "${_PATH_DIRS[@]}"; do
  [ -x "$d/gh" ] && continue
  [ -x "$d/curl" ] && continue
  FIXTURE_BIN_LIST="${FIXTURE_BIN_LIST:+$FIXTURE_BIN_LIST:}$d"
done

OUT_2=$(PATH="$FIXTURE_BIN_LIST" bash "$VERIFIER_SRC" 2>&1)

if echo "$OUT_2" | grep -q "gate1_probe gh-credential=INDETERMINATE"; then
  pass "missing-gh-is-indeterminate"
else
  fail "missing-gh-is-indeterminate" "expected gh-credential=INDETERMINATE, got: $OUT_2"
fi

if echo "$OUT_2" | grep -q "gate1_probe network=INDETERMINATE"; then
  pass "missing-curl-is-indeterminate"
else
  fail "missing-curl-is-indeterminate" "expected network=INDETERMINATE, got: $OUT_2"
fi

if echo "$OUT_2" | grep -q "gate1_containment_verdict=INDETERMINATE"; then
  pass "missing-tools-verdict-is-indeterminate-not-contained"
else
  fail "missing-tools-verdict-is-indeterminate-not-contained" "expected INDETERMINATE verdict (this is the exact bug the security review found — a missing tool must never read as CONTAINED), got: $OUT_2"
fi

if echo "$OUT_2" | grep -q "gate1_containment_verdict=CONTAINED"; then
  fail "missing-tools-never-contained" "verdict printed CONTAINED with gh/curl merely absent — the exact regression"
else
  pass "missing-tools-never-contained"
fi

# ── test 3: state-dir target doesn't exist ─────────────────────────────────

OUT_3=$(GATE1_VERIFY_STATE_DIR="$FIXTURE/does-not-exist" bash "$VERIFIER_SRC" 2>&1)

if echo "$OUT_3" | grep -q "gate1_probe state-dir=INDETERMINATE"; then
  pass "absent-state-dir-is-indeterminate"
else
  fail "absent-state-dir-is-indeterminate" "expected state-dir=INDETERMINATE, got: $OUT_3"
fi

if echo "$OUT_3" | grep -q "gate1_probe state-dir=DENIED"; then
  fail "absent-state-dir-never-denied" "an absent target reported DENIED instead of INDETERMINATE — this is the false-DENIED bug (D#2560 review): under PR-B's step 5, HOME moves and this exact path stops existing while the real state dir stays reachable"
else
  pass "absent-state-dir-never-denied"
fi

if echo "$OUT_3" | grep -q "gate1_containment_verdict=INDETERMINATE"; then
  pass "absent-state-dir-verdict-indeterminate"
else
  fail "absent-state-dir-verdict-indeterminate" "expected overall verdict INDETERMINATE, got: $OUT_3"
fi

# ── test 4: checkout-dir target has no .git (the worktree shape) ──────────

NO_GIT_DIR="$FIXTURE/no-git-here"
mkdir -p "$NO_GIT_DIR"

OUT_4=$(GATE1_VERIFY_CHECKOUT_DIR="$NO_GIT_DIR" bash "$VERIFIER_SRC" 2>&1)

if echo "$OUT_4" | grep -q "gate1_probe operator-checkout-write=INDETERMINATE"; then
  pass "no-git-checkout-is-indeterminate"
else
  fail "no-git-checkout-is-indeterminate" "expected operator-checkout-write=INDETERMINATE, got: $OUT_4"
fi

if echo "$OUT_4" | grep -q "gate1_probe operator-checkout-write=DENIED"; then
  fail "no-git-checkout-never-denied" "a target with no .git directory reported DENIED instead of INDETERMINATE — this is exactly the bug that made the original script report false DENIED inside any worktree"
else
  pass "no-git-checkout-never-denied"
fi

# ── test 5: a genuine four-way denial still reaches CONTAINED ─────────────
# Tools present (so `command -v` succeeds) but each one fails; targets
# present but unreadable/unwritable. Proves DENIED and INDETERMINATE are
# not the same bucket, and that CONTAINED is still reachable when every
# probe genuinely ran and genuinely failed.

DENY_BIN="$FIXTURE/deny-bin"
mkdir -p "$DENY_BIN"
cat > "$DENY_BIN/gh" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$DENY_BIN/gh"
cat > "$DENY_BIN/curl" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$DENY_BIN/curl"

DENY_STATE_DIR="$FIXTURE/deny-state"
mkdir -p "$DENY_STATE_DIR"
chmod 000 "$DENY_STATE_DIR"

DENY_CHECKOUT_DIR="$FIXTURE/deny-checkout"
mkdir -p "$DENY_CHECKOUT_DIR/.git"
chmod 000 "$DENY_CHECKOUT_DIR/.git"

OUT_5=$(PATH="$DENY_BIN:$PATH" \
  GATE1_VERIFY_STATE_DIR="$DENY_STATE_DIR" \
  GATE1_VERIFY_CHECKOUT_DIR="$DENY_CHECKOUT_DIR" \
  bash "$VERIFIER_SRC" 2>&1)

chmod 755 "$DENY_STATE_DIR" "$DENY_CHECKOUT_DIR/.git"

if echo "$OUT_5" | grep -qc "INDETERMINATE"; then
  fail "genuine-denial-has-no-indeterminate" "expected zero INDETERMINATE lines when every probe's precondition is met, got: $OUT_5"
else
  pass "genuine-denial-has-no-indeterminate"
fi

if echo "$OUT_5" | grep -q "gate1_containment_verdict=CONTAINED"; then
  pass "genuine-four-way-denial-reaches-contained"
else
  fail "genuine-four-way-denial-reaches-contained" "expected CONTAINED when gh/curl fail and both dirs are unreadable, got: $OUT_5"
fi

# ── Teardown ─────────────────────────────────────────────────────────────

chmod -R u+rwx "$FIXTURE" 2>/dev/null || true
rm -rf "$FIXTURE"

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
exit 0
