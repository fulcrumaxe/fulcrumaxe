#!/usr/bin/env bash
# tests/test_shipped_dir_inventory.sh — regression test for
# open-source/checks/shipped-dir-inventory.sh (D#2275 rev 2).
#
# Runs the REAL shipped-dir-inventory.sh, unmodified, against a synthetic
# <export-dir> + MANIFEST.md built fresh per test case, same division of
# labor as tests/test_shipped_to_listed.sh.
#
# Usage: bash tests/test_shipped_dir_inventory.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$REPO_ROOT/open-source/checks/shipped-dir-inventory.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

make_case() {
  local body="$1"
  local root
  root="$(mktemp -d)"
  mkdir -p "$root/export"
  printf '%s\n' "$body" > "$root/MANIFEST.md"
  echo "$root/MANIFEST.md $root/export"
}

run_check() {
  local export_dir="$1" manifest="$2"
  bash "$CHECK_SCRIPT" "$export_dir" "$manifest"
}

echo "=== Every shipped directory is declared -- exits 0 ==="
read -r manifest export_dir < <(make_case '<!-- SHIPPED_DIRS_START -->
backend
backend/tests
<!-- SHIPPED_DIRS_END -->')
mkdir -p "$export_dir/backend/tests"
echo x > "$export_dir/backend/main.py"
echo x > "$export_dir/backend/tests/test_main.py"
echo x > "$export_dir/CLAUDE.md"
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  pass "fully-declared export exits 0"
else
  fail "fully-declared export should exit 0, got: $out"
fi
echo "$out" | grep -q "scanned 3 shipped file(s)" && pass "scope line reports 3 files scanned" || fail "scope line wrong: $out"
echo "$out" | grep -qF "CLAUDE.md" && fail "root-level file should not be named as a problem" || pass "root-level file is not checked against SHIPPED_DIRS at all"

echo
echo "=== Depth cap: a file three segments deep still resolves via its depth-2 prefix ==="
mkdir -p "$export_dir/backend/tests/fixtures"
echo x > "$export_dir/backend/tests/fixtures/deep.json"
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  pass "backend/tests/fixtures/deep.json resolves via the backend/tests entry"
else
  fail "depth-3 file should resolve via its depth-2 prefix, got: $out"
fi
rm -rf "$export_dir/backend/tests/fixtures"

echo
echo "=== Undeclared new directory -- exits 1, names it ==="
mkdir -p "$export_dir/backend/customer-data"
echo "pii-rows" > "$export_dir/backend/customer-data/rows.csv"
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  fail "an undeclared new directory should exit non-zero"
else
  pass "undeclared new directory exits non-zero"
fi
echo "$out" | grep -qF "backend/customer-data" && pass "FAIL line names the undeclared directory" || fail "directory not named: $out"
rm -rf "$export_dir/backend/customer-data"

echo
echo "=== Stale-declaration guard: a SHIPPED_DIRS entry matching nothing hard-fails ==="
read -r manifest2 export_dir2 < <(make_case '<!-- SHIPPED_DIRS_START -->
backend
nonexistent-dir
<!-- SHIPPED_DIRS_END -->')
mkdir -p "$export_dir2/backend"
echo x > "$export_dir2/backend/main.py"
if out=$(run_check "$export_dir2" "$manifest2" 2>&1); then
  fail "a stale SHIPPED_DIRS entry should hard-fail"
else
  pass "stale SHIPPED_DIRS entry hard-fails"
fi
echo "$out" | grep -qF "stale SHIPPED_DIRS entry 'nonexistent-dir'" && pass "FAIL line names the stale entry" || fail "stale entry not named: $out"

echo
echo "=== No-blanket guard: a wildcard SHIPPED_DIRS entry is rejected, not honoured ==="
read -r manifest3 export_dir3 < <(make_case '<!-- SHIPPED_DIRS_START -->
*
<!-- SHIPPED_DIRS_END -->')
mkdir -p "$export_dir3/anything"
echo x > "$export_dir3/anything/file.txt"
if out=$(run_check "$export_dir3" "$manifest3" 2>&1); then
  fail "a blanket '*' SHIPPED_DIRS entry should be rejected, not silently honoured"
else
  pass "blanket SHIPPED_DIRS entry rejected"
fi
echo "$out" | grep -qF "blanket entry" && pass "FAIL line names it as a blanket entry" || fail "blanket reason not stated: $out"

echo
echo "=== No SHIPPED_DIRS marker block at all is a hard error, not a silent pass ==="
root4="$(mktemp -d)"
mkdir -p "$root4/export"
echo "no marker blocks here" > "$root4/MANIFEST.md"
if out=$(run_check "$root4/export" "$root4/MANIFEST.md" 2>&1); then
  fail "a manifest with no SHIPPED_DIRS block should hard-fail, not pass"
else
  pass "missing SHIPPED_DIRS block hard-fails"
fi

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
