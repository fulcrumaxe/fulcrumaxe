#!/usr/bin/env bash
# tests/test_subshell_mutation_guard.sh — fixtures for D#2512's Spec
# acceptance items against the REAL script, on throwaway scratch trees. The
# three historical shapes are reconstructed here rather than pointed at the
# live files that carried them, since all three are already fixed on this
# tree (Spec item 1).
#
# Run: bash tests/test_subshell_mutation_guard.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Moved from scripts/ci/ to scripts/lib/ in the D#2512 fix round: the raw
# scanner is now invoked by scripts/ci/subshell-mutation-ratchet.py rather
# than running standalone, so it has to sit somewhere
# scripts/ci/run-guards.sh's maxdepth-1 discovery does not reach — see that
# file's own header comment.
GUARD="$REPO_ROOT/scripts/lib/subshell-mutation-scan.sh"

if [[ ! -f "$GUARD" ]]; then
  echo "FAIL: $GUARD is missing — the guard this suite tests does not exist"
  exit 1
fi

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# ---------------------------------------------------------------------------
# TC-1 — historical case 1: export inside a function, no local (CLAUDE.md /
# tests/lib/blackboard-fixture.sh's blackboard_scratch_state_dir).
# ---------------------------------------------------------------------------
echo "=== TC-1: export-without-local mutation (blackboard_scratch_state_dir shape) ==="
dir="$SCRATCH/hist1"
mkdir -p "$dir"
cat >"$dir/state-fixture.sh" <<'SH'
scratch_state_dir() {
  local dir
  dir="$(mktemp -d)" || return 1
  export SCRATCH_STATE_DIR="$dir"
}

use_it() {
  local out
  out="$(scratch_state_dir)"
  echo "$out"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 1 ]]; then
  fail "expected exit 1, got $rc — output: $out"
elif ! grep -q "state-fixture.sh:9" <<<"$out" || ! grep -q "scratch_state_dir" <<<"$out" || ! grep -q "SCRATCH_STATE_DIR" <<<"$out"; then
  fail "output did not name the call-site file/line and the mutated variable: $out"
else
  pass "flags the call site, naming file, line, function and mutated variable"
  echo "    $(grep '^FAIL:' <<<"$out")"
fi

# ---------------------------------------------------------------------------
# TC-2 — historical case 2: bare global reassignment (coldstart-backlog.sh's
# refusal-reason shape).
# ---------------------------------------------------------------------------
echo "=== TC-2: bare-assignment mutation (coldstart-backlog refusal-reason shape) ==="
dir="$SCRATCH/hist2"
mkdir -p "$dir"
cat >"$dir/ask-importer.sh" <<'SH'
_REFUSAL_REASON=""

_ask_importer() {
  local target="$1"
  _REFUSAL_REASON=""
  if [[ ! -d "$target" ]]; then
    _REFUSAL_REASON="importer target vanished: $target"
    return 1
  fi
  echo "ok"
}

check_backlog() {
  local status
  status="$(_ask_importer "/no/such/dir")"
  echo "status=$status reason=$_REFUSAL_REASON"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 1 ]]; then
  fail "expected exit 1, got $rc — output: $out"
elif ! grep -q "ask-importer.sh:15" <<<"$out" || ! grep -q "_ask_importer" <<<"$out" || ! grep -q "_REFUSAL_REASON" <<<"$out"; then
  fail "output did not name the call-site file/line, function, and mutated variable: $out"
else
  pass "flags the call site for a bare non-local global reassignment"
  echo "    $(grep '^FAIL:' <<<"$out")"
fi

# ---------------------------------------------------------------------------
# TC-3 — historical case 3: array append (mkfixture shape), both $(...) and
# backtick call sites (Spec item 5 — backtick form covered).
# ---------------------------------------------------------------------------
echo "=== TC-3: array-append mutation (mkfixture shape), \$(...) and backtick forms ==="
dir="$SCRATCH/hist3"
mkdir -p "$dir"
cat >"$dir/mkfixture.sh" <<'SH'
mkfixture() { local d; d="$(mktemp -d)"; FIXTURES+=("$d"); echo "$d"; }

