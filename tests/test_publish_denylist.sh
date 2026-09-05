#!/usr/bin/env bash
# tests/test_publish_denylist.sh — the FAILING direction of the two D#2348
# PR-h gates: scripts/ci/publish-denylist.sh and scripts/ci/pr-link-policy.sh.
#
# WHY THIS SUITE EXISTS
#
# Both gates are wired as jobs in .github/workflows/ci.yml, so their PASSING
# direction is exercised against a real PR on every run and needs no test.
# Their failing direction is exercised by nothing on a well-behaved PR — and
# three gates in this same cutover shipped in a state where they could not
# fail at all (a script referenced by no job, a hook registered in no
# settings file, a SKIP-on-missing-input branch). Every one was found by a
# person going to look. So every assertion below is of the shape "construct
# the violation, run the REAL script, require it to exit non-zero and to
# name what it found".
#
# Two assertions go further and defeat the script itself — emptying the
# denylist, and stubbing the owner lookup — to establish that these fixtures
# can tell a working gate from a broken one. A negative test that still
# passes against a gutted implementation is measuring nothing.
#
# The private owner name is not written down anywhere in this file. Both
# gates resolve it at run time, and every fixture here supplies a synthetic
# one instead — which is a stronger assertion than using the real name would
# be, since a hard-coded literal in either script would make these fail.
#
# Run: bash tests/test_publish_denylist.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DENYLIST="$REPO_ROOT/scripts/ci/publish-denylist.sh"
LINKPOLICY="$REPO_ROOT/scripts/ci/pr-link-policy.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

FIXTURE_OWNER="synthetic-private-owner"

for f in "$DENYLIST" "$LINKPOLICY"; do
  if [[ ! -f "$f" ]]; then
    echo "FAIL: $f is missing — the gate this suite tests does not exist"
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Fixture repo helper.
#
# publish-denylist.sh reads a real diff, so a fixture has to be a real git
# repository with a base commit and a branch commit — a directory of loose
# files produces no diff at all and every assertion would pass vacuously,
# which is the exact shape of green this suite exists to rule out.
# ---------------------------------------------------------------------------
new_repo() {
  local dir="$SCRATCH/$1"
  mkdir -p "$dir"
  git -C "$dir" init -q -b main
  git -C "$dir" config user.email "fixture@example.invalid"
  git -C "$dir" config user.name "fixture"
  mkdir -p "$dir/backend"
  echo "base" >"$dir/backend/base.txt"
  git -C "$dir" add -A
  git -C "$dir" commit -qm "base"
  printf '%s\n' "$dir"
}

# commit_add <repo> <path> — writes a file at <path> and commits it.
commit_add() {
  local dir="$1" path="$2"
  mkdir -p "$dir/$(dirname "$path")"
  echo "content" >"$dir/$path"
  git -C "$dir" add -A
  git -C "$dir" commit -qm "add $path"
}

# run_denylist <repo> [base] — runs the gate inside <repo>, echoes output,
# returns its exit code.
run_denylist() {
  local dir="$1" base="${2:-main}"
  ( cd "$dir" && PUBLISH_DENYLIST_BASE="$base" bash "$DENYLIST" 2>&1 )
}

echo "=== publish-denylist.sh ==="

# --- 1. Each denied prefix fails, on a real diff, naming the path ----------
#
# Four of the six prefixes have tracked files in the live repo today;
# gemma-sandbox/ has none (the real directory is scripts/gemma-sandbox/), so
# its case is necessarily synthetic. That is why every case here is
# synthetic: one fixture shape, no special case for the one path that cannot
# have a real example.
i=0
for denied in \
  ".autonomous-team/state.json" \
  "archive/some-tool-2026-09-04/thing.py" \
  "scripts/training/train.py" \
  "scripts/serving/deploy.sh" \
  "scripts/gemma-sandbox/loop.sh" \
  "gemma-sandbox/loop.sh" \
  "backend/.env.local" \
  ".env"; do
  i=$((i + 1))
  repo="$(new_repo "denied-$i")"
  git -C "$repo" checkout -qb topic
  commit_add "$repo" "$denied"
  out="$(run_denylist "$repo")"
  rc=$?
  if [[ $rc -eq 0 ]]; then
    fail "adding '$denied' should fail the gate, but it exited 0"
  elif ! printf '%s' "$out" | grep -Fq "$denied"; then
    fail "gate failed on '$denied' but did not name the path in its output"
  else
    pass "adding '$denied' fails and names the path"
  fi
