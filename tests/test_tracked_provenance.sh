#!/usr/bin/env bash
# tests/test_tracked_provenance.sh — regression test for
# open-source/checks/tracked-provenance.sh (D#2275 rev 2).
#
# Runs the REAL tracked-provenance.sh, unmodified, against a synthetic git
# repo + <export-dir> + MANIFEST.md built fresh per test case — the same
# division of labor tests/test_shipped_to_listed.sh uses for its sibling
# check: this pins the check's own logic (tracked resolution, the exact-path
# exemption, fail-closed-on-missing-git-root) independent of whatever the
# live repo's export happens to contain on a given day.
#
# Usage: bash tests/test_tracked_provenance.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$REPO_ROOT/open-source/checks/tracked-provenance.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# make_case <manifest-body> — a fresh temp dir with a git repo at
# <root>/src (committed files land here), a MANIFEST.md, and an empty
# export/ dir to populate. Echoes "<manifest> <export-dir> <git-root>".
make_case() {
  local body="$1"
  local root
  root="$(mktemp -d)"
  mkdir -p "$root/export" "$root/src"
  git init -q "$root/src"
  git -C "$root/src" config user.email "test@example.com"
  git -C "$root/src" config user.name "test"
  printf '%s\n' "$body" > "$root/MANIFEST.md"
  echo "$root/MANIFEST.md $root/export $root/src"
}

commit_tracked() {
  local git_root="$1" relpath="$2"
  mkdir -p "$(dirname "$git_root/$relpath")"
  echo x > "$git_root/$relpath"
  git -C "$git_root" add "$relpath"
  git -C "$git_root" commit -q -m "add $relpath"
}

run_check() {
  local export_dir="$1" manifest="$2" git_root="$3"
  bash "$CHECK_SCRIPT" "$export_dir" "$manifest" "$git_root"
}

echo "=== Every shipped file is tracked -- exits 0 ==="
read -r manifest export_dir git_root < <(make_case '<!-- GENERATED_PATHS_START -->
agents/
<!-- GENERATED_PATHS_END -->')
commit_tracked "$git_root" "dashboard/a.txt"
commit_tracked "$git_root" "CLAUDE.md"
mkdir -p "$export_dir/dashboard" "$export_dir/agents"
echo x > "$export_dir/dashboard/a.txt"
echo x > "$export_dir/CLAUDE.md"
echo x > "$export_dir/agents/role.md"
if out=$(run_check "$export_dir" "$manifest" "$git_root" 2>&1); then
  pass "fully-tracked export exits 0"
else
  fail "fully-tracked export should exit 0, got: $out"
fi
echo "$out" | grep -q "scanned 3 shipped file(s)" && pass "scope line reports 3 files scanned" || fail "scope line missing or wrong count: $out"
echo "$out" | grep -q "against 2 tracked git entries" && pass "scope line reports 2 tracked entries" || fail "tracked-entry count missing: $out"

echo
echo "=== Untracked, undeclared file -- exits 1, names it and states why ==="
echo "internal, not committed" > "$export_dir/dashboard/leaked.internal"
if out=$(run_check "$export_dir" "$manifest" "$git_root" 2>&1); then
  fail "export with an untracked undeclared file should exit non-zero"
else
  pass "export with an untracked undeclared file exits non-zero"
fi
echo "$out" | grep -qF "dashboard/leaked.internal" && pass "FAIL line names the untracked path" || fail "untracked path not named: $out"
echo "$out" | grep -qF "not tracked in git and not declared as generated" && pass "FAIL line states the reason, not just 'unaccounted'" || fail "reason missing: $out"
rm -f "$export_dir/dashboard/leaked.internal"

echo
echo "=== Exact-path exemption: loop-bootstrap/bootstrap-paths.generated is not flagged ==="
mkdir -p "$export_dir/loop-bootstrap"
echo x > "$export_dir/loop-bootstrap/bootstrap-paths.generated"
if out=$(run_check "$export_dir" "$manifest" "$git_root" 2>&1); then
  pass "the hardcoded exact-path exemption resolves without a GENERATED_PATHS entry"
else
  fail "exact-path exemption should exit 0, got: $out"
fi
rm -rf "$export_dir/loop-bootstrap"

echo
echo "=== GENERATED_PATHS resolves an untracked file the same way shipped-to-listed.sh does ==="
echo x > "$export_dir/agents/another-role.md"
if out=$(run_check "$export_dir" "$manifest" "$git_root" 2>&1); then
  pass "untracked file under a declared GENERATED_PATHS prefix resolves"
else
  fail "GENERATED_PATHS entry should cover this file, got: $out"
fi
rm -f "$export_dir/agents/another-role.md"

echo
echo "=== Fail-closed: REPO_ROOT is not a git repo at all ==="
root2="$(mktemp -d)"
mkdir -p "$root2/notgit"
if out=$(run_check "$export_dir" "$manifest" "$root2/notgit" 2>&1); then
  fail "a non-git REPO_ROOT should hard-fail, not pass"
else
  pass "non-git REPO_ROOT hard-fails"
fi
echo "$out" | grep -qF "git provenance could not be established" && pass "failure names the reason (not a git repo)" || fail "reason not stated: $out"

echo
echo "=== Fail-closed: git repo with zero tracked entries (empty ls-files) ==="
root3="$(mktemp -d)"
git init -q "$root3"
if out=$(run_check "$export_dir" "$manifest" "$root3" 2>&1); then
  fail "an empty git index should hard-fail, not pass everything"
else
  pass "empty git index hard-fails"
fi
echo "$out" | grep -qF "returned zero entries" && pass "failure states the empty-index reason" || fail "empty-index reason not stated: $out"

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