use_fixtures() {
  local a b
  a="$(mkfixture)"
  b=`mkfixture`
  echo "$a $b"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 1 ]]; then
  fail "expected exit 1, got $rc — output: $out"
else
  n_matches="$(grep -c '^FAIL:.*mkfixture' <<<"$out" || true)"
  has_dollar_paren="$(grep -c '^FAIL: mkfixture.sh:5:' <<<"$out" || true)"
  has_backtick="$(grep -c '^FAIL: mkfixture.sh:6:' <<<"$out" || true)"
  if [[ "$n_matches" -ne 2 || "$has_dollar_paren" -ne 1 || "$has_backtick" -ne 1 ]]; then
    fail "expected exactly one \$(...) finding at line 5 and one backtick finding at line 6, got: $out"
  else
    pass "flags array-append mutation via both \$(...) and backtick call sites"
    grep '^FAIL:' <<<"$out" | sed 's/^/    /'
  fi
fi

# ---------------------------------------------------------------------------
# TC-4 — false-positive floor (Spec item 2, all three sub-cases in one run):
#   - a function whose assignments are all local
#   - a call site written as a direct invocation, not a capture
#   - a function whose only global mutation is a log-only counter increment
# ---------------------------------------------------------------------------
echo "=== TC-4: false-positive floor ==="
dir="$SCRATCH/fp"
mkdir -p "$dir"
cat >"$dir/fp.sh" <<'SH'
_pure_helper() {
  local x
  x="hello"
  local -a arr
  arr+=(1 2 3)
  echo "$x ${arr[*]}"
}

_log_only() {
  ((SEEN_COUNT++))
  echo "logged"
}

_mutates_but_called_directly() {
  local v
  v="1"
  GLOBAL_FLAG="set"
}

use_fp() {
  local h l
  h="$(_pure_helper)"
  l="$(_log_only)"
  _mutates_but_called_directly
  echo "$h $l"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 0 ]]; then
  fail "false-positive floor: expected exit 0, got $rc — output: $out"
else
  pass "all-local function, log-only counter, and direct invocation of a mutating function are all unflagged"
fi

# Companion check: prove _mutates_but_called_directly's mutation is genuinely
# detected (not silently missed) — capture it via \$(...) in a *different*
# fixture and confirm it DOES flag. Otherwise TC-4's clean exit could mean
# "the guard never noticed GLOBAL_FLAG=" rather than "direct calls are exempt".
dir2="$SCRATCH/fp-companion"
mkdir -p "$dir2"
cat >"$dir2/fp-companion.sh" <<'SH'
_mutates_but_called_directly() {
  local v
  v="1"
  GLOBAL_FLAG="set"
}

use_it_captured() {
  local out
  out="$(_mutates_but_called_directly)"
  echo "$out"
}
SH
out2="$(bash "$GUARD" "$dir2" 2>&1)"; rc2=$?
if [[ $rc2 -ne 1 ]] || ! grep -q "GLOBAL_FLAG" <<<"$out2"; then
  fail "companion check: _mutates_but_called_directly should flag when actually captured — got rc=$rc2: $out2"
else
  pass "companion check: the same mutation DOES flag once actually captured — proves TC-4 exempts by call shape, not by missed detection"
fi

# ---------------------------------------------------------------------------
# TC-5 — mutation check, both directions (Spec item 4): introduce the shape
# into a scratch fixture and confirm non-zero naming file/line/function;
# remove it and confirm exit 0.
# ---------------------------------------------------------------------------
echo "=== TC-5: toggle — introduce the shape, then remove it ==="
dir="$SCRATCH/toggle"
mkdir -p "$dir"
cat >"$dir/toggle.sh" <<'SH'
helper() {
  local x
  x="1"
  echo "$x"
}

caller() {
  local out
  out="$(helper)"
  echo "$out"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 0 ]]; then
  fail "toggle baseline (no mutation) should be exit 0, got $rc: $out"
else
  pass "toggle baseline: clean fixture exits 0"
fi

cat >"$dir/toggle.sh" <<'SH'
helper() {
  local x
  x="1"
  GLOBAL_STATE="mutated"
  echo "$x"
}

