#!/usr/bin/env bash
# tests/test_run_guards.sh — exercises scripts/ci/run-guards.sh (D#2339 PR-b).
#
# The runner replaced nineteen per-guard steps in ci.yml's `backend
# (import-smoke)` job with one step. That removes the shared insertion point
# those nineteen steps were conflicting on, and it concentrates the risk: the
# properties below are the ones that, if lost, would make the runner worse
# than the steps it replaced rather than better.
#
#   - a failing guard is named, and named on the last line
#   - two failing guards are both named, in one run
#   - a passing run does not hide a failing guard behind it (every guard runs)
#   - discovering nothing FAILS; it does not report all-clear
#   - a file the runner cannot dispatch FAILS; it is not skipped past
#   - a ledgered file is announced, with its reason, rather than just absent
#
# Every case runs the real runner against a throwaway guards directory built
# here. --dir changes which directory is listed and nothing else, so these are
# the same discovery, dispatch, reporting and exit-code paths a CI run takes.
#
# Usage: bash tests/test_run_guards.sh — exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$REPO_ROOT/scripts/ci/run-guards.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

if [ ! -f "$RUNNER" ]; then
  echo "FAIL: runner not found: $RUNNER" >&2
  exit 1
fi

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

OUT=""
RC=0

# run <dir> [args...] — run the real runner against <dir>, capture rc + output.
run() {
  local dir="$1"; shift
  OUT="$(bash "$RUNNER" --dir "$dir" "$@" 2>&1)"
  RC=$?
}

expect_rc() {
  local label="$1" want="$2"
  if [ "$RC" = "$want" ]; then pass "$label"; else fail "$label" "expected exit $want, got $RC — output: $(printf '%s' "$OUT" | tr '\n' ' ')"; fi
}

expect_out() {
  local label="$1" want="$2"
  if [[ "$OUT" == *"$want"* ]]; then pass "$label"; else fail "$label" "output never contained '$want' — output: $(printf '%s' "$OUT" | tr '\n' ' ')"; fi
}

expect_last_line() {
  local label="$1" want="$2" last
  last="$(printf '%s' "$OUT" | tail -1)"
  if [[ "$last" == *"$want"* ]]; then pass "$label"; else fail "$label" "last line was '$last', wanted it to name '$want'"; fi
}

mkguards() { local d="$TMPROOT/$1"; mkdir -p "$d"; echo "$d"; }
ok_py()   { printf '#!/usr/bin/env python3\nprint("%s ran")\n' "$2" > "$1/$2"; }
bad_py()  { printf '#!/usr/bin/env python3\nimport sys\nprint("%s ran")\nsys.exit(1)\n' "$2" > "$1/$2"; }
bad_sh()  { printf '#!/usr/bin/env bash\necho "%s ran"\nexit 3\n' "$2" > "$1/$2"; }

echo "== run-guards =="

# 1. All green.
D="$(mkguards allgreen)"
ok_py "$D" a-guard.py
ok_py "$D" b-guard.py
printf '#!/usr/bin/env bash\necho "c-guard.sh ran"\n' > "$D/c-guard.sh"
run "$D"
expect_rc  "all-passing run exits 0" 0
expect_out "each guard gets a PASS line" "PASS a-guard.py"
expect_out "shell guards are dispatched too" "PASS c-guard.sh"
expect_last_line "a clean run says so on its last line" "run-guards: OK"

# 2. One failure — named, and named last.
D="$(mkguards onefail)"
ok_py  "$D" a-guard.py
bad_py "$D" b-guard.py
ok_py  "$D" c-guard.py
run "$D"
expect_rc  "one failing guard fails the run" 1
expect_out "the failing guard gets its own FAIL line" "FAIL b-guard.py"
expect_last_line "the last line names the failing guard" "b-guard.py"
expect_out "guards after the failure still ran" "PASS c-guard.py"

