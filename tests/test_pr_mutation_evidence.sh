#!/usr/bin/env bash
# tests/test_pr_mutation_evidence.sh — the seven required cases (D#1984
# Spec acceptance items 4-10) plus the always-reverts assertion (item 9),
# against the REAL script, on throwaway fixture repos.
#
# Run: bash tests/test_pr_mutation_evidence.sh
# Expects: all assertions pass, exit 0
#
# WHY BASH FIXTURES, NOT PYTHON
#
# The tested command runs twice per gate invocation — once on the clean
# tree, once with the patch applied, seconds apart. A Python fixture that
# imports a module hits CPython's __pycache__: if the git apply / git apply
# -R round trip lands within the same mtime tick, the second run can reuse
# bytecode compiled from the FIRST run's source and silently produce the
# wrong verdict (measured while building this suite: a `return x > 0` ->
# `return x < 0` mutation reported classify(5) as False on what should have
# been the clean-tree baseline run). Bash has no such cache — sourcing a
# file re-reads it from disk every time — so the fixtures here use a bash
# function under test, never a Python import.
#
# Run: bash tests/test_pr_mutation_evidence.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATE="$REPO_ROOT/scripts/ci/pr-mutation-evidence.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

if [[ ! -f "$GATE" ]]; then
  echo "FAIL: $GATE is missing — the gate this suite tests does not exist"
  exit 1
fi

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# ---------------------------------------------------------------------------
# Fixture repos. Each has backend/foo.sh (a classify() function) and
# tests/check.sh (asserts classify 5 == "True"). new_repo_broken_baseline's
# check.sh fails unconditionally, for the "baseline is not green" case.
# ---------------------------------------------------------------------------
_write_foo() {
  cat >"$1/backend/foo.sh" <<'SH'
classify() {
  local x="$1"
  if (( x > 0 )); then
    echo "True"
  else
    echo "False"
  fi
}
SH
}

new_repo() {
  local dir="$SCRATCH/$1"
  mkdir -p "$dir/backend" "$dir/tests"
  git -C "$dir" init -q -b main
  git -C "$dir" config user.email "fixture@example.invalid"
  git -C "$dir" config user.name "fixture"
  _write_foo "$dir"
  cat >"$dir/tests/check.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$DIR/backend/foo.sh"
RESULT="$(classify 5)"
[[ "$RESULT" == "True" ]]
SH
  chmod +x "$dir/tests/check.sh"
  git -C "$dir" add -A
  git -C "$dir" commit -qm "base"
  printf '%s\n' "$dir"
}

new_repo_broken_baseline() {
  local dir="$SCRATCH/$1"
  mkdir -p "$dir/backend" "$dir/tests"
  git -C "$dir" init -q -b main
  git -C "$dir" config user.email "fixture@example.invalid"
  git -C "$dir" config user.name "fixture"
  _write_foo "$dir"
  cat >"$dir/tests/check.sh" <<'SH'
#!/usr/bin/env bash
exit 1
SH
  chmod +x "$dir/tests/check.sh"
  git -C "$dir" add -A
  git -C "$dir" commit -qm "base (already-broken test)"
  printf '%s\n' "$dir"
}

run_gate() {
  local dir="$1" body="$2"
  # unset PR_BODY_FILE: the gate prefers PR_BODY_FILE over PR_BODY when both
  # are set, and PR_BODY_FILE can be inherited from this suite's own caller's
  # environment (concretely: when this suite itself is invoked as the
  # Command: in a real PR's Mutation evidence block, the outer gate run sets
  # PR_BODY_FILE for its own `bash -c "$COMMAND"` call, and that variable is
  # then inherited all the way down into every fixture invocation here,
  # silently overriding the per-case PR_BODY below with the OUTER claim's
  # body). Without this every fixture would test the wrong input under
  # exactly that condition — the same shape of defect this file exists to
  # catch, just relocated into the test harness itself.
  ( cd "$dir" && unset PR_BODY_FILE && PR_BODY="$body" bash "$GATE" 2>&1 )
}

assert_reverted() {
  local dir="$1" label="$2"
  if ( cd "$dir" && git diff --quiet ); then
    pass "$label: checkout reverted clean (git diff --quiet exits 0)"
  else
    fail "$label: checkout left dirty — patch was not reverted"
  fi
}

