#!/usr/bin/env bash
# tests/test_bootstrap_paths_generated.sh — regression test for the
# loop-bootstrap/bootstrap-paths.generated generator in open-source/export.sh
# (PR #2206 fix round 3).
#
# export.sh classifies every RSYNC_EXCLUDES pattern into exactly one of two
# lists (GENERATED_EXCLUDES_SHIP, GENERATED_EXCLUDES_NOOP) before baking the
# generated data file loop-bootstrap/bootstrap.sh falls back to when
# open-source/ isn't shipped. A pattern in neither list must hard-fail the
# export -- that's the whole point of the classification being a fail-closed
# partition rather than a denylist that lets an unclassified pattern through
# by default (a denylist is the exact stale-mask shape that let
# loop-bootstrap/backend-snapshot/ ship silently before D#1890).
#
# Runs the REAL open-source/export.sh, unmodified, against a minimal
# synthetic REPO_ROOT built fresh per test case -- not a full checkout copy,
# just the four files export.sh's generator step actually touches
# (export.sh, MANIFEST.md, lib/manifest_paths.sh, lib/rsync-excludes.sh) plus
# a one-file loop-bootstrap/ dir so the "did loop-bootstrap/ land in this
# export" gate trips. export.sh warns-and-skips any other PATHS entry whose
# source dir is missing, so nothing else needs to exist.
#
# Usage: bash tests/test_bootstrap_paths_generated.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_EXPORT_SH="$REPO_ROOT/open-source/export.sh"
REAL_MANIFEST_PATHS_SH="$REPO_ROOT/open-source/lib/manifest_paths.sh"
REAL_RSYNC_EXCLUDES_SH="$REPO_ROOT/open-source/lib/rsync-excludes.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# make_repo <rsync-excludes-source-file> — builds a fresh synthetic
# REPO_ROOT under a new temp dir, using the given file verbatim as
# open-source/lib/rsync-excludes.sh. Echoes the REPO_ROOT path.
make_repo() {
  local excludes_src="$1"
  local export_sh_src="${2:-$REAL_EXPORT_SH}"
  local repo
  repo="$(mktemp -d)"
  mkdir -p "$repo/open-source/lib" "$repo/loop-bootstrap"
  cp "$export_sh_src" "$repo/open-source/export.sh"
  cp "$REAL_MANIFEST_PATHS_SH" "$repo/open-source/lib/manifest_paths.sh"
  cp "$excludes_src" "$repo/open-source/lib/rsync-excludes.sh"
  echo "placeholder, so export.sh's PATHS loop actually copies this dir" > "$repo/loop-bootstrap/placeholder.txt"
  cat > "$repo/open-source/MANIFEST.md" <<'EOF'
<!-- PATHS_START -->
loop-bootstrap/
<!-- PATHS_END -->

<!-- BOOTSTRAP_PATHS_START -->
backend/
<!-- BOOTSTRAP_PATHS_END -->
EOF
  echo "$repo"
}

echo "=== Happy path: the real rsync-excludes.sh classifies cleanly ==="

REPO1="$(make_repo "$REAL_RSYNC_EXCLUDES_SH")"
TARGET1="$(mktemp -d)"
OUT1="$(bash "$REPO1/open-source/export.sh" "$TARGET1" 2>&1)"
RC1=$?
GENERATED1="$TARGET1/loop-bootstrap/bootstrap-paths.generated"

if [[ "$RC1" -eq 0 ]]; then
  pass "export.sh exits 0 against the real, fully-classified rsync-excludes.sh"
else
  fail "export.sh exited $RC1 against the real rsync-excludes.sh: $OUT1"
fi

if [[ -f "$GENERATED1" ]]; then
  pass "bootstrap-paths.generated was written"
else
  fail "bootstrap-paths.generated was not written"
fi

