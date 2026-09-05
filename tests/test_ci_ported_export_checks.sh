#!/usr/bin/env bash
# tests/test_ci_ported_export_checks.sh — the FAILING direction of the three
# checks D#2348 PR-i ported from open-source/checks/ into scripts/ci/.
#
# Why this suite exists, specifically. All three ported checks are wired as
# steps in .github/workflows/ci.yml, so their PASSING direction is exercised
# on every PR against the real tree — that half needs no test. Their failing
# direction is exercised by nothing. A regex that silently stopped matching,
# an exclusion prefix that swallowed the whole tree, a document list that
# came out empty: each of those turns the gate green and looks exactly like
# a clean repository. A negative assertion that fails open is worse than a
# red one, so the assertions here are all of the shape "plant the defect,
# run the REAL check, require it to fail and to name what it found".
#
# The originals keep their own suites (tests/test_repo_target_gate.sh,
# tests/test_dangling_doc_commands.sh) and are untouched by this file. This
# covers what the port changed: a different forbidden slug, a git-ls-files
# subject set with push-set exclusions, and a document set narrowed to
# README.md + CONTRIBUTING.md.
#
# Run: bash tests/test_ci_ported_export_checks.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RTG="$REPO_ROOT/scripts/ci/repo-target-gate.sh"
DDC="$REPO_ROOT/scripts/ci/dangling-doc-commands.sh"
RML="$REPO_ROOT/scripts/ci/readme-links.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# The owner identity comes from IDENTIFIER-RULES.txt, the same place the gate
# reads it, and is deliberately not spelled out anywhere in this file. That
# name is itself a forbidden identifier, and tests/ joins the published tree
# later in this cutover — a fixture that hard-codes it would be a leak
# waiting on a manifest change, and it is exactly the defect the gate under
# test exists to catch.
RULES_FILE="$REPO_ROOT/open-source/IDENTIFIER-RULES.txt"
if [[ ! -f "$RULES_FILE" ]]; then
  echo "SKIP: $RULES_FILE not present (export or adopter tree) — nothing to test"
  exit 0
fi
OWNER="$(sed -n 's/^[[:space:]]*OLD_OWNER=\(.*\)$/\1/p' "$RULES_FILE" | head -1)"
CURRENT="$(sed -n 's/^[[:space:]]*CURRENT_REPO=\(.*\)$/\1/p' "$RULES_FILE" | head -1)"
PRERENAME="$(sed -n 's/^[[:space:]]*OLD_REPO_PRERENAME=\(.*\)$/\1/p' "$RULES_FILE" | head -1)"
if [[ -z "$OWNER" || -z "$CURRENT" || -z "$PRERENAME" ]]; then
  echo "FAIL: could not read OLD_OWNER / CURRENT_REPO / OLD_REPO_PRERENAME from $RULES_FILE"
  exit 1
fi
SLUG="$OWNER/$CURRENT"
OLD_SLUG="$OWNER/$PRERENAME"

# Every fixture needs open-source/IDENTIFIER-RULES.txt present, because the
# gate resolves the owner from it and skips cleanly on a tree without one.
# A fixture missing it would make every assertion below pass vacuously —
# the exact shape of green this suite exists to rule out.
seed_rules() {
  mkdir -p "$1/open-source"
  cp "$RULES_FILE" "$1/open-source/IDENTIFIER-RULES.txt"
}