# A mutation that changes classify()'s outcome for x=5 — kills the test.
REPRODUCING_DIFF='```diff
--- a/backend/foo.sh
+++ b/backend/foo.sh
@@ -1,6 +1,6 @@
 classify() {
   local x="$1"
-  if (( x > 0 )); then
+  if (( x < 0 )); then
     echo "True"
   else
     echo "False"
```'

# A mutation that does NOT change the outcome for x=5 — the test cannot
# distinguish it. This is the shape the whole check exists for (D#1942).
SURVIVING_DIFF='```diff
--- a/backend/foo.sh
+++ b/backend/foo.sh
@@ -1,6 +1,6 @@
 classify() {
   local x="$1"
-  if (( x > 0 )); then
+  if (( x >= 0 )); then
     echo "True"
   else
     echo "False"
```'

# Context lines that do not match the fixture's real file — git apply must
# refuse this outright.
NONAPPLYING_DIFF='```diff
--- a/backend/foo.sh
+++ b/backend/foo.sh
@@ -1,6 +1,6 @@
 classify() {
   local x="$1"
-  if (( x > 999 )); then
+  if (( x < 999 )); then
     echo "True"
   else
     echo "False"
```'

# ---------------------------------------------------------------------------
# TC-1 (item 4) — no block at all: exit 0, names what happened.
# ---------------------------------------------------------------------------
echo "=== TC-1 (item 4): no mutation evidence block ==="
repo="$(new_repo tc1)"
out="$(run_gate "$repo" "Just a description, no mutation evidence block here.")"
rc=$?
if [[ $rc -ne 0 ]]; then
  fail "no-block body should exit 0, got $rc: $out"
elif ! printf '%s' "$out" | grep -Fq "no mutation evidence block"; then
  fail "no-block body exited 0 but did not name it: $out"
else
  pass "no-block body exits 0 and names the reason"
fi

# ---------------------------------------------------------------------------
# TC-2 (item 5) — patch does not apply: exit 1, names it, does not run the
# command at all (the fixture's check.sh would pass if it were run, so a
# false PASS here would mean it fell through).
# ---------------------------------------------------------------------------
echo "=== TC-2 (item 5): patch does not apply ==="
repo="$(new_repo tc2)"
body="## Mutation evidence

Host shape: fresh clone (CI runner)
Command: bash tests/check.sh

$NONAPPLYING_DIFF
"
out="$(run_gate "$repo" "$body")"
rc=$?
if [[ $rc -ne 1 ]]; then
  fail "non-applying patch should exit 1, got $rc: $out"
elif ! printf '%s' "$out" | grep -Fq "patch did not apply"; then
  fail "non-applying patch failed but did not say why: $out"
else
  pass "non-applying patch fails and names it"
fi
assert_reverted "$repo" "TC-2"

# ---------------------------------------------------------------------------
# TC-3 (item 6) — baseline is not green: exit 1, names it. Uses the
# already-reproducing diff to prove the failure is about the BASELINE, not
# the patch.
# ---------------------------------------------------------------------------
echo "=== TC-3 (item 6): baseline is not green ==="
repo="$(new_repo_broken_baseline tc3)"
body="## Mutation evidence

Host shape: fresh clone (CI runner)
Command: bash tests/check.sh

$REPRODUCING_DIFF
"
out="$(run_gate "$repo" "$body")"
rc=$?
if [[ $rc -ne 1 ]]; then
  fail "red-baseline claim should exit 1, got $rc: $out"
elif ! printf '%s' "$out" | grep -Fq "baseline is not green"; then
  fail "red-baseline claim failed but did not say why: $out"
else
  pass "a claim attached to an already-broken suite fails and names it"
fi
assert_reverted "$repo" "TC-3"

# ---------------------------------------------------------------------------
# TC-4 (item 7) — mutant survived: exit 1, names it, names the command. This
# is D#1942's shape: the patch applies, the baseline is green, but the
# command does not distinguish clean from mutated.
# ---------------------------------------------------------------------------
echo "=== TC-4 (item 7): mutant survived ==="
repo="$(new_repo tc4)"
body="## Mutation evidence

Host shape: fresh clone (CI runner)
Command: bash tests/check.sh

$SURVIVING_DIFF
"
out="$(run_gate "$repo" "$body")"
rc=$?
if [[ $rc -ne 1 ]]; then
  fail "surviving mutant should exit 1, got $rc: $out"
elif ! printf '%s' "$out" | grep -Fq "mutant survived"; then
  fail "surviving mutant failed but did not say why: $out"
elif ! printf '%s' "$out" | grep -Fq "bash tests/check.sh"; then
  fail "surviving mutant failure did not name the command: $out"
else
  pass "a surviving mutant fails, names it, and names the command"
fi
assert_reverted "$repo" "TC-4"

# ---------------------------------------------------------------------------
# TC-5 (item 8) — the claim reproduces: exit 0.
# ---------------------------------------------------------------------------
echo "=== TC-5 (item 8): claim reproduces (green clean, red patched) ==="
repo="$(new_repo tc5)"
body="## Mutation evidence

Host shape: fresh clone (CI runner)
Command: bash tests/check.sh

$REPRODUCING_DIFF
"
out="$(run_gate "$repo" "$body")"
rc=$?
if [[ $rc -ne 0 ]]; then
  fail "a reproducing claim should exit 0, got $rc: $out"
else
  pass "a reproducing claim exits 0"
fi
assert_reverted "$repo" "TC-5"

# ---------------------------------------------------------------------------
# TC-6/7/8 (item 10) — Command: absent, empty, or not on the allowlist. None
# of these may be treated as a vacuous pass.
# ---------------------------------------------------------------------------
echo "=== TC-6/7/8 (item 10): rejected commands ==="

repo="$(new_repo tc6-absent)"
body="## Mutation evidence

Host shape: fresh clone (CI runner)

$REPRODUCING_DIFF
"
out="$(run_gate "$repo" "$body")"
rc=$?
if [[ $rc -ne 1 ]]; then
  fail "absent Command: should exit 1, got $rc: $out"
elif ! printf '%s' "$out" | grep -Fq "rejected command"; then
  fail "absent Command: failed but did not name the rejection: $out"
else
  pass "absent Command: line is rejected, not a vacuous pass"
fi

repo="$(new_repo tc7-empty)"
body="## Mutation evidence

Host shape: fresh clone (CI runner)
Command:

$REPRODUCING_DIFF
"
out="$(run_gate "$repo" "$body")"
rc=$?
if [[ $rc -ne 1 ]]; then
  fail "empty Command: should exit 1, got $rc: $out"
elif ! printf '%s' "$out" | grep -Fq "rejected command"; then
  fail "empty Command: failed but did not name the rejection: $out"
else
  pass "empty Command: is rejected, not a vacuous pass"
fi

repo="$(new_repo tc8-disallowed)"
body="## Mutation evidence

Host shape: fresh clone (CI runner)
Command: rm -rf /

$REPRODUCING_DIFF
"
out="$(run_gate "$repo" "$body")"
rc=$?
if [[ $rc -ne 1 ]]; then
  fail "a non-allowlisted command should exit 1, got $rc: $out"
elif ! printf '%s' "$out" | grep -Fq "rejected command"; then
  fail "non-allowlisted command failed but did not name the rejection: $out"
elif ! printf '%s' "$out" | grep -Fq "rm -rf /"; then
  fail "non-allowlisted command failure did not name the rejected command: $out"
else
  pass "a non-allowlisted command is rejected and named"
fi

# ---------------------------------------------------------------------------
# TC-9 — sanity: a wiring error (neither PR_BODY_FILE nor PR_BODY set) is
# exit 2, distinguishable from every data-shaped outcome above.
# ---------------------------------------------------------------------------
echo "=== TC-9: no input at all is a wiring error ==="
repo="$(new_repo tc9)"
# Explicitly unset both — see run_gate's comment above for why an inherited
# PR_BODY_FILE from this suite's own caller cannot be trusted to be absent.
out="$( cd "$repo" && unset PR_BODY_FILE PR_BODY && bash "$GATE" 2>&1 )"
rc=$?
if [[ $rc -ne 2 ]]; then
  fail "no PR_BODY/PR_BODY_FILE at all should exit 2, got $rc: $out"
else
  pass "no input at all exits 2 — distinguishable from a real empty/absent claim"
fi

echo
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
