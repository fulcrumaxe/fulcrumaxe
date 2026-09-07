#!/usr/bin/env bash
# tests/test_state_dir_resolver_guard.sh — hermetic unit tests for
# scripts/ci/state-dir-resolver-guard.py (D#2183 Spec items 6-9).
#
# Modelled on tests/test_check_tests_live_state_paths.sh and
# tests/test_check_tests_fixed_tmp_paths.sh: every fixture is a small
# synthetic git repo built under mktemp -d, with a COPY of the real guard
# installed at the same relative path (scripts/ci/state-dir-resolver-guard.py)
# so its own `git ls-files` and REPO_ROOT resolution (__file__-derived) both
# work inside the fixture, never against the live repo tree.
#
# Run: bash tests/test_state_dir_resolver_guard.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD_SRC="$REPO_ROOT/scripts/ci/state-dir-resolver-guard.py"
GUARD_REL="scripts/ci/state-dir-resolver-guard.py"
ALLOWLIST_REL="scripts/fixtures/allowed_state_dir_resolvers.txt"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# new_fixture — empty synthetic repo with just the guard installed.
new_fixture() {
  local dir
  dir=$(mktemp -d)
  mkdir -p "$dir/scripts/fixtures" "$dir/scripts/ci" "$dir/backend"
  cp "$GUARD_SRC" "$dir/$GUARD_REL"
  : > "$dir/$ALLOWLIST_REL"
  git -C "$dir" init -q
  git -C "$dir" -c user.email=t@t.com -c user.name=Tester add -A
  git -C "$dir" -c user.email=t@t.com -c user.name=Tester commit -q -m init
  printf '%s\n' "$dir"
}

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

add_module() {
  local dir="$1" relpath="$2" content="$3"
  mkdir -p "$dir/$(dirname "$relpath")"
  printf '%s\n' "$content" > "$dir/$relpath"
  git -C "$dir" -c user.email=t@t.com -c user.name=Tester add -A
}

run_guard() {
  local dir="$1"
  OUT="$(cd "$dir" && python3 "$GUARD_REL" 2>&1)"
  RC=$?
}

echo "=== state-dir-resolver-guard.py hermetic tests ==="

# ── Test 1: empty fixture — passes with 0 allowlisted readers ─────────────
echo ""
echo "--- Test 1: empty tree ---"
D1=$(new_fixture)
run_guard "$D1"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 allowlisted reader"; then
  pass "empty tree: exits 0 with 0 allowlisted readers"
else
  fail "empty tree: expected exit 0 / 0 readers, got rc=$RC out=$OUT"
fi
rm -rf "$D1"

# ── Test 2: unlisted single-line read — FAILS, names file:line ────────────
echo ""
echo "--- Test 2: unlisted single-line read ---"
D2=$(new_fixture)
add_module "$D2" "backend/single.py" 'import os


def f():
    return os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")'
run_guard "$D2"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "backend/single.py: reads AUTONOMOUS_TEAM_STATE_DIR directly at line(s) 5"; then
  pass "unlisted single-line read: fails and names file:line"
else
  fail "unlisted single-line read: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D2"

# ── Test 3: unlisted MULTI-LINE read — FAILS too (the whole point of AST) ─
echo ""
echo "--- Test 3: unlisted multi-line read (line-oriented grep would miss this) ---"
D3=$(new_fixture)
add_module "$D3" "backend/multi.py" 'import os


def f():
    return os.environ.get(
        "AUTONOMOUS_TEAM_STATE_DIR",
        "/some/default",
    )'
run_guard "$D3"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "backend/multi.py: reads AUTONOMOUS_TEAM_STATE_DIR directly at line(s) 5"; then
  pass "unlisted multi-line read: still flagged (AST, not line-oriented)"
else
  fail "unlisted multi-line read: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D3"

# ── Test 4: same multi-line read, now allowlisted — passes ────────────────
echo ""
echo "--- Test 4: allowlisted read passes ---"
D4=$(new_fixture)
add_module "$D4" "backend/multi.py" 'import os


def f():
    return os.environ.get(
        "AUTONOMOUS_TEAM_STATE_DIR",
        "/some/default",
    )'
write_allowlist "$D4" \
  "backend/multi.py:AUTONOMOUS_TEAM_STATE_DIR:synthetic fixture, deliberately reads the variable for this test."