# ---------------------------------------------------------------------------
# repo-target-gate.sh
#
# Its subject set is `git ls-files`, so a fixture has to be a real git repo
# with the files actually staged — a directory of loose files is invisible
# to it, which is itself worth asserting (test 5).
#
# Every fixture also has to satisfy the gate's unconditional allowlist: an
# entry whose anchor resolves to no line is a stale entry and fails the run
# on its own. So the base fixture stubs each allowlisted path with a line
# carrying that entry's anchor AND a matching forbidden shape, at line
# numbers deliberately unrelated to the real files' — the anchor's whole
# point is that its resolved position is irrelevant.
# ---------------------------------------------------------------------------
make_repo() {
  local dir="$SCRATCH/$1"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" config user.email t@t.invalid
  git -C "$dir" config user.name t
  seed_rules "$dir"

  # Second arg overrides the slug the stubs are written with, so a fixture
  # can be built for an owner other than this repo's.
  local slug="${2:-$SLUG}"
  mkdir -p "$dir/backend/fleet" "$dir/ts-backend/src/config" "$dir/tui/src" \
           "$dir/loop-bootstrap" "$dir/scripts" "$dir/dashboard_tui/readers"
  printf '#     "repo": "%s",\n' "$slug"                                        > "$dir/backend/fleet/runtime.py"
  printf '\n\nexport const DEFAULT_REPO = "%s";\n' "$slug"                       > "$dir/ts-backend/src/config/repo.ts"
  printf '\nGH_REPO: "%s",\n' "$slug"                                            > "$dir/tui/src/backend.ts"
  printf 'x = "gh api graphql --repo %s"\n' "$slug"                              > "$dir/tui/src/index.tsx"
  printf 'SOURCE_REPO="${LOOP_BOOTSTRAP_SOURCE_REPO:-%s}"\nENGINE_CANONICAL_REPO="${LOOP_BOOTSTRAP_ENGINE_REPO:-%s}"\n' "$slug" "$slug" > "$dir/loop-bootstrap/bootstrap.sh"
  printf '\n\n\nDEFAULT_ENGINE_REPO="${LOOP_BOOTSTRAP_ENGINE_REPO:-%s}"\n' "$slug" > "$dir/scripts/update-check.sh"
  printf '_REPO = "%s"\n' "$slug"                                                > "$dir/dashboard_tui/readers/pr_detail.py"
  # open-source/ is an excluded scan prefix, so the seeded rules file is
  # never itself a subject — assert that rather than assume it.

  git -C "$dir" add -A
  printf '%s' "$dir"
}

run_rtg() { OUT="$(bash "$RTG" "$1" 2>&1)"; RC=$?; }

echo "repo-target-gate.sh (ported)"

BASE="$(make_repo base)"
run_rtg "$BASE"
if [[ "$RC" -eq 0 ]]; then
  pass "clean fixture passes (every allowlist anchor resolves)"
else
  fail "clean fixture should pass, got rc=$RC: $OUT"
fi

# The assertion the whole suite exists for: the pattern set is live.
DIR="$(make_repo detect)"
printf 'PLANTED="%s"\n' "$SLUG" > "$DIR/scripts/planted.sh"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -ne 0 && "$OUT" == *"scripts/planted.sh:1"* ]]; then
  pass "an assignment resolving to the private owner fails, naming file and line"
else
  fail "planted assignment should fail naming scripts/planted.sh:1, got rc=$RC: $OUT"
fi

# The old repo name is equally wrong as a default; the port matches both.
DIR="$(make_repo oldname)"
printf 'PLANTED="%s"\n' "$OLD_SLUG" > "$DIR/scripts/planted.sh"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -ne 0 && "$OUT" == *"scripts/planted.sh"* ]]; then
  pass "the pre-rename private slug is caught too, not just the current one"
else
  fail "pre-rename slug should fail, got rc=$RC: $OUT"
fi

# The pass above only means something if a URL does NOT trip it — otherwise
# the gate would be flagging every github.com link in the tree and its green
# runs would be luck.
DIR="$(make_repo urlform)"
printf 'PLANTED="https://github.com/%s"\n' "$SLUG" > "$DIR/scripts/planted.sh"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -eq 0 ]]; then
  pass "a URL carrying the same slug is not a repo-target default"
else
  fail "URL form should pass, got rc=$RC: $OUT"
fi

