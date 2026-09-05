#!/usr/bin/env bash
# tests/test_shipped_extensions.sh — regression test for
# open-source/checks/shipped-extensions.sh (D#2275 rev 2).
#
# Runs the REAL shipped-extensions.sh, unmodified, against a synthetic
# <export-dir> + MANIFEST.md built fresh per test case, same division of
# labor as tests/test_shipped_to_listed.sh.
#
# Usage: bash tests/test_shipped_extensions.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$REPO_ROOT/open-source/checks/shipped-extensions.sh"

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

echo "=== Every extension declared, extensionless files exact-path declared -- exits 0 ==="
read -r manifest export_dir < <(make_case '<!-- EXTENSIONS_START -->
.py
.md
.gitignore
<!-- EXTENSIONS_END -->

<!-- EXTENSIONLESS_START -->
LICENSE
<!-- EXTENSIONLESS_END -->')
mkdir -p "$export_dir/backend"
echo x > "$export_dir/backend/main.py"
echo x > "$export_dir/README.md"
echo x > "$export_dir/.gitignore"
echo x > "$export_dir/LICENSE"
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  pass "fully-declared export exits 0"
else
  fail "fully-declared export should exit 0, got: $out"
fi
echo "$out" | grep -q "scanned 4 shipped file(s)" && pass "scope line reports 4 files scanned" || fail "scope line wrong: $out"

echo
echo "=== Undeclared extension -- exits 1, names the extension and the file ==="
echo x > "$export_dir/backend/dump.sql"
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  fail "an undeclared .sql extension should exit non-zero"
else
  pass "undeclared extension exits non-zero"
fi
echo "$out" | grep -qF "'.sql'" && pass "FAIL line names the extension" || fail "extension not named: $out"
echo "$out" | grep -qF "backend/dump.sql" && pass "FAIL line names the file" || fail "file not named: $out"
rm -f "$export_dir/backend/dump.sql"

echo
echo "=== Extensionless file NOT in EXTENSIONLESS -- exits 1 ==="
echo x > "$export_dir/NOTICE"
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  fail "an undeclared extensionless file should exit non-zero"
else
  pass "undeclared extensionless file exits non-zero"
fi
echo "$out" | grep -qF "NOTICE" && pass "FAIL line names the extensionless file" || fail "file not named: $out"
rm -f "$export_dir/NOTICE"

echo
echo "=== Dotfile convention: a second-level extension on a dotfile takes the LAST segment ==="
echo x > "$export_dir/.eslintrc.json"
read -r manifest2 export_dir2 < <(make_case '<!-- EXTENSIONS_START -->
.json
<!-- EXTENSIONS_END -->')
mkdir -p "$export_dir2"
echo x > "$export_dir2/.eslintrc.json"
if out=$(run_check "$export_dir2" "$manifest2" 2>&1); then
  pass ".eslintrc.json resolves against a declared .json entry (last-dot rule)"
else
  fail ".eslintrc.json should resolve via .json, got: $out"
fi

echo
echo "=== Stale-declaration guard: an EXTENSIONS entry matching nothing hard-fails ==="
read -r manifest3 export_dir3 < <(make_case '<!-- EXTENSIONS_START -->
.py
.rb
<!-- EXTENSIONS_END -->')
echo x > "$export_dir3/main.py"
if out=$(run_check "$export_dir3" "$manifest3" 2>&1); then
  fail "a stale EXTENSIONS entry should hard-fail"
else
  pass "stale EXTENSIONS entry hard-fails"
fi
echo "$out" | grep -qF "stale EXTENSIONS entry '.rb'" && pass "FAIL line names the stale entry" || fail "stale entry not named: $out"

echo
echo "=== Stale-declaration guard: an EXTENSIONLESS entry matching nothing hard-fails ==="
read -r manifest4 export_dir4 < <(make_case '<!-- EXTENSIONS_START -->
.py
<!-- EXTENSIONS_END -->

<!-- EXTENSIONLESS_START -->
LICENSE
<!-- EXTENSIONLESS_END -->')
echo x > "$export_dir4/main.py"
if out=$(run_check "$export_dir4" "$manifest4" 2>&1); then
  fail "a stale EXTENSIONLESS entry should hard-fail"
else
  pass "stale EXTENSIONLESS entry hard-fails"
fi
echo "$out" | grep -qF "stale EXTENSIONLESS entry 'LICENSE'" && pass "FAIL line names the stale entry" || fail "stale entry not named: $out"

echo
echo "=== No-blanket guard: a wildcard EXTENSIONS entry is rejected, not honoured ==="
read -r manifest5 export_dir5 < <(make_case '<!-- EXTENSIONS_START -->
*
<!-- EXTENSIONS_END -->')
echo x > "$export_dir5/anything.xyz"
if out=$(run_check "$export_dir5" "$manifest5" 2>&1); then
  fail "a blanket '*' EXTENSIONS entry should be rejected, not silently honoured"
else
  pass "blanket EXTENSIONS entry rejected"
fi
echo "$out" | grep -qF "blanket entry" && pass "FAIL line names it as a blanket entry" || fail "blanket reason not stated: $out"

echo
echo "=== No EXTENSIONS marker block at all is a hard error, not a silent pass ==="
root6="$(mktemp -d)"
mkdir -p "$root6/export"
echo "no marker blocks here" > "$root6/MANIFEST.md"
if out=$(run_check "$root6/export" "$root6/MANIFEST.md" 2>&1); then
  fail "a manifest with no EXTENSIONS block should hard-fail, not pass"
else
  pass "missing EXTENSIONS block hard-fails"
fi

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
