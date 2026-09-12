#!/usr/bin/env bash
# tests/test_check_subsystems_index.sh — tests for scripts/check-subsystems-index.sh (D#2573)
#
# Run: bash tests/test_check_subsystems_index.sh
# Expects: all assertions pass, exit 0
#
# check-subsystems-index.sh locates its own repo root relative to its own
# file path (SCRIPT_DIR/BASH_SOURCE), not an argument or env var, so the only
# way to exercise it against a synthetic tree is to give it a synthetic repo
# root to sit in. Each fixture below is a throwaway `mktemp -d` tree with its
# own scripts/, backend/, and (optionally) wiki/ directories, and the script
# UNDER TEST is copied into it byte-for-byte — never reimplemented, never
# summarized into a heredoc. That is what SCRIPT_UNDER_TEST controls: it
# defaults to the real shipping script, so `bash tests/test_check_subsystems_index.sh`
# with no override runs every fixture against the actual file this PR ships.
#
# The base-vs-head differential required by this Discussion (D#1984, D#2149 —
# a fix must be shown failing against the unmodified script, not just passing
# against the fixed one) is driven the same way, from outside this file:
#
#   SCRIPT_UNDER_TEST=/path/to/base/check-subsystems-index.sh \
#     bash tests/test_check_subsystems_index.sh
#
# Never a fixed /tmp path (scripts/check-tests-fixed-tmp-paths.sh) — every
# fixture is its own mktemp -d, cleaned up on exit.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_UNDER_TEST="${SCRIPT_UNDER_TEST:-$REAL_REPO_ROOT/scripts/check-subsystems-index.sh}"

if [ ! -f "$SCRIPT_UNDER_TEST" ]; then
  echo "FAIL: SCRIPT_UNDER_TEST not found: $SCRIPT_UNDER_TEST" >&2
  exit 1
fi

PASS=0
FAIL=0
FIXTURES=()

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; [[ $# -gt 1 ]] && echo "        $2"; FAIL=$((FAIL + 1)); }

assert_eq()       { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected [$3], got [$2]"; fi; }
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain [$2], got: $3"; fi
}
assert_not_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then bad "$1" "expected NOT to contain [$2], got: $3"; else ok "$1"; fi
}

# ── fixture helper ────────────────────────────────────────────────────────────
#
# mk_fixture <backend-file...> -- copies SCRIPT_UNDER_TEST into a fresh
# mktemp -d tree at <dir>/scripts/check-subsystems-index.sh and creates
# <dir>/backend/<name> for each argument (touch — content doesn't matter,
# only the filename does). wiki/ is added separately per case since its
# shape differs across the five states. Prints the fixture root on stdout.
mk_fixture() {
  local dir
  dir="$(mktemp -d)" || { echo "mktemp -d failed" >&2; return 1; }
  FIXTURES+=("$dir")
  mkdir -p "$dir/scripts" "$dir/backend"
  cp "$SCRIPT_UNDER_TEST" "$dir/scripts/check-subsystems-index.sh"
  local f
  for f in "$@"; do
    : > "$dir/backend/$f"
  done
  printf '%s\n' "$dir"
}

run_check() {
  # run_check <fixture-dir> -- runs the script and sets RC, OUT, ERR.
  # stdout and stderr are captured separately via a private mktemp file
  # (never a fixed /tmp path) rather than merging them with 2>&1.
  local dir="$1" errfile
  errfile="$(mktemp)"
  FIXTURES+=("$errfile")
  OUT="$(bash "$dir/scripts/check-subsystems-index.sh" 2>"$errfile")"
  RC=$?
  ERR="$(cat "$errfile")"
}

echo "== case 1: state 1 -- wiki/ absent =="
F1="$(mk_fixture a.py)"
run_check "$F1"
assert_eq "case1 exit code is 0"    "$RC" "0"
assert_contains "case1 stdout names wiki/ absent" "wiki/ absent" "$OUT"

echo "== case 2: state 2 -- wiki/ present, no index (the live defect) =="
F2="$(mk_fixture a.py)"
mkdir -p "$F2/wiki"
: > "$F2/wiki/Gate-1-Containment-Runbook.md"
run_check "$F2"
assert_eq "case2 exit code is 0" "$RC" "0"
assert_not_contains "case2 stdout+stderr has no [FAIL]" "[FAIL]" "$OUT$ERR"
assert_not_contains "case2 message is distinct from state-1's" "wiki/ absent" "$OUT"

echo "== case 3: state 3, stale -- index missing a module =="
F3="$(mk_fixture a.py b.py)"
mkdir -p "$F3/wiki"
cat > "$F3/wiki/Subsystems-Index.md" <<'EOF'
# Subsystems Index

| Module | Owner |
|---|---|
| `a.py` | team |
EOF
run_check "$F3"
assert_eq "case3 exit code is 1" "$RC" "1"
assert_contains "case3 stderr names b.py" "b.py" "$ERR"

echo "== case 4: state 3, current -- index covers every module =="
F4="$(mk_fixture a.py b.py)"
mkdir -p "$F4/wiki"
cat > "$F4/wiki/Subsystems-Index.md" <<'EOF'
# Subsystems Index

| Module | Owner |
|---|---|
| `a.py` | team |
| `b.py` | team |
EOF
run_check "$F4"
assert_eq "case4 exit code is 0" "$RC" "0"
assert_contains "case4 stdout contains [PASS]" "[PASS]" "$OUT"

echo "== case 5: zero subjects -- index present, no backend/*.py at all =="
F5="$(mk_fixture)"
mkdir -p "$F5/wiki"
cat > "$F5/wiki/Subsystems-Index.md" <<'EOF'
# Subsystems Index
EOF
run_check "$F5"
assert_eq "case5 exit code is 1" "$RC" "1"
# Exit code alone doesn't prove the deliberate branch ran — the unpatched
# module-discovery pipeline crashes to exit 1 on its own (xargs invoking
# basename on empty stdin, or grep -v selecting zero lines), via pipefail +
# set -e, before the `[ -z "$MODULES" ]` check is ever reached. Assert the
# message text so a regression that re-breaks reachability (e.g. dropping
# the `|| true`) shows up here instead of hiding behind a coincidentally
# correct exit code.
assert_contains "case5 stderr names the broken-glob failure" "No backend/*.py modules found" "$ERR"

echo ""
echo "== summary =="
echo "pass=$PASS fail=$FAIL script_under_test=$SCRIPT_UNDER_TEST"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
