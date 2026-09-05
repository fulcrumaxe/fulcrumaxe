#!/usr/bin/env bash
# tests/test_build_public_seed.sh — the seed builder carries every export.sh
# transformation, and its verify arm can actually fail.
#
# Two of export.sh's filters were dropped in the builder's first version and
# both were found by a person going to look. The one that mattered would have
# published a personal account handle. So this suite is written the way the
# other guard suites here are: each assertion is paired with a MUTATION that
# must flip exactly that assertion, because a gate nobody has watched fail is
# not evidence of anything.
#
# The mutations run against a COPY of the builder in a tmpdir, never the real
# one, and every build targets the real repo read-only — the builder writes
# blobs to the object store and nothing else, so there is no tree to restore.
#
# Run: bash tests/test_build_public_seed.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILDER="$REPO_ROOT/scripts/build-public-seed.sh"
PARENT="$(git -C "$REPO_ROOT" rev-parse HEAD)"   # any commit works as the parent

PASS=0
FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# run_builder <script-path> -> sets OUT (stdout+stderr) and RC
#
# AUTONOMOUS_TEAM_REPO_ROOT is what lets a mutant copy in $TMP resolve the
# real repo. Without it the builder derives its root from its own location
# and a mutant reports "not a git repository" — which reads as the mutation
# being caught when nothing was caught at all.
run_builder() {
  OUT="$(cd "$REPO_ROOT" && AUTONOMOUS_TEAM_REPO_ROOT="$REPO_ROOT" bash "$1" --parent "$PARENT" 2>&1)"
  RC=$?
}

# run_builder_fixture <script-path> -> same, but against the fixture commit.
run_builder_fixture() {
  OUT="$(cd "$REPO_ROOT" && AUTONOMOUS_TEAM_REPO_ROOT="$REPO_ROOT" \
    bash "$1" --sha "$FIXTURE_SHA" --parent "$PARENT" 2>&1)"
  RC=$?
}

# tree_of <builder-output> -> the built tree's sha, or EMPTY if the build did
# not print a commit.
#
# `git rev-parse <junk>^{tree}` prints "<junk>^{tree}" on STDOUT and exits 128
# when junk does not resolve, so capturing it unguarded yields a non-empty
# string that is not a tree. Every downstream "X is absent from the tree"
# assertion then passes for the wrong reason: nothing is present in a tree
# that does not exist. That is how E3 and E4 passed against a builder whose
# prune had been deliberately broken — the build refused, so the last line of
# output was the refusal message, and absence was trivially satisfied.
#
# --verify --quiet is what turns a non-resolving argument into empty output;
# the hex guard keeps a refused build's prose from reaching rev-parse at all.
# Callers must treat empty as a FAILURE, never as a satisfied absence.
tree_of() {
  local sha
  sha="$(tail -1 <<<"$1")"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 0
  git -C "$REPO_ROOT" rev-parse --verify --quiet "$sha^{tree}" 2>/dev/null
}

# blob_in_tree <tree> <path> -> that path's blob sha in that tree, or EMPTY.
#
# The same trap as tree_of, one level down and nastier. With an empty tree
# argument the string becomes ":<path>", which is valid git syntax for stage 0
# of the INDEX — so a check written to read the built tree silently reads the
# working checkout instead, finds the file present and clean, and reports a
# pass about an artifact it never looked at. E4 passed exactly this way
# against a builder whose prune was broken: no tree was built, ":engine/
# manifest.json" resolved to the checked-out manifest, and that one has no
# withheld path in it.
#
# Callers must treat empty as a FAILURE, never as a satisfied absence.
blob_in_tree() {
  [[ -n "$1" ]] || return 0
  git -C "$REPO_ROOT" rev-parse --verify --quiet "$1:$2" 2>/dev/null
}

# mutant <name> <sed-expr> -> path to a mutated copy of the builder
mutant() {
  local out="$TMP/$1.sh"
  sed "$2" "$BUILDER" > "$out"
  printf '%s' "$out"
}