caller() {
  local out
  out="$(helper)"
  echo "$out"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 1 ]] || ! grep -q "toggle.sh:10" <<<"$out" || ! grep -q "helper" <<<"$out" || ! grep -q "GLOBAL_STATE" <<<"$out"; then
  fail "toggle introduced: expected exit 1 naming file/line/function/var, got rc=$rc: $out"
else
  pass "toggle introduced: exit 1, naming the call site and the mutation"
fi

cat >"$dir/toggle.sh" <<'SH'
helper() {
  local x
  x="1"
  echo "$x"
}

caller() {
  local out
  out="$(helper)"
  echo "$out"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 0 ]]; then
  fail "toggle removed: expected exit 0 again, got $rc: $out"
else
  pass "toggle removed: back to exit 0"
fi

# ---------------------------------------------------------------------------
# TC-6 — usage: zero *.sh files under target-dir is a failure, not a pass.
# ---------------------------------------------------------------------------
echo "=== TC-6: empty target dir is exit 2, not a silent pass ==="
dir="$SCRATCH/empty"
mkdir -p "$dir"
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 2 ]]; then
  fail "empty target dir should exit 2, got $rc: $out"
else
  pass "empty target dir exits 2 rather than silently reporting clean"
fi

# ---------------------------------------------------------------------------
# TC-7 — a mutation fully inside an explicit `( ... )` subshell grouping is
# already contained regardless of how the function is called, and must not
# flag (real shape: tests/test_ci_status_check.sh's _run_status()).
# ---------------------------------------------------------------------------
echo "=== TC-7: mutation inside an explicit ( ... ) grouping is not flagged ==="
dir="$SCRATCH/subshell-group"
mkdir -p "$dir"
cat >"$dir/group.sh" <<'SH'
run_isolated() {
  local arg="$1"
  (
    GLOBAL_STATE="mutated by the inner subshell only"
    echo "arg=$arg"
  )
}

caller() {
  local out
  out="$(run_isolated "x")"
  echo "$out"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 0 ]]; then
  fail "mutation inside ( ... ) should not flag, got rc=$rc: $out"
else
  pass "mutation inside an explicit ( ... ) grouping stays unflagged"
fi

# ---------------------------------------------------------------------------
# TC-8 — a per-command environment prefix (`VAR=val cmd`, single line or
# backslash-continued) is not a mutation of the function's own state.
# ---------------------------------------------------------------------------
echo "=== TC-8: env-prefix assignment before a command is not a mutation ==="
dir="$SCRATCH/env-prefix"
mkdir -p "$dir"
cat >"$dir/prefix.sh" <<'SH'
run_job() {
  PATH="$MOCK_BIN:$PATH" \
  REPO_ROOT="$TMPDIR_TEST" \
      bash "$JOB_SCRIPT" > "$OUT_FILE" 2>&1
  echo $?
}

caller() {
  local rc
  rc="$(run_job)"
  echo "$rc"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 0 ]]; then
  fail "env-prefix assignment before a command should not flag, got rc=$rc: $out"
else
  pass "PATH/REPO_ROOT env-prefix lines are not mistaken for mutations"
fi

# ---------------------------------------------------------------------------
# TC-9 — a function name defined more than once in the tree is dropped
# entirely, in both directions: neither definition is treated as mutating
# for the purpose of flagging a call to it (real shape: `new_fixture`,
# redefined independently in 9 different test files in this tree).
# ---------------------------------------------------------------------------
echo "=== TC-9: a same-named function defined in two files is never flagged ==="
dir="$SCRATCH/collision"
mkdir -p "$dir"
cat >"$dir/a.sh" <<'SH'
new_fixture() {
  local d
  d="$(mktemp -d)"
  FIXTURES_A+=("$d")
  echo "$d"
}

use_a() {
  local out
  out="$(new_fixture)"
  echo "$out"
}
SH
cat >"$dir/b.sh" <<'SH'
new_fixture() {
  local d
  d="hello"
  echo "$d"
}
SH
out="$(bash "$GUARD" "$dir" 2>&1)"; rc=$?
if [[ $rc -ne 0 ]]; then
  fail "a same-named function defined twice should not be flagged either way, got rc=$rc: $out"
else
  pass "a function name with more than one definition in the tree is excluded from both directions"
fi

echo
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
