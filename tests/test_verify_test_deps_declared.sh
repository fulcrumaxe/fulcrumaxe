#!/usr/bin/env bash
# tests/test_verify_test_deps_declared.sh — hermetic tests for
# scripts/ci/verify-test-deps-declared.py's archive/ exclusion (D#2491).
#
# D#2464 shipped this guard's PYTEST_TIMEOUT fix but deliberately left the
# archive/ question undecided; D#2491 settled it (archive/ is OUT of scope —
# it is not part of the live dependency boundary this guard describes) and
# this file pins that decision with a real mutation check, not just an
# assertion of the current behaviour.
#
# Modelled on tests/test_no_hardcoded_checkout_paths_guard.sh — every fixture
# is a small synthetic tree built under mktemp, never the real repo, and the
# mutation test copies the REAL check into a mutant tmpfile, removes exactly
# the archive-exclusion block with one targeted `sed`, and confirms exactly
# that assertion flips (not some unrelated one). Because the real check
# derives REPO_ROOT from `Path(__file__).resolve().parent.parent.parent`,
# each fixture places its copy of the check at the same relative depth
# (<fixture>/scripts/ci/verify-test-deps-declared.py) so the scan root
# resolves to the fixture root, never the real repo or /tmp at large.
#
# Run: bash tests/test_verify_test_deps_declared.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SRC="$REPO_ROOT/scripts/ci/verify-test-deps-declared.py"
CHECK_REL="scripts/ci/verify-test-deps-declared.py"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# new_fixture — an empty synthetic tree with a copy of the real check
# installed at the relative depth it expects (scripts/ci/<name>), a
# requirements.txt that declares pytest with a lower bound (so check 1
# always passes and every fixture below isolates check 2, the marker scan),
# and one in-scope file using a pytest BUILTIN marker — so the "zero
# `pytest.mark.*` uses found" guard (a scan that finds nothing is a broken
# scan, not agreement) never fires just because the file under test here is
# the only other marker user in the fixture.
new_fixture() {
  local dir
  dir="$(mktemp -d)"
  mkdir -p "$dir/$(dirname "$CHECK_REL")" "$dir/tests"
  cp "$CHECK_SRC" "$dir/$CHECK_REL"
  cat >"$dir/requirements.txt" <<'EOF'
pytest>=7.0
EOF
  cat >"$dir/tests/test_sample.py" <<'EOF'
import pytest


@pytest.mark.skip(reason="builtin marker, always resolves")
def test_noop():
    pass
EOF
  printf '%s\n' "$dir"
}

# plant_unresolved_marker <dir> <rel-path> — writes a .py file at <rel-path>
# (inside the fixture) that uses a pytest marker with no providing
# distribution declared and no pytest.ini registration: unresolved unless
# the file's directory is excluded from the scan entirely.
plant_unresolved_marker() {
  local dir="$1" rel="$2"
  mkdir -p "$dir/$(dirname "$rel")"
  cat >"$dir/$rel" <<'EOF'
import pytest

pytestmark = pytest.mark.undeclaredthirdparty
EOF
}

run_check() {
  # run_check <fixture-dir> [check-path]
  local dir="$1" check="${2:-}"
  [ -z "$check" ] && check="$dir/$CHECK_REL"
  OUT="$(python3 "$check" "$dir/requirements.txt" 2>&1)"
  RC=$?
}

echo "== verify-test-deps-declared.py: archive/ exclusion =="

# --- 1. exclusion present, file under archive/: must NOT be flagged -------
d1="$(new_fixture)"
plant_unresolved_marker "$d1" "archive/oldpkg-2026-01-01/mod.py"
run_check "$d1"
if [[ "$RC" -eq 0 ]]; then
  pass "archive/-prefixed file with an unresolved marker does not fail the guard"
else
  fail "archive/-prefixed file with an unresolved marker should not fail the guard (rc=$RC): $OUT"