done

# --- 2. Allowed paths that sit right next to a rule -------------------------
#
# These are the discriminating cases. Each one starts with, or contains, the
# text of a denied rule and is separated from it by a single character. A
# prefix rule that lost its trailing slash, or a glob that decayed to a bare
# "env" substring, would over-block and turn every one of these red — which
# is the failure direction nobody looks for, because it presents as a gate
# doing its job.
i=0
for allowed in \
  "scripts/trainingwheels.sh" \
  "docs/archived-notes.md" \
  "backend/environment.py" \
  "autonomous-team-notes.md" \
  ".env.example" \
  "backend/.env.example"; do
  i=$((i + 1))
  repo="$(new_repo "allowed-$i")"
  git -C "$repo" checkout -qb topic
  commit_add "$repo" "$allowed"
  out="$(run_denylist "$repo")"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    fail "adding '$allowed' should pass the gate, but it exited $rc: $out"
  else
    pass "adding '$allowed' passes — the rule still discriminates"
  fi
done

# --- 2b. Paths git would quote must still be matched ----------------------
#
# Regression for a real bypass. With plain `git diff --name-only`, git applies
# core.quotePath to any path it thinks is unprintable: a non-ASCII path comes
# back as "\303\251tat.json", wrapped in literal double quotes with its bytes
# octal-escaped. That string does not start with `.autonomous-team/`, so every
# prefix rule missed it and the gate passed — with a fully green self-test,
# because the self-test feeds the classifier paths it constructs itself and so
# never sees the diff-reading step where the defect lives. Measured before the
# fix: two files under two different denied prefixes, rc=0.
i=0
for denied in \
  ".autonomous-team/état-résumé.json" \
  "archive/2026/日本語.md" \
  "scripts/training/naïve model.py"; do
  i=$((i + 1))
  repo="$(new_repo "quoted-$i")"
  git -C "$repo" checkout -qb topic
  commit_add "$repo" "$denied"
  out="$(run_denylist "$repo")"
  rc=$?
  if [[ $rc -eq 0 ]]; then
    fail "adding '$denied' should fail; a path git quotes must not slip the prefix rules"
  elif ! printf '%s' "$out" | grep -Fq "$denied"; then
    fail "gate failed on '$denied' but did not name it verbatim (still quoted?): $out"
  else
    pass "a path git would quote ('$denied') is matched and named verbatim"
  fi
done

# And the parse invariant itself: a path arriving quoted must be a loud
# failure rather than a silent miss, so that dropping -z can never go green.
repo="$(new_repo "quotepath-invariant")"
git -C "$repo" checkout -qb topic
commit_add "$repo" "docs/café.md"
UNZ="$SCRATCH/no-z-denylist.sh"
sed 's|git diff -z --diff-filter=A|git diff --diff-filter=A|' "$DENYLIST" >"$UNZ"
out="$( cd "$repo" && PUBLISH_DENYLIST_BASE=main bash "$UNZ" 2>&1 )"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "dropping -z left the gate passing on a quoted path — the parse invariant is not load-bearing"
elif ! printf '%s' "$out" | grep -q "none were parsed out of its output"; then
  fail "dropping -z failed, but not via the parse invariant: $out"
else
  pass "dropping -z is caught by the parse invariant, on an otherwise-allowed path"
fi

# --- 3. The .env.example carve-out is exact-basename, not a suffix ---------
repo="$(new_repo "env-lookalike")"
git -C "$repo" checkout -qb topic
commit_add "$repo" "prod.env.example"
out="$(run_denylist "$repo")"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "'prod.env.example' should still be denied — the carve-out must not be a suffix rule"
else
  pass "'prod.env.example' is denied: the .env.example carve-out is exact-basename"
fi

# --- 4. A modified (not added) file under a denied prefix does not fail ----
#
# The gate's subject is added paths. archive/ and .autonomous-team/ are meant
# to stay tracked in the private repo, so a tree-wide scan would be red on
# every PR forever. This asserts the diff-filter is really doing that work.
repo="$(new_repo "modify-only")"
mkdir -p "$repo/.autonomous-team"
echo "v1" >"$repo/.autonomous-team/state.json"
git -C "$repo" add -A
git -C "$repo" commit -qm "pre-existing state file"
git -C "$repo" checkout -qb topic
echo "v2" >"$repo/.autonomous-team/state.json"
git -C "$repo" add -A
git -C "$repo" commit -qm "modify state file"
out="$(run_denylist "$repo")"
rc=$?
if [[ $rc -ne 0 ]]; then
  fail "modifying an existing file under a denied prefix should pass, got $rc: $out"