# Every real RSYNC_EXCLUDES pattern is either present (ships) or absent
# (no-op) in the generated file's exclude section -- cross-checked against
# export.sh's own classification lists, extracted from its source so this
# test can't hardcode a copy that drifts from the real lists.
SHIP_LIST="$(awk '/^  GENERATED_EXCLUDES_SHIP=\(/{f=1;next} /^  \)/{if(f)exit} f' "$REAL_EXPORT_SH" | sed -e "s/^ *'//" -e "s/'$//")"
NOOP_LIST="$(awk '/^  GENERATED_EXCLUDES_NOOP=\(/{f=1;next} /^  \)/{if(f)exit} f' "$REAL_EXPORT_SH" | sed -e "s/^ *'//" -e "s/'$//")"

if [[ -n "$SHIP_LIST" ]]; then
  ship_ok=true
  while IFS= read -r pattern; do
    [[ -z "$pattern" ]] && continue
    grep -qxF "$pattern" "$GENERATED1" || { ship_ok=false; fail "ship-classified pattern '$pattern' missing from generated file"; }
  done <<< "$SHIP_LIST"
  [[ "$ship_ok" == "true" ]] && pass "every GENERATED_EXCLUDES_SHIP pattern appears in the generated file"
else
  fail "could not extract GENERATED_EXCLUDES_SHIP from export.sh (did its shape change?)"
fi

if [[ -n "$NOOP_LIST" ]]; then
  noop_ok=true
  while IFS= read -r pattern; do
    [[ -z "$pattern" ]] && continue
    grep -qxF "$pattern" "$GENERATED1" && { noop_ok=false; fail "no-op-classified pattern '$pattern' leaked into the generated file"; }
  done <<< "$NOOP_LIST"
  [[ "$noop_ok" == "true" ]] && pass "no GENERATED_EXCLUDES_NOOP pattern leaked into the generated file"
else
  fail "could not extract GENERATED_EXCLUDES_NOOP from export.sh (did its shape change?)"
fi

rm -rf "$REPO1" "$TARGET1"

echo ""
echo "=== Fail-closed: a pattern in RSYNC_EXCLUDES but classified nowhere must fail the export ==="

UNCLASSIFIED_RSYNC_EXCLUDES="$(mktemp)"
head -n -1 "$REAL_RSYNC_EXCLUDES_SH" > "$UNCLASSIFIED_RSYNC_EXCLUDES"
echo "  --exclude='/UNCLASSIFIED_TEST_PATTERN_XYZ/'" >> "$UNCLASSIFIED_RSYNC_EXCLUDES"
echo ")" >> "$UNCLASSIFIED_RSYNC_EXCLUDES"

REPO2="$(make_repo "$UNCLASSIFIED_RSYNC_EXCLUDES")"
TARGET2="$(mktemp -d)"
OUT2="$(bash "$REPO2/open-source/export.sh" "$TARGET2" 2>&1)"
RC2=$?

if [[ "$RC2" -ne 0 ]]; then
  pass "export.sh exits non-zero when RSYNC_EXCLUDES carries an unclassified pattern"
else
  fail "export.sh exited 0 with an unclassified pattern present -- the fail-closed partition let it through silently"
fi

if echo "$OUT2" | grep -q "UNCLASSIFIED_TEST_PATTERN_XYZ"; then
  pass "the error names the specific unclassified pattern"
else
  fail "the error output does not name the unclassified pattern: $OUT2"
fi

if [[ -f "$TARGET2/loop-bootstrap/bootstrap-paths.generated" ]]; then
  fail "bootstrap-paths.generated was written despite the export failing (should not exist)"
else
  pass "no bootstrap-paths.generated was left behind by the failed export"
fi

rm -rf "$REPO2" "$TARGET2" "$UNCLASSIFIED_RSYNC_EXCLUDES"

echo ""
echo "=== Fail-closed, glob-absorption near-miss: an unclassified pattern a glob compare would silently ship must still fail ==="

