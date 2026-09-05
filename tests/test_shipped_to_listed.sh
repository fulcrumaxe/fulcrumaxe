#!/usr/bin/env bash
# tests/test_shipped_to_listed.sh — regression test for
# open-source/checks/shipped-to-listed.sh (D#2211), the shipped -> listed
# half of manifest-completeness.
#
# Runs the REAL shipped-to-listed.sh, unmodified, against a synthetic
# <export-dir> + MANIFEST.md built fresh per test case -- not the real
# 1335-file export, just the handful of files each case needs to exercise
# one behavior. This pins the check's own logic (resolution rules, the
# stale-declaration guard, the duplicate-declaration guard) independent of
# whatever the live repo's export tree happens to contain on a given day --
# open-source/verify-export.sh's own red/green run against the real export
# (see the PR this test shipped with) is what proves the check works on the
# real tree today; this test is what stops its logic regressing silently
# later, the same division of labor test_bootstrap_paths_generated.sh uses
# for export.sh's generator step.
#
# Usage: bash tests/test_shipped_to_listed.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$REPO_ROOT/open-source/checks/shipped-to-listed.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# make_case <manifest-body> -- builds a fresh temp dir with an
# open-source/MANIFEST.md containing the given body and an empty
# export/ dir alongside it to populate with files. Echoes "<manifest> <export-dir>".
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

echo "=== Every shipped file resolves -- exits 0 ==="
read -r manifest export_dir < <(make_case '<!-- PATHS_START -->
dashboard/
CLAUDE.md
<!-- PATHS_END -->

<!-- GENERATED_PATHS_START -->
agents/
<!-- GENERATED_PATHS_END -->')
mkdir -p "$export_dir/dashboard/sub" "$export_dir/agents"
echo x > "$export_dir/dashboard/a.txt"
echo x > "$export_dir/dashboard/sub/b.txt"
echo x > "$export_dir/CLAUDE.md"
echo x > "$export_dir/agents/role.md"
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  pass "fully-accounted export exits 0"
else
  fail "fully-accounted export should exit 0, got: $out"
fi
echo "$out" | grep -q "scanned 4 shipped file(s)" && pass "scope line reports 4 files scanned" || fail "scope line missing or wrong count: $out"

echo
echo "=== Unlisted file -- exits 1, names it ==="
mkdir -p "$export_dir/plugins"
echo x > "$export_dir/plugins/injected-unlisted.md"
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  fail "export with an unlisted file should exit non-zero"
else
  pass "export with an unlisted file exits non-zero"
fi
echo "$out" | grep -qF "plugins/injected-unlisted.md" && pass "FAIL line names the unlisted path" || fail "unlisted path not named in output: $out"
rm -rf "$export_dir/plugins"

echo
echo "=== Directory PATHS entry matches by prefix, not just top-level ==="
if out=$(run_check "$export_dir" "$manifest" 2>&1); then
  pass "nested dashboard/sub/b.txt resolves via the dashboard/ prefix entry"
else
  fail "dashboard/sub/b.txt should resolve via prefix, got: $out"
fi

echo
echo "=== Glob PATHS entry matches only the glob's own directory ==="
read -r manifest2 export_dir2 < <(make_case '<!-- PATHS_START -->
.claude/agents/*.md
<!-- PATHS_END -->')
mkdir -p "$export_dir2/.claude/agents/nested"
echo x > "$export_dir2/.claude/agents/role.md"
if out=$(run_check "$export_dir2" "$manifest2" 2>&1); then
  pass "glob entry alone (no nested file) exits 0"
else
  fail "glob-only export should exit 0, got: $out"
fi
echo x > "$export_dir2/.claude/agents/nested/deep.md"
if out=$(run_check "$export_dir2" "$manifest2" 2>&1); then
  fail "a file the glob entry does not literally cover should not be silently accepted"
else
  pass "file outside the glob's own directory (nested/deep.md) is correctly unaccounted"
fi
echo "$out" | grep -qF "nested/deep.md" && pass "FAIL line names the out-of-glob path" || fail "out-of-glob path not named: $out"

echo
echo "=== Stale-declaration guard: a GENERATED_PATHS entry matching nothing hard-fails ==="
read -r manifest3 export_dir3 < <(make_case '<!-- PATHS_START -->
CLAUDE.md
<!-- PATHS_END -->

<!-- GENERATED_PATHS_START -->
nonexistent-generated/
<!-- GENERATED_PATHS_END -->')
echo x > "$export_dir3/CLAUDE.md"
if out=$(run_check "$export_dir3" "$manifest3" 2>&1); then
  fail "a GENERATED_PATHS entry matching zero files should hard-fail"
else
  pass "stale GENERATED_PATHS entry hard-fails"
fi
echo "$out" | grep -qF "stale GENERATED_PATHS entry 'nonexistent-generated/'" && pass "FAIL line names the stale entry" || fail "stale entry not named: $out"

echo
echo "=== Duplicate-declaration guard: a GENERATED_PATHS entry redundant with PATHS hard-fails ==="
read -r manifest4 export_dir4 < <(make_case '<!-- PATHS_START -->
backend/
<!-- PATHS_END -->

<!-- GENERATED_PATHS_START -->
backend/
<!-- GENERATED_PATHS_END -->')
mkdir -p "$export_dir4/backend"
echo x > "$export_dir4/backend/main.py"
if out=$(run_check "$export_dir4" "$manifest4" 2>&1); then
  fail "a GENERATED_PATHS entry duplicating a PATHS entry should hard-fail"
else
  pass "redundant GENERATED_PATHS entry hard-fails"
fi
echo "$out" | grep -qF "redundant with PATHS entry 'backend/'" && pass "FAIL line names the redundant entry" || fail "redundant entry not named: $out"

echo
echo "=== Duplicate-declaration guard also catches a nested GENERATED_PATHS dir inside a listed PATHS dir ==="
read -r manifest5 export_dir5 < <(make_case '<!-- PATHS_START -->
scripts/
<!-- PATHS_END -->

<!-- GENERATED_PATHS_START -->
scripts/gen/
<!-- GENERATED_PATHS_END -->')
mkdir -p "$export_dir5/scripts/gen"
echo x > "$export_dir5/scripts/gen/tool.sh"
if out=$(run_check "$export_dir5" "$manifest5" 2>&1); then
  fail "a GENERATED_PATHS dir nested inside an already-listed PATHS dir should hard-fail as redundant"
else
  pass "nested-inside-listed-dir GENERATED_PATHS entry hard-fails"
fi
echo "$out" | grep -qF "redundant with PATHS entry 'scripts/'" && pass "FAIL line names the containing PATHS entry" || fail "containing entry not named: $out"

echo
echo "=== No PATHS marker block at all is a hard error, not a silent pass ==="
root6="$(mktemp -d)"
mkdir -p "$root6/export"
echo "no marker blocks here" > "$root6/MANIFEST.md"
if out=$(run_check "$root6/export" "$root6/MANIFEST.md" 2>&1); then
  fail "a manifest with no PATHS block should hard-fail, not pass"
else
  pass "missing PATHS block hard-fails"
fi

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