# Untracked is invisible — documented blind spot 5, asserted rather than
# left as a claim in a comment.
DIR="$(make_repo untracked)"
printf 'PLANTED="%s"\n' "$SLUG" > "$DIR/scripts/planted.sh"
run_rtg "$DIR"
if [[ "$RC" -eq 0 ]]; then
  pass "an untracked file is outside the subject set (blind spot 5)"
else
  fail "untracked file should not be scanned, got rc=$RC: $OUT"
fi

# An excluded prefix really excludes.
DIR="$(make_repo excluded)"
mkdir -p "$DIR/archive/old"
printf 'PLANTED="%s"\n' "$SLUG" > "$DIR/archive/old/planted.sh"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -eq 0 ]]; then
  pass "archive/ is excluded, so an archive move does not fail the gate"
else
  fail "archive/ should be excluded, got rc=$RC: $OUT"
fi

# A subject set of zero must be a hard failure, not a vacuous pass. The
# fixture carries the rules file (so the gate gets past its tree-shape
# check) and nothing else — which also demonstrates that open-source/ is
# genuinely excluded from the subject set rather than merely assumed to be.
DIR="$SCRATCH/empty"
mkdir -p "$DIR"
git -C "$DIR" init -q
seed_rules "$DIR"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -ne 0 && "$OUT" == *"zero candidate files"* ]]; then
  pass "an empty subject set fails rather than reporting everything fine"
else
  fail "empty repo should hard-fail, got rc=$RC: $OUT"
fi

# Tree-shape skip: an export or adopter tree has no open-source/, therefore
# no engine-owner identity to hunt, therefore nothing to gate. Skipping is
# right there — the risk is skipping for the WRONG reason, which the next
# assertion covers.
DIR="$SCRATCH/noopensource"
mkdir -p "$DIR/scripts"
git -C "$DIR" init -q
printf 'PLANTED="%s"\n' "$SLUG" > "$DIR/scripts/planted.sh"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -eq 0 && "$OUT" == *"SKIP"* ]]; then
  pass "a tree with no open-source/ skips cleanly (export / adopter shape)"
else
  fail "no-open-source tree should skip, got rc=$RC: $OUT"
fi

# ...and open-source/ present with the rules file GONE is rot, not export
# shape. Without this the skip above would also absorb a deleted rules file
# and the gate would quietly stop gating.
DIR="$SCRATCH/rot"
mkdir -p "$DIR/scripts"
git -C "$DIR" init -q
seed_rules "$DIR"
rm "$DIR/open-source/IDENTIFIER-RULES.txt"
printf 'PLANTED="%s"\n' "$SLUG" > "$DIR/scripts/planted.sh"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -ne 0 ]]; then
  pass "open-source/ present but the rules file missing is rot, and fails"
else
  fail "missing rules file under a present open-source/ should fail, got rc=$RC: $OUT"
fi

# The owner really is read from the rules file, not baked into the gate.
# Build a whole fixture belonging to a different owner — stubs, rules file
# and all — then plant a line carrying THIS repo's slug. It must not match,
# because this repo's owner is not the one that fixture's rules file names.
# The mirror of it (same fixture, a line carrying the fixture's own owner)
# must match, or "does not match" would just mean the gate stopped working.
OTHER_OWNER="some-other-owner"
OTHER_SLUG="$OTHER_OWNER/thing"

DIR="$(make_repo otherowner "$OTHER_SLUG")"
sed -i "s|^OLD_OWNER=.*|OLD_OWNER=$OTHER_OWNER|" "$DIR/open-source/IDENTIFIER-RULES.txt"
printf 'PLANTED="%s"\n' "$SLUG" > "$DIR/scripts/planted.sh"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -eq 0 ]]; then
  pass "under a different OLD_OWNER, this repo's slug is not hunted"
else
  fail "a different OLD_OWNER should stop matching this repo's slug, got rc=$RC: $OUT"
fi