# The fail-closed classification in export.sh only works because its
# comparisons quote the right-hand side ([[ "$pattern" == "$p" ]]) -- a
# literal string compare, not a glob match. secret.pyc is genuinely
# unclassified (it is not GENERATED_EXCLUDES_SHIP's *.pyc entry, which
# only ships the bare literal "*.pyc"), but an unquoted RHS would let a
# glob comparison silently absorb it into that entry and ship it with no
# error. This is the one near-miss shape where a regression yields a
# silent ship instead of a loud failure, so it needs its own case rather
# than relying on the unrelated bogus pattern above to stand in for it.
GLOB_ABSORBED_RSYNC_EXCLUDES="$(mktemp)"
head -n -1 "$REAL_RSYNC_EXCLUDES_SH" > "$GLOB_ABSORBED_RSYNC_EXCLUDES"
echo "  --exclude='secret.pyc'" >> "$GLOB_ABSORBED_RSYNC_EXCLUDES"
echo ")" >> "$GLOB_ABSORBED_RSYNC_EXCLUDES"

REPO3="$(make_repo "$GLOB_ABSORBED_RSYNC_EXCLUDES")"
TARGET3="$(mktemp -d)"
OUT3="$(bash "$REPO3/open-source/export.sh" "$TARGET3" 2>&1)"
RC3=$?

if [[ "$RC3" -ne 0 ]]; then
  pass "export.sh exits non-zero on 'secret.pyc' even though '*.pyc' is a real SHIP entry"
else
  fail "export.sh exited 0 with 'secret.pyc' unclassified -- a glob-shaped near-miss was absorbed and shipped silently"
fi

if echo "$OUT3" | grep -q "secret.pyc"; then
  pass "the error names 'secret.pyc' specifically, not just some pattern"
else
  fail "the error output does not name secret.pyc: $OUT3"
fi

if [[ -f "$TARGET3/loop-bootstrap/bootstrap-paths.generated" ]]; then
  fail "bootstrap-paths.generated was written despite the export failing (should not exist)"
else
  pass "no bootstrap-paths.generated was left behind by the failed export"
fi

rm -rf "$REPO3" "$TARGET3" "$GLOB_ABSORBED_RSYNC_EXCLUDES"

echo ""
echo "=== Disjointness: a pattern classified in BOTH lists must fail the export ==="

# Mutates a COPY of the real export.sh, duplicating an existing SHIP entry
# (.autonomous-team/) into GENERATED_EXCLUDES_NOOP as well. rsync-excludes.sh
# itself is untouched and real -- the overlap lives entirely in export.sh's
# own classification lists, which is exactly the seam the disjointness
# assertion at export.sh guards.
DUPLICATED_EXPORT_SH="$(mktemp)"
sed "/GENERATED_EXCLUDES_NOOP=(/a\\    '.autonomous-team/'" "$REAL_EXPORT_SH" > "$DUPLICATED_EXPORT_SH"

if ! grep -q "'.autonomous-team/'" <(awk '/^  GENERATED_EXCLUDES_NOOP=\(/{f=1;next} /^  \)/{if(f)exit} f' "$DUPLICATED_EXPORT_SH"); then
  fail "test setup: the sed injection did not land inside GENERATED_EXCLUDES_NOOP (did export.sh's shape change?)"
else
  REPO4="$(make_repo "$REAL_RSYNC_EXCLUDES_SH" "$DUPLICATED_EXPORT_SH")"
  TARGET4="$(mktemp -d)"
  OUT4="$(bash "$REPO4/open-source/export.sh" "$TARGET4" 2>&1)"
  RC4=$?

  if [[ "$RC4" -ne 0 ]]; then
    pass "export.sh exits non-zero when a pattern is classified in both GENERATED_EXCLUDES_SHIP and GENERATED_EXCLUDES_NOOP"
  else
    fail "export.sh exited 0 with '.autonomous-team/' in both lists -- the disjointness assertion let it through silently"
  fi

  if echo "$OUT4" | grep -q "'.autonomous-team/' is classified in BOTH"; then
    pass "the error names the specific double-classified pattern"
  else
    fail "the error output does not name the double-classified pattern: $OUT4"
  fi

  rm -rf "$REPO4" "$TARGET4"
fi

rm -f "$DUPLICATED_EXPORT_SH"

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
exit $?