# ---------------------------------------------------------------------------
# THE FIXTURE COMMIT — why the guards below do not read the live tree.
#
# On 2026-09-05 four assertions in this suite went red with nothing touched in
# either the builder or this file. The phase-1 split (D#2348, merged as #2398)
# moved the 13 `tier: project` memories and the 13 engine/manifest.json
# entries naming withheld scripts/ subdirectories out to the internal repo.
# Both SUBJECTS left the tree, so the prune correctly removed 0 files and the
# manifest filter correctly dropped 0 entries.
#
# The four that went red were the vacuity guards. The two they were guarding —
# B1 ("no tier:project memory survived") and B4b ("the manifest names no
# withheld path") — did NOT go red. They went vacuously green, which is worse:
# a vacuous green is indistinguishable from a working guard, and these two are
# the last check before a one-way publication.
#
# The relaxation that would turn this file green again — assert >= 0 in place
# of > 0 — is the defect this suite exists to catch, not a fix for it. So the
# subject is SUPPLIED rather than asserted away. build_fixture builds a commit
# that is the parent's tree plus exactly three things: one tier:project
# memory, one MEMORY.md line pointing at it, and one manifest entry naming a
# path under scripts/training/. The builder then runs against THAT commit via
# --sha, which it already accepts.
#
# This is stronger than what it replaces, not weaker. The old assertions held
# only for as long as the repo happened to contain a tier:project memory, and
# disarmed themselves silently the day it stopped — which is exactly what
# happened. These test the BUILDER'S BEHAVIOUR and keep working whatever the
# live tree holds, including on the public repo, where none of the original
# subjects will ever exist again.
#
# Nothing is written to the working tree. The objects go into the object store
# the same way the builder's own blobs and commit already do, and nothing ever
# references them.
# ---------------------------------------------------------------------------
MEMORY_REL="scripts/memory-triage"
FIXTURE_MEMORY="zzz-seed-fixture-tier-project.md"
FIXTURE_WITHHELD="scripts/training/zzz-seed-fixture-withheld.sh"

build_fixture() {
  local idx="$TMP/fixture.index"
  rm -f "$idx"
  GIT_INDEX_FILE="$idx" git -C "$REPO_ROOT" read-tree "$PARENT"

  # A memory the prune must remove. Frontmatter shaped exactly as read_tier
  # expects: `---`, the field, `---`.
  local mem_blob
  mem_blob="$(printf '%s\n' '---' 'tier: project' '---' '' \
    'Synthetic memory added by tests/test_build_public_seed.sh so the tier' \
    'prune has something to remove. It exists only inside a fixture commit;' \
    'the prune this fixture exercises is what takes it back out again.' \
    | git -C "$REPO_ROOT" hash-object -w --stdin)"
  GIT_INDEX_FILE="$idx" git -C "$REPO_ROOT" update-index --add \
    --cacheinfo "100644,$mem_blob,$MEMORY_REL/$FIXTURE_MEMORY"

  # An index line pointing at it, so the MEMORY.md prune has a line to drop.
  local idx_blob new_idx_blob
  idx_blob="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$PARENT:$MEMORY_REL/MEMORY.md")"
  [[ -n "$idx_blob" ]] || {
    echo "build_fixture: $MEMORY_REL/MEMORY.md not in $PARENT" >&2; return 1; }
  new_idx_blob="$( { git -C "$REPO_ROOT" cat-file blob "$idx_blob"
      printf -- '- [seed fixture](%s) — synthetic, pruned at build time\n' "$FIXTURE_MEMORY"
    } | git -C "$REPO_ROOT" hash-object -w --stdin)"
  GIT_INDEX_FILE="$idx" git -C "$REPO_ROOT" update-index --add \
    --cacheinfo "100644,$new_idx_blob,$MEMORY_REL/MEMORY.md"

  # A manifest entry naming a path under a withheld directory — the exact
  # disclosure shape the filter exists to remove. The hash is filler; the
  # filter decides on the KEY, and nothing in the seed reads the value.
  local mf_blob new_mf_blob
  mf_blob="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$PARENT:engine/manifest.json")"
  [[ -n "$mf_blob" ]] || {
    echo "build_fixture: engine/manifest.json not in $PARENT" >&2; return 1; }
  new_mf_blob="$(git -C "$REPO_ROOT" cat-file blob "$mf_blob" \
    | FIXTURE_WITHHELD="$FIXTURE_WITHHELD" python3 -c '