else
  pass "modifying (not adding) under a denied prefix passes"
fi

# --- 5. An unresolvable base fails; it does not skip -----------------------
repo="$(new_repo "bad-base")"
out="$(run_denylist "$repo" "no-such-ref")"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "an unresolvable base ref must fail, not pass — got exit 0"
elif ! printf '%s' "$out" | grep -q "does not resolve"; then
  fail "unresolvable base failed but did not say why: $out"
else
  pass "an unresolvable base ref fails rather than skipping"
fi

# --- 6. Not a git checkout fails; it does not skip -------------------------
mkdir -p "$SCRATCH/not-a-repo"
out="$( cd "$SCRATCH/not-a-repo" && bash "$DENYLIST" 2>&1 )"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "running outside a git checkout must fail, not pass — got exit 0"
else
  pass "running outside a git checkout fails rather than skipping"
fi

# --- 7. Defeat the classifier: the self-test must catch it ----------------
#
# This is the assertion that makes the rest of this section mean something.
# If the denylist were emptied — the way a badly-resolved merge conflict
# would empty it — every "denied" case above would go green and look exactly
# like a clean repository. The in-script self-test is what turns that into a
# red check-run, and this proves it is load-bearing rather than decorative.
GUTTED="$SCRATCH/gutted-denylist.sh"
sed 's|^  "scripts/training/"$||' "$DENYLIST" >"$GUTTED"
repo="$(new_repo "gutted")"
out="$( cd "$repo" && PUBLISH_DENYLIST_BASE=main bash "$GUTTED" 2>&1 )"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "removing a denylist entry left the gate passing — the self-test is not load-bearing"
elif ! printf '%s' "$out" | grep -q "SELF-TEST FAIL"; then
  fail "gutted gate failed, but not via the self-test: $out"
else
  pass "removing a denylist entry is caught by the script's own self-test"
fi

echo
echo "=== pr-link-policy.sh ==="

run_link() {
  local body="$1"
  PRIVATE_REPO_OWNER="$FIXTURE_OWNER" PR_BODY="$body" bash "$LINKPOLICY" 2>&1
}

# --- 8. A bare closing reference passes ------------------------------------
out="$(run_link "Adds the thing.

Closes D#2348")"
rc=$?
if [[ $rc -ne 0 ]]; then
  fail "a bare 'Closes D#2348' body should pass, got $rc: $out"
else
  pass "a bare D# closing reference with no URL passes"
fi

# --- 9. A private-repo URL fails, and the message redacts the owner --------
out="$(run_link "Closes D#2348

Context: https://github.com/$FIXTURE_OWNER/somerepo/discussions/2348")"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "a body containing a private-repo URL must fail, got exit 0"
elif printf '%s' "$out" | grep -Fq "$FIXTURE_OWNER"; then
  fail "the failure message echoed the private owner name back instead of redacting it"
else
  pass "a private-repo URL fails, with the owner name redacted from the output"
fi

# --- 10. Missing and Issue-only references fail ----------------------------
out="$(run_link "Just a description with no reference.")"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "a body with no closing reference must fail, got exit 0"
else
  pass "a body with no closing reference fails"
fi

out="$(run_link "Fixes a timer bug.

Closes #14")"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "a bare Issue reference ('Closes #14') must not satisfy the D# rule"
else
  pass "an Issue-only reference does not satisfy the D# rule"
fi

# --- 11. An empty body fails; an unset PR_BODY is a wiring error (exit 2) --
out="$(run_link "")"
rc=$?
if [[ $rc -ne 1 ]]; then
  fail "an empty PR body should fail rule 1 with exit 1, got $rc: $out"
else
  pass "an empty PR body fails rule 1"
fi

out="$(PRIVATE_REPO_OWNER="$FIXTURE_OWNER" bash "$LINKPOLICY" 2>&1)"
rc=$?
if [[ $rc -ne 2 ]]; then
  fail "with no body input at all the gate should exit 2, got $rc: $out"
else
  pass "no body input at all exits 2 — distinguishable from a real empty body"