# 3. Two failures — both named, one run. Per-guard attribution is the point:
#    an aggregate "guards failed" would be worse than nineteen steps.
D="$(mkguards twofail)"
ok_py  "$D" a-guard.py
bad_py "$D" b-guard.py
bad_sh "$D" d-guard.sh
run "$D"
expect_rc  "two failing guards fail the run" 1
expect_last_line "the last line names the first failure" "b-guard.py"
expect_last_line "the last line names the second failure too" "d-guard.sh"
expect_out "a non-1 exit code is reported as itself" "exit=3"

# 4. Empty discovery is a FAILURE. A runner that finds nothing and exits 0 is
#    the silent skip D#2339 exists to remove — it would report every guard
#    fine while running none of them.
D="$(mkguards empty)"
run "$D"
expect_rc  "an empty guards directory fails" 1
expect_out "and says discovery was empty" "discovered zero guards"

# 5. A file the runner cannot dispatch fails, by name. Not skipped: a file
#    nobody can run is a guard nobody runs.
D="$(mkguards undispatchable)"
ok_py "$D" a-guard.py
printf 'notes, not a guard\n' > "$D/README.txt"
run "$D"
expect_rc  "an undispatchable file fails the run" 1
expect_out "and is named" "README.txt"
expect_out "with the two ways to resolve it" "no runnable extension"

# 6. Ledgered files are excluded — and announced, with the reason. The check
#    a machine can make is in guard-registry-check.py; this is the part only a
#    human reading the log can make, so the log has to carry it.
D="$(mkguards ledgered)"
ok_py "$D" a-guard.py
ok_py "$D" local-tool.py
cat > "$D/guard-ledger.json" <<'JSON'
{"exempt": {"local-tool.py": "run by hand on a dev host"}, "own_step": {}}
JSON
run "$D"
expect_rc  "a ledgered file does not break the run" 0
expect_out "the ledgered file is announced with its reason" "SKIP local-tool.py — ledgered 'exempt': run by hand on a dev host"
expect_out "and counted in the summary" "1 ledgered as not-run"
if [[ "$OUT" == *"PASS local-tool.py"* ]]; then
  fail "a ledgered file is not run" "it produced a PASS line"
else
  pass "a ledgered file is not run"
fi

# 7. --list prints the discovered set and a count that matches it.
run "$D" --list
expect_rc  "--list exits 0" 0
expect_out "--list names the discovered guard" "a-guard.py"
expect_out "--list counts what it printed" "count: 1"
if [[ "$OUT" == *"local-tool.py"* ]]; then
  fail "--list excludes ledgered files" "local-tool.py appeared"
else
  pass "--list excludes ledgered files"
fi

# 8. The runner never lists itself — it cannot run itself, and a copy of it
#    inside a guards directory must not recurse.
D="$(mkguards selfname)"
ok_py "$D" a-guard.py
cp "$RUNNER" "$D/run-guards.sh"
run "$D" --list
expect_rc  "a directory containing the runner still lists" 0
expect_out "and finds the real guard" "a-guard.py"
expect_out "and counts only it" "count: 1"

# 9. Usage errors are exit 2, distinct from a guard failure.
OUT="$(bash "$RUNNER" --nonsense 2>&1)"; RC=$?
expect_rc "an unknown flag exits 2" 2
OUT="$(bash "$RUNNER" --dir 2>&1)"; RC=$?
expect_rc "--dir with no argument exits 2" 2

# 10. The real guards directory, run the way ci.yml runs it. Exit code is
#     deliberately not asserted: a guard may legitimately fail here on a
#     developer host that CI's checkout satisfies. What must hold is that the
#     runner discovers the real set and reports per-guard, which is what the
#     job's one step depends on.
OUT="$(bash "$RUNNER" --list 2>&1)"; RC=$?
expect_rc  "the real guards directory lists" 0
REAL_COUNT="$(printf '%s' "$OUT" | sed -n 's/^count: //p')"
if [ "${REAL_COUNT:-0}" -gt 0 ] 2>/dev/null; then
  pass "the real guards directory is not empty (count: $REAL_COUNT)"
else
  fail "the real guards directory is not empty" "count was '${REAL_COUNT:-}'"
fi

echo ""
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