run_guard "$D4"
if [[ "$RC" -eq 0 ]]; then
  pass "allowlisted read: exits 0"
else
  fail "allowlisted read: expected exit 0, got rc=$RC out=$OUT"
fi
rm -rf "$D4"

# ── Test 5: read+write in the same function is a save/restore utility,
#            never flagged, allowlist or not ────────────────────────────
echo ""
echo "--- Test 5: save/restore override utility is not flagged ---"
D5=$(new_fixture)
add_module "$D5" "backend/env_scope.py" 'import os


class Scope:
    def __enter__(self):
        self._old = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = "/scratch"
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("AUTONOMOUS_TEAM_STATE_DIR", None)
        else:
            os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = self._old'
run_guard "$D5"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 allowlisted reader"; then
  pass "save/restore utility: not flagged, no allowlist entry required"
else
  fail "save/restore utility: expected exit 0 / 0 readers, got rc=$RC out=$OUT"
fi
rm -rf "$D5"

# ── Test 6: archive/ is excluded entirely ──────────────────────────────────
echo ""
echo "--- Test 6: archive/ read is not flagged ---"
D6=$(new_fixture)
add_module "$D6" "backend/archive/frozen.py" 'import os


def f():
    return os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")'
run_guard "$D6"
if [[ "$RC" -eq 0 ]] && echo "$OUT" | grep -q "0 allowlisted reader"; then
  pass "archive/ read: not flagged"
else
  fail "archive/ read: expected exit 0 / 0 readers, got rc=$RC out=$OUT"
fi
rm -rf "$D6"

# ── Test 7: stale allowlist entry (no matching read left) — FAILS ─────────
echo ""
echo "--- Test 7: stale allowlist entry fails ---"
D7=$(new_fixture)
add_module "$D7" "backend/fixed.py" 'import os


def f():
    from backend.state_paths import STATE_DIR
    return STATE_DIR'
write_allowlist "$D7" \
  "backend/fixed.py:AUTONOMOUS_TEAM_STATE_DIR:no longer reads the variable directly — stale on purpose for this test."
run_guard "$D7"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "stale allowlist entry 'backend/fixed.py:AUTONOMOUS_TEAM_STATE_DIR'"; then
  pass "stale allowlist entry: fails and names it"
else
  fail "stale allowlist entry: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D7"

# ── Test 8: dangling allowlist entry (path git doesn't track) — FAILS ─────
echo ""
echo "--- Test 8: dangling allowlist entry fails ---"
D8=$(new_fixture)
write_allowlist "$D8" \
  "backend/does_not_exist.py:AUTONOMOUS_TEAM_STATE_DIR:this file was never added to the fixture repo."
run_guard "$D8"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qF "dangling allowlist entry 'backend/does_not_exist.py:AUTONOMOUS_TEAM_STATE_DIR'"; then
  pass "dangling allowlist entry: fails and names it"
else
  fail "dangling allowlist entry: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D8"

# ── Test 9: banned reason — FAILS ──────────────────────────────────────────
echo ""
echo "--- Test 9: banned reason fails ---"
D9=$(new_fixture)
add_module "$D9" "backend/single.py" 'import os


def f():
    return os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")'
write_allowlist "$D9" \
  "backend/single.py:AUTONOMOUS_TEAM_STATE_DIR:TODO figure out why this needs it"
run_guard "$D9"
if [[ "$RC" -ne 0 ]] && echo "$OUT" | grep -qi "banned reason"; then
  pass "banned reason: fails"
else
  fail "banned reason: expected a named failure, got rc=$RC out=$OUT"
fi
rm -rf "$D9"

# ── Test 10: --allowlist override points at a different file ──────────────
echo ""
echo "--- Test 10: --allowlist override is honoured ---"
D10=$(new_fixture)
add_module "$D10" "backend/single.py" 'import os


def f():
    return os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")'
ALT="$D10/alt-allowlist.txt"
printf '%s\n' "backend/single.py:AUTONOMOUS_TEAM_STATE_DIR:override allowlist for this test." > "$ALT"
OUT="$(cd "$D10" && python3 "$GUARD_REL" --allowlist "$ALT" 2>&1)"
RC=$?
if [[ "$RC" -eq 0 ]]; then
  pass "--allowlist override: exits 0 against the override file"
else
  fail "--allowlist override: expected exit 0, got rc=$RC out=$OUT"
fi
rm -rf "$D10"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