DIR="$(make_repo otherowner2 "$OTHER_SLUG")"
sed -i "s|^OLD_OWNER=.*|OLD_OWNER=$OTHER_OWNER|" "$DIR/open-source/IDENTIFIER-RULES.txt"
printf 'PLANTED="%s"\n' "$OTHER_SLUG" > "$DIR/scripts/planted.sh"
git -C "$DIR" add -A
run_rtg "$DIR"
if [[ "$RC" -ne 0 && "$OUT" == *"scripts/planted.sh"* ]]; then
  pass "...and that fixture's own owner IS hunted, so the pass above is real"
else
  fail "the fixture's own owner should match, got rc=$RC: $OUT"
fi

# ---------------------------------------------------------------------------
# dangling-doc-commands.sh — the port narrowed the document set to
# README.md + CONTRIBUTING.md. Assert both that it still detects, and that
# the narrowing did not leave it scanning nothing.
# ---------------------------------------------------------------------------
echo "dangling-doc-commands.sh (ported)"

DIR="$SCRATCH/ddc"
mkdir -p "$DIR"
printf 'Run `bash scripts/definitely-not-here.sh` first.\n' > "$DIR/README.md"
OUT="$(bash "$DDC" "$DIR" 2>&1)"; RC=$?
if [[ "$RC" -ne 0 && "$OUT" == *"definitely-not-here.sh"* ]]; then
  pass "a doc command pointing at a missing file fails, naming the reference"
else
  fail "missing reference should fail, got rc=$RC: $OUT"
fi

printf 'Run `bash scripts/real.sh` first.\n' > "$DIR/README.md"
mkdir -p "$DIR/scripts"; : > "$DIR/scripts/real.sh"
OUT="$(bash "$DDC" "$DIR" 2>&1)"; RC=$?
if [[ "$RC" -eq 0 && "$OUT" == *"scanned 1 doc(s)"* ]]; then
  pass "a resolvable reference passes, and the scope line says what was scanned"
else
  fail "resolvable reference should pass, got rc=$RC: $OUT"
fi

# wiki/ is deliberately no longer scanned. Asserted so that re-adding it is
# a deliberate act with a test to update, not an accident.
mkdir -p "$DIR/wiki"
printf 'Run `bash scripts/definitely-not-here.sh` first.\n' > "$DIR/wiki/Page.md"
OUT="$(bash "$DDC" "$DIR" 2>&1)"; RC=$?
if [[ "$RC" -eq 0 && "$OUT" != *"wiki/Page.md"* ]]; then
  pass "wiki/ is outside the ported document set"
else
  fail "wiki/ should not be scanned, got rc=$RC: $OUT"
fi

DIR="$SCRATCH/ddc-empty"
mkdir -p "$DIR"
OUT="$(bash "$DDC" "$DIR" 2>&1)"; RC=$?
if [[ "$RC" -eq 2 ]]; then
  pass "no documents at all is a usage error, not a green run"
else
  fail "empty document set should exit 2, got rc=$RC: $OUT"
fi

# ---------------------------------------------------------------------------
# readme-links.sh
# ---------------------------------------------------------------------------
echo "readme-links.sh (ported)"

DIR="$SCRATCH/rml"
mkdir -p "$DIR"
printf 'See [nothing](docs/definitely-not-here.md).\n' > "$DIR/README.md"
OUT="$(bash "$RML" "$DIR" 2>&1)"; RC=$?
if [[ "$RC" -ne 0 && "$OUT" == *"definitely-not-here.md"* ]]; then
  pass "a broken README link fails, naming the target"
else
  fail "broken link should fail, got rc=$RC: $OUT"
fi

mkdir -p "$DIR/docs"; : > "$DIR/docs/real.md"
printf 'See [something](docs/real.md).\n' > "$DIR/README.md"
OUT="$(bash "$RML" "$DIR" 2>&1)"; RC=$?
if [[ "$RC" -eq 0 ]]; then
  pass "a resolving README link passes"
else
  fail "resolving link should pass, got rc=$RC: $OUT"
fi

echo
echo "passed: $PASS   failed: $FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