fi

# --- 11b. PR_BODY_FILE is the CI input path, and it must not echo the body -
#
# CI reads the body from a file rather than the step's env: block, because the
# runner prints a step's env into the log before the step runs — which
# published a violating body verbatim into a public log, on exactly the PRs
# this gate exists to catch. So the file path has to work, and the script's
# own output must never contain the body.
BODYFILE="$SCRATCH/pr-body.txt"
printf 'Adds a thing.\n\nCloses D#2348\n' >"$BODYFILE"
out="$(PRIVATE_REPO_OWNER="$FIXTURE_OWNER" PR_BODY_FILE="$BODYFILE" bash "$LINKPOLICY" 2>&1)"
rc=$?
if [[ $rc -ne 0 ]]; then
  fail "a compliant body supplied via PR_BODY_FILE should pass, got $rc: $out"
else
  pass "PR_BODY_FILE is accepted as the body source"
fi

# The marker sits on its own line, away from the offending one. Naming the
# offending line is the gate's job and is wanted; reproducing the rest of the
# body is not.
SECRET_MARKER="unique-marker-that-must-not-be-logged"
printf 'Closes D#2348\n%s\nsee https://github.com/%s/x\n' "$SECRET_MARKER" "$FIXTURE_OWNER" >"$BODYFILE"
out="$(PRIVATE_REPO_OWNER="$FIXTURE_OWNER" PR_BODY_FILE="$BODYFILE" bash "$LINKPOLICY" 2>&1)"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "a violating body via PR_BODY_FILE should fail, got exit 0"
elif printf '%s' "$out" | grep -Fq "$SECRET_MARKER"; then
  fail "the gate echoed a non-offending part of the body into its output"
else
  pass "a violating body via PR_BODY_FILE fails without echoing the whole body"
fi

out="$(PRIVATE_REPO_OWNER="$FIXTURE_OWNER" PR_BODY_FILE="$SCRATCH/does-not-exist" bash "$LINKPOLICY" 2>&1)"
rc=$?
if [[ $rc -ne 2 ]]; then
  fail "an unreadable PR_BODY_FILE should be a wiring error (exit 2), got $rc: $out"
else
  pass "an unreadable PR_BODY_FILE exits 2 rather than passing"
fi

# --- 12. The owner comes from the rules file, and its absence FAILS -------
#
# Copies the real script into a synthetic tree so REPO_ROOT resolves there,
# then runs it with no rules file and no environment override. The branch
# under test is the one this cutover has already shipped wrong three times:
# a missing input must produce a red verdict, not a green skip.
TREE="$SCRATCH/tree/scripts/ci"
mkdir -p "$TREE"
cp "$LINKPOLICY" "$TREE/pr-link-policy.sh"
out="$(PR_BODY="Closes D#1" bash "$TREE/pr-link-policy.sh" 2>&1)"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "with no rules file and no override the gate passed — a gate that cannot name its target must not report a pass"
elif ! printf '%s' "$out" | grep -q "could not resolve the private owner"; then
  fail "no-owner case failed but did not say why: $out"
else
  pass "with no resolvable owner the gate fails rather than skipping"
fi

# And with a rules file present at the post-PR-i location, it reads OLD_OWNER
# from there — proving the name is data, not a literal in the script.
printf 'OLD_OWNER=%s\n' "$FIXTURE_OWNER" >"$TREE/IDENTIFIER-RULES.txt"
out="$(PR_BODY="Closes D#1 https://github.com/$FIXTURE_OWNER/x" bash "$TREE/pr-link-policy.sh" 2>&1)"
rc=$?
if [[ $rc -eq 0 ]]; then
  fail "the gate did not hunt the owner named by the rules file it found"
else
  pass "the owner is read from IDENTIFIER-RULES.txt, not hard-coded"
fi

# A body citing a DIFFERENT owner must pass — otherwise the rule is matching
# 'github.com/' rather than the owner, and would block every legitimate link.
out="$(PR_BODY="Closes D#1 https://github.com/some-other-org/x" bash "$TREE/pr-link-policy.sh" 2>&1)"
rc=$?
if [[ $rc -ne 0 ]]; then
  fail "an unrelated GitHub URL should pass, got $rc: $out"
else
  pass "an unrelated GitHub URL passes — the rule is owner-scoped, not host-scoped"
fi

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] || exit 1
exit 0