fi
if ! grep -q "archive/" <<<"$OUT"; then
  pass "guard output does not name any archive/ path"
else
  fail "guard output unexpectedly names an archive/ path: $OUT"
fi
rm -rf "$d1"

# --- 2. mutation check: remove the exclusion, same fixture must go RED ----
# The mutant is written back into the FIXTURE's own copy at the fixture's
# scripts/ci/ path, not a flat tmpfile — the real check derives its scan
# root from `Path(__file__).resolve().parent.parent.parent`, so a mutant
# living anywhere else would scan the wrong tree entirely (and, pointed at
# something as broad as /tmp, hang).
d2="$(new_fixture)"
plant_unresolved_marker "$d2" "archive/oldpkg-2026-01-01/mod.py"
mutant_check="$d2/$CHECK_REL"
sed -i '/if rel_path\.parts and rel_path\.parts\[0\] == ARCHIVE_DIR_NAME:/,+2d' \
  "$mutant_check"
if diff -q "$CHECK_SRC" "$mutant_check" >/dev/null; then
  fail "mutation did not change the check — sed pattern no longer matches the source (guard drifted, update this test)"
else
  pass "mutation removed the archive-exclusion block from a copy of the real check"
fi
run_check "$d2"
if [[ "$RC" -ne 0 ]] && grep -q "undeclaredthirdparty" <<<"$OUT" && grep -q "archive/oldpkg-2026-01-01/mod.py" <<<"$OUT"; then
  pass "with the exclusion removed, the same archive/ file is now flagged (exit $RC)"
else
  fail "removing the exclusion should flag archive/oldpkg-2026-01-01/mod.py (rc=$RC): $OUT"
fi
rm -rf "$d2"

# --- 3. same file OUTSIDE archive/, exclusion present: must be flagged ----
d3="$(new_fixture)"
plant_unresolved_marker "$d3" "libx/mod.py"
run_check "$d3"
if [[ "$RC" -ne 0 ]] && grep -q "undeclaredthirdparty" <<<"$OUT" && grep -q "libx/mod.py" <<<"$OUT"; then
  pass "the same unresolved-marker file outside archive/ is still flagged (exit $RC)"
else
  fail "a file outside archive/ with an unresolved marker should be flagged (rc=$RC): $OUT"
fi
rm -rf "$d3"

# --- 4. existing negative case is undisturbed: real repo, real fixture ----
NO_PYTEST_TXT="$REPO_ROOT/tests/fixtures/requirements/no-pytest.txt"
if [[ ! -f "$NO_PYTEST_TXT" ]]; then
  fail "missing fixture: $NO_PYTEST_TXT"
else
  OUT="$(python3 "$CHECK_SRC" "$NO_PYTEST_TXT" 2>&1)"
  RC=$?
  if [[ "$RC" -ne 0 ]]; then
    pass "no-pytest.txt (pytest undeclared) still fails the guard (exit $RC)"
  else
    fail "no-pytest.txt should still fail the guard, got exit $RC: $OUT"
  fi
  if grep -q pytest "$NO_PYTEST_TXT"; then
    pass "contrast holds: a naive 'grep -q pytest' on the same file exits 0 while the guard exits non-zero"
  else
    fail "the no-pytest.txt fixture no longer contains the substring 'pytest' — the grep contrast this guard exists to beat is gone"
  fi
fi

# --- 5. real tree, real check: exit 0 unless the tree itself regressed ----
OUT="$(python3 "$CHECK_SRC" "$REPO_ROOT/requirements.txt" 2>&1)"
RC=$?
if [[ "$RC" -eq 0 ]]; then
  pass "guard exits 0 against this repo's real requirements.txt"
else
  fail "guard should exit 0 against the real tree (rc=$RC): $OUT"
fi
if grep -q "archive/" <<<"$OUT"; then
  fail "guard output names an archive/ path when run against the real tree: $OUT"
else
  pass "guard output names no archive/ path against the real tree"
fi

echo
echo "== summary: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
