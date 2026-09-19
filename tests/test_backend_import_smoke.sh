#!/usr/bin/env bash
# tests/test_backend_import_smoke.sh — fixture tests for
# scripts/ci/backend-import-smoke.py's scripts/lib/*.py subject set (D#2588).
#
# Run: bash tests/test_backend_import_smoke.sh
#
# Each fixture is a throwaway tree under mktemp -d holding its own
# scripts/ci/backend-import-smoke.py (a copy of the real script). The real
# script resolves REPO_ROOT from its own __file__ location
# (Path(__file__).resolve().parent.parent.parent), so copying it three
# directories deep into a fixture makes it treat that fixture as the repo
# root — no env var or monkeypatch needed to retarget it.
#
# Covers Spec items 2 (hyphenated filenames, path-based import), 3 (a
# module-level raise turns the check red), 5 (an empty scripts/lib subject
# set is not a failure) and 6 (no new dependency, no state-dir write).
# Item 1 (clean code-plane main exits 0 with a higher count) and item 4
# (a removed backend symbol) are one-time measurements against the real
# tree, recorded in the PR body — see its Verification section.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/ci/backend-import-smoke.py"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_exit() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" -eq "$expected" ]; then
    pass "$label (exit $actual)"
  else
    fail "$label (expected exit $expected, got $actual)"
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if printf '%s' "$haystack" | grep -qF "$needle"; then
    pass "$label"
  else
    fail "$label — expected output to contain: $needle"
  fi
}

# Build a fresh fixture tree with scripts/ci/backend-import-smoke.py copied
# in at the right depth, and empty backend/ + scripts/lib/ dirs. Prints the
# fixture root on stdout.
new_fixture() {
  local dir
  dir="$(mktemp -d)"
  mkdir -p "$dir/scripts/ci" "$dir/backend" "$dir/scripts/lib"
  cp "$SCRIPT" "$dir/scripts/ci/backend-import-smoke.py"
  printf '%s\n' "$dir"
}

FIXTURES_TO_CLEAN=()
cleanup() {
  local d
  for d in "${FIXTURES_TO_CLEAN[@]:-}"; do
    [ -n "$d" ] && rm -rf "$d"
  done
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
echo "-- item 2: a hyphenated scripts/lib filename is imported by file path --"
F2="$(new_fixture)"
FIXTURES_TO_CLEAN+=("$F2")
cat > "$F2/scripts/lib/cross-file-fixture.py" <<'EOF'
VALUE = 1
EOF
OUT2=$(python3 "$F2/scripts/ci/backend-import-smoke.py" 2>&1)
RC2=$?
assert_exit "hyphenated filename: script exits 0" 0 "$RC2"
assert_contains "hyphenated filename: counted in the scripts/lib subject set" "scripts/lib/*.py: 1 modules" "$OUT2"
assert_contains "hyphenated filename: no FAIL recorded for it" "backend import-smoke: all clear" "$OUT2"

# Watched negative half of item 2: the literal `import` statement (which a
# naive dotted-name implementation would have to execute, directly or via
# exec() of a built-up string) cannot even parse this name — the file-path
# importer used above never goes through that syntax at all.
DOTTED_OUT=$(cd "$F2" && python3 -c "
try:
    exec('import scripts.lib.cross-file-fixture')
    print('IMPORTED')
except SyntaxError as e:
    print('SYNTAXERROR', e)
" 2>&1)
case "$DOTTED_OUT" in
  SYNTAXERROR*) pass "hyphenated filename: literal dotted import statement is a SyntaxError (the class item 2 guards against)" ;;
  *) fail "hyphenated filename: expected the dotted import statement to fail, got: $DOTTED_OUT" ;;
esac

# ---------------------------------------------------------------------------
echo ""
echo "-- item 3: a module-level raise in scripts/lib turns the check red --"
F3="$(new_fixture)"
FIXTURES_TO_CLEAN+=("$F3")
cat > "$F3/scripts/lib/broken.py" <<'EOF'
raise RuntimeError("smoke")
EOF
OUT3=$(python3 "$F3/scripts/ci/backend-import-smoke.py" 2>&1)
RC3=$?
assert_exit "module-level raise: script exits non-zero" 1 "$RC3"
assert_contains "module-level raise: names the failing file" "FAIL scripts/lib/broken.py" "$OUT3"
assert_contains "module-level raise: carries the exception text" "RuntimeError('smoke')" "$OUT3"

# ---------------------------------------------------------------------------
echo ""
echo "-- item 5: an empty scripts/lib subject set is not a failure --"
F5="$(new_fixture)"
FIXTURES_TO_CLEAN+=("$F5")
rm -rf "$F5/scripts/lib"
OUT5=$(python3 "$F5/scripts/ci/backend-import-smoke.py" 2>&1)
RC5=$?
assert_exit "absent scripts/lib: script exits 0" 0 "$RC5"
assert_contains "absent scripts/lib: says the subject set was empty" "scripts/lib/*.py: subject set was empty (0 modules)" "$OUT5"

# Same claim, one directory further: scripts/lib present but with zero
# *.py files in it (a fork that keeps the dir but never populates it).
F5B="$(new_fixture)"
FIXTURES_TO_CLEAN+=("$F5B")
echo "not python" > "$F5B/scripts/lib/README.md"
OUT5B=$(python3 "$F5B/scripts/ci/backend-import-smoke.py" 2>&1)
RC5B=$?
assert_exit "empty scripts/lib (no .py files): script exits 0" 0 "$RC5B"
assert_contains "empty scripts/lib (no .py files): says the subject set was empty" "scripts/lib/*.py: subject set was empty (0 modules)" "$OUT5B"

# ---------------------------------------------------------------------------
echo ""
echo "-- item 6: no new dependency, no state-dir write --"
F6="$(new_fixture)"
FIXTURES_TO_CLEAN+=("$F6")
cat > "$F6/scripts/lib/plain.py" <<'EOF'
import json  # stdlib only — this fixture asserts the smoke mechanism
             # itself adds no dependency and writes no state, independent
             # of what any particular scripts/lib module needs.

VALUE = json.dumps({"ok": True})
EOF
SCRATCH_STATE="$(mktemp -d)"
FIXTURES_TO_CLEAN+=("$SCRATCH_STATE")
OUT6=$(env -u AUTONOMOUS_TEAM_REPO AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE" python3 "$F6/scripts/ci/backend-import-smoke.py" 2>&1)
RC6=$?
assert_exit "no new dependency: script exits 0 with AUTONOMOUS_TEAM_REPO unset" 0 "$RC6"
STATE_FILES=$(find "$SCRATCH_STATE" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')
if [ "$STATE_FILES" -eq 0 ]; then
  pass "no state-dir write: scratch AUTONOMOUS_TEAM_STATE_DIR is empty afterwards"
else
  fail "no state-dir write: scratch AUTONOMOUS_TEAM_STATE_DIR has $STATE_FILES entr(y/ies) afterwards"
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