import json, os, sys
doc = json.load(sys.stdin)
doc["files"][os.environ["FIXTURE_WITHHELD"]] = "0" * 64
json.dump(doc, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
' | git -C "$REPO_ROOT" hash-object -w --stdin)"
  GIT_INDEX_FILE="$idx" git -C "$REPO_ROOT" update-index --add \
    --cacheinfo "100644,$new_mf_blob,engine/manifest.json"

  local tree
  tree="$(GIT_INDEX_FILE="$idx" git -C "$REPO_ROOT" write-tree)"
  [[ -n "$tree" ]] || { echo "build_fixture: write-tree produced nothing" >&2; return 1; }
  # Identity supplied explicitly: commit-tree refuses without one, and a CI
  # clone has no user.name/user.email configured. Without this the fixture
  # build fails there and every E assertion goes red on a machine difference
  # rather than on anything about the builder.
  GIT_AUTHOR_NAME="seed-suite" GIT_AUTHOR_EMAIL="seed-suite@invalid" \
  GIT_COMMITTER_NAME="seed-suite" GIT_COMMITTER_EMAIL="seed-suite@invalid" \
    git -C "$REPO_ROOT" commit-tree "$tree" -p "$PARENT" \
      -m "seed-suite fixture (never referenced)"
}

echo "=== A. the real builder produces a tree and a sha ==="
run_builder "$BUILDER"
if [[ "$RC" -eq 0 ]]; then ok "builder exits 0"; else bad "builder exits 0" "rc=$RC: $OUT"; fi
SEED_SHA="$(tail -1 <<<"$OUT")"
if [[ "$SEED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  ok "prints a 40-hex commit sha on stdout"
else
  bad "prints a commit sha" "got: $SEED_SHA"
fi
TREE="$(tree_of "$OUT")"

echo ""
echo "=== B. every export.sh transformation is carried ==="

# B1 — memory-tier prune. Not one tier:project memory survives.
#
# This is a statement about the tree this repo would actually publish today,
# and it is worth keeping for that alone: it goes red the moment someone adds
# a tier:project memory back and the prune fails to catch it. But since the
# split it is also VACUOUSLY true — there are no tier:project memories left in
# the tree for the prune to miss. It therefore proves nothing about the prune
# on its own, and the assertion that it works is E3, on the fixture commit.
# B1b, which used to carry that non-vacuity claim here, moved there with it.
survived=0
while IFS= read -r path; do
  blob="$(git -C "$REPO_ROOT" rev-parse "$TREE:$path" 2>/dev/null)" || continue
  tier="$(git -C "$REPO_ROOT" cat-file blob "$blob" | sed -n '2,/^---$/p' | sed -n 's/^tier:[[:space:]]*//p' | head -1)"
  [[ "$tier" == "project" ]] && survived=$((survived + 1))
done < <(git -C "$REPO_ROOT" ls-tree -r --name-only "$TREE" -- scripts/memory-triage | grep '\.md$' | grep -v 'MEMORY\.md$')
if [[ "$survived" -eq 0 ]]; then
  ok "B1 memory-tier prune: zero tier:project memories in the tree"
else
  bad "B1 memory-tier prune" "$survived tier:project file(s) survived"
fi

# B1b moved to E1. It asserted "the prune removed a non-zero count" against
# the live tree, which stopped being true when the 13 tier:project memories
# left in the split — not because the prune broke, but because it ran out of
# subjects. Re-pointed at a fixture that always supplies one rather than
# relaxed to accept zero; see build_fixture's header.

# B2 — MEMORY.md index prune. Every link target still resolves in the tree.
# Vacuous on the live tree for the same reason as B1 (nothing is pruned, so
# no entry can dangle); E2 is where it is exercised with something to drop.
dangling=0
idx_blob="$(blob_in_tree "$TREE" "scripts/memory-triage/MEMORY.md")"
if [[ -n "$idx_blob" ]]; then
  while IFS= read -r target; do
    git -C "$REPO_ROOT" rev-parse "$TREE:scripts/memory-triage/$target" >/dev/null 2>&1 \
      || dangling=$((dangling + 1))
  done < <(git -C "$REPO_ROOT" cat-file blob "$idx_blob" | grep -oE '\]\([^)]+\)' | tr -d '](' | tr -d ')')
  if [[ "$dangling" -eq 0 ]]; then
    ok "B2 MEMORY.md index prune: no entry points at a pruned file"
  else
    bad "B2 MEMORY.md index prune" "$dangling dangling entr(y|ies)"
  fi
else
  bad "B2 MEMORY.md index prune" "MEMORY.md missing from the tree entirely"
fi

# B3 — plugin auto-discovery mirror, byte-identical to .claude/.
mirror_ok=1
for name in agents commands; do
  a="$(git -C "$REPO_ROOT" ls-tree -r "$TREE" -- ".claude/$name" | sed "s#\t.claude/#\t#")"
  b="$(git -C "$REPO_ROOT" ls-tree -r "$TREE" -- "$name")"
  [[ -n "$a" && "$a" == "$b" ]] || mirror_ok=0
done
if [[ "$mirror_ok" -eq 1 ]]; then
  ok "B3 plugin-root mirror: agents/ and commands/ are byte-identical to .claude/"
else
  bad "B3 plugin-root mirror" "agents/ or commands/ missing or drifted from .claude/"
fi

# B4 — bootstrap-paths.generated present and comment-free.
gen="$(blob_in_tree "$TREE" "loop-bootstrap/bootstrap-paths.generated")"
if [[ -n "$gen" ]]; then
  body="$(git -C "$REPO_ROOT" cat-file blob "$gen")"
  if grep -q '===' <<<"$body" && ! grep -q '^#' <<<"$body"; then
    ok "B4 bootstrap-paths.generated present, has its === separator, carries no prose"
  else
    bad "B4 bootstrap-paths.generated" "malformed: $body"
  fi
else
  bad "B4 bootstrap-paths.generated" "absent from the tree"
fi

# B4b — engine/manifest.json names no path that is absent from the tree.
# 13 of the entries it shipped with named files under scripts/training/,
# scripts/serving/ and scripts/gemma-sandbox/ — a list of withheld internal
# work by name, which is the disclosure the denylist exists to prevent.
#
# Those 13 left the manifest in the split, so like B1 this now says something
# true about today's shipped artifact while proving nothing about the filter.
# E4 is the non-vacuous form, on a fixture whose manifest does name one.
mf_blob="$(blob_in_tree "$TREE" "engine/manifest.json")"
if [[ -n "$mf_blob" ]]; then
  mf_missing=0
  while IFS= read -r p; do
    git -C "$REPO_ROOT" rev-parse "$TREE:$p" >/dev/null 2>&1 || mf_missing=$((mf_missing + 1))
  done < <(git -C "$REPO_ROOT" cat-file blob "$mf_blob" \
            | python3 -c 'import json,sys; [print(p) for p in json.load(sys.stdin)["files"]]')
  if [[ "$mf_missing" -eq 0 ]]; then
    ok "B4b engine/manifest.json names no path absent from the tree"
  else
    bad "B4b engine/manifest.json" "$mf_missing entr(y|ies) name a file not in the tree"
  fi
  if git -C "$REPO_ROOT" cat-file blob "$mf_blob" \
       | grep -qE 'scripts/(training|serving|gemma-sandbox)/'; then
    bad "B4b manifest names no withheld path" "a withheld scripts/ subdirectory is still named"
  else
    ok "B4b engine/manifest.json names none of the withheld scripts/ subdirectories"
  fi
else
  bad "B4b engine/manifest.json" "absent from the tree"
fi

# B5 — the identifier rewrite pass is deliberately NOT carried. Asserted so a
# future reader sees a decision rather than wondering whether it was missed:
# the private slug is expected present, because CLAUDE.md's Repo Scope table
# names the Discussion plane as a literal and that file ships.
if git -C "$REPO_ROOT" grep -q -I -E 'autonomous-agent-7' "$TREE" -- CLAUDE.md 2>/dev/null; then
  ok "B5 identifier rewrite deliberately not carried (private slug present, per the Repo Scope rule)"
else
  bad "B5 identifier rewrite" "the slug is absent — a rewrite pass ran that was not supposed to"
fi

echo ""
echo "=== E. the transformations fire when there IS something to transform ==="

FIXTURE_SHA="$(build_fixture)"
run_builder_fixture "$BUILDER"
if [[ "$RC" -eq 0 ]]; then
  ok "E0 the builder accepts the fixture commit and still builds"
else
  bad "E0 builder on fixture" "rc=$RC: $(tail -5 <<<"$OUT")"
fi
FIX_TREE="$(tree_of "$OUT")"

# E1 — replaces B1b. The prune removed the memory the fixture planted.
if grep -qE 'removed [1-9][0-9]* tier:project' <<<"$OUT"; then
  ok "E1 the tier prune removes a tier:project memory when one is present"
else
  bad "E1 tier prune removes" "$(grep 'Memory-tier prune' <<<"$OUT" || tail -3 <<<"$OUT")"
fi

# E2 — the index prune dropped the line pointing at it, rather than shipping
# a MEMORY.md that names a file the prune had just taken out.
if grep -qE 'MEMORY\.md dropped [1-9][0-9]* entries' <<<"$OUT"; then
  ok "E2 the MEMORY.md index prune drops the entry whose target was pruned"
else
  bad "E2 index prune drops" "$(grep 'Memory-tier prune' <<<"$OUT" || tail -3 <<<"$OUT")"
fi

# E3 — the outcome B1 asserts, stated where it is not vacuous: the planted
# tier:project memory is absent from the tree that was actually built.
#
# "No tree" is a FAILURE here, not an absence that satisfies the assertion.
# The first version of this check read `[[ -n "$FIX_TREE" ]] && rev-parse ...`
# and fell through to ok when the build refused — so breaking the prune made
# E3 pass, because a build that never produced a tree trivially produced no
# tree containing the memory. Caught by running this suite against a builder
# whose prune had been mutated to keep tier:project files.
if [[ -z "$FIX_TREE" ]]; then
  bad "E3 tier:project memory absent from the built tree" "no fixture tree was built"
elif git -C "$REPO_ROOT" rev-parse "$FIX_TREE:$MEMORY_REL/$FIXTURE_MEMORY" >/dev/null 2>&1; then
  bad "E3 tier:project memory absent from the built tree" "it survived the prune"
else
  ok "E3 the planted tier:project memory is absent from the built tree"
fi

# E4 — the outcome B4b asserts, likewise: the manifest entry naming a path
# under a withheld directory is gone from the built tree's manifest. Same
# no-tree rule as E3, for the same reason.
fix_mf="$(blob_in_tree "$FIX_TREE" "engine/manifest.json")"
if [[ -z "$fix_mf" ]]; then
  bad "E4 manifest filter drops the withheld entry" "no manifest in the fixture tree"
elif git -C "$REPO_ROOT" cat-file blob "$fix_mf" | grep -qE 'scripts/(training|serving|gemma-sandbox)/'; then
  bad "E4 manifest filter drops the withheld entry" "a withheld path survived into the manifest"
else
  ok "E4 the manifest filter drops the planted entry naming a withheld path"
fi

echo ""
echo "=== C. the verify arm refuses, and refuses to print a sha ==="

# C1 — mutant: prune keeps tier:project files. Verify must catch it.
#
# Runs against the FIXTURE, not the live tree. Against the live tree this
# mutant is a no-op — a prune told to keep tier:project memories removes the
# same zero files as one told to remove them, so the verify arm has nothing to
# catch and the assertion passes whether the mutation is applied or not. That
# is precisely the shape this suite exists to reject, and it is what C1 had
# become. The fixture guarantees there is one memory whose fate differs
# between the two versions, which is what makes the mutant a mutant.
M1="$(mutant keeps_project 's/(removed if tier == "project" else kept)/(kept if tier == "project" else kept)/')"
run_builder_fixture "$M1"
if [[ "$RC" -ne 0 ]] && grep -q 'tier:project memory survived' <<<"$OUT"; then
  ok "C1 mutant (prune keeps tier:project) is caught by the verify arm"
else
  bad "C1 mutant caught" "rc=$RC, output: $(tail -3 <<<"$OUT")"
fi
if [[ "$RC" -ne 0 ]] && ! tail -1 <<<"$OUT" | grep -qE '^[0-9a-f]{40}$'; then
  ok "C1 no commit sha is printed when verify fails"
else
  bad "C1 no sha on failure" "last line: $(tail -1 <<<"$OUT")"
fi

# C2 — mutant: an enforced forbidden pattern is present in the tree. Injects a
# blob rather than editing a tracked file, so the real tree is never touched.
#
# The injected string is DERIVED from the live pattern list, never written
# here: a test that spelled a forbidden identifier out would itself be a
# tracked file carrying one, and the pre-push gate rightly fails the commit.
# (It did, on the first version of this line.) Deriving it also means the
# test keeps testing the real list rather than one literal that was true
# once — every enforced pattern here is a run of literal characters and
# single-character classes, so taking the first member of each class yields
# a concrete string that matches.
LEAK_LITERAL="$(bash "$REPO_ROOT/scripts/check-forbidden-identifiers.sh" --list-patterns \
  | head -1 \
  | python3 -c 'import re,sys; print(re.sub(r"\[(.)[^]]*\]", r"\1", sys.stdin.read().strip()))')"
if [[ -z "$LEAK_LITERAL" ]]; then
  bad "C2 setup" "could not derive a matching literal from the pattern list"
fi
M2="$(mutant leaks_identifier 's#^printf .100644 blob %s\\t%s\\0. "\$GEN_BLOB".*#&\nprintf "100644 blob %s\\t%s\\0" "$(printf "%s\\n" "$LEAK_LITERAL" | git hash-object -w --stdin)" "LEAK.md" >> "$INDEX_INFO"#')"
export LEAK_LITERAL
run_builder "$M2"
if [[ "$RC" -ne 0 ]] && grep -q 'forbidden pattern present in the built tree' <<<"$OUT"; then
  ok "C2 mutant (forbidden pattern in the tree) is caught by the verify arm"
else
  bad "C2 mutant caught" "rc=$RC, output: $(tail -3 <<<"$OUT")"
fi
if [[ "$RC" -ne 0 ]] && ! tail -1 <<<"$OUT" | grep -qE '^[0-9a-f]{40}$'; then
  ok "C2 no commit sha is printed when the pattern scan fails"
else
  bad "C2 no sha on failure" "last line: $(tail -1 <<<"$OUT")"
fi

# C3 — mutant: an unrecognised tier must hard-fail the prune, not default.
# This is why the prune is carried rather than a path list.
M3="$(mutant bad_tier 's/VALID_TIERS = {"transferable", "hardwire-candidate", "project"}/VALID_TIERS = {"transferable"}/')"
run_builder "$M3"
if [[ "$RC" -ne 0 ]] && grep -q 'unrecognised tier' <<<"$OUT"; then
  ok "C3 an unrecognised tier hard-fails the build (fail-closed, not defaulted)"
else
  bad "C3 unrecognised tier hard-fails" "rc=$RC, output: $(tail -3 <<<"$OUT")"
fi

# C3b — mutant: a manifest filter that drops nothing must be visible. The
# builder cannot refuse here (a manifest naming only present files is the
# normal case), so the assertion is E4's — the unfiltered manifest reaches the
# tree still naming a withheld path.
#
# On the FIXTURE for the same reason as C1: the live manifest no longer names
# any withheld path, so `dropped = []` changes nothing there and the mutant
# was silently equivalent to the original.
M3b="$(mutant keeps_manifest 's/^dropped = sorted(p for p in files if p not in kept)$/dropped = []/')"
run_builder_fixture "$M3b"
if [[ "$RC" -eq 0 ]]; then
  T3B="$(tree_of "$OUT")"
  mb="$(blob_in_tree "$T3B" "engine/manifest.json")"
  if [[ -n "$mb" ]] \
     && git -C "$REPO_ROOT" cat-file blob "$mb" | grep -qE 'scripts/(training|serving|gemma-sandbox)/'; then
    ok "C3b mutant (filter drops nothing) reintroduces the withheld path — E4 is not vacuous"
  else
    bad "C3b mutant reintroduces withheld paths" "unfiltered manifest names none, so E4 proves nothing"
  fi
else
  bad "C3b mutant builds" "rc=$RC: $(tail -3 <<<"$OUT")"
fi

# C4 — mutant: empty pattern list must refuse rather than pass vacuously.
M4="$(mutant empty_patterns 's#^done < <(bash "\$REPO_ROOT/scripts/check-forbidden-identifiers.sh" --list-patterns) \\#done < <(true) \\#')"
run_builder "$M4"
if [[ "$RC" -ne 0 ]] && grep -q 'zero enforced patterns' <<<"$OUT"; then
  ok "C4 an empty pattern list refuses instead of reporting a vacuous pass"
else
  bad "C4 empty pattern list refuses" "rc=$RC, output: $(tail -3 <<<"$OUT")"
fi

echo ""
echo "=== D. the exclusion list still matches the recorded decisions ==="
for d in archive/ .autonomous-team/ open-source/ wiki/ dashboard_tui/ docker/ \
         systemd/ verification-report/ templates/ scripts/training/ \
         scripts/serving/ scripts/gemma-sandbox/; do
  if git -C "$REPO_ROOT" ls-tree -r --name-only "$TREE" | grep -q "^$d"; then
    bad "D excluded prefix absent from the tree" "$d is present"
  fi
done
ok "D no excluded prefix appears in the built tree"

if git -C "$REPO_ROOT" ls-tree -r --name-only "$TREE" | grep -qx 'pr-body-p5e.txt'; then
  bad "D scratch file excluded" "pr-body-p5e.txt is present"
else
  ok "D the committed PR description is excluded"
fi

echo ""
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
[[ "$FAIL" -eq 0 ]]
