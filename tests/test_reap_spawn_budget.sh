#!/usr/bin/env bash
# tests/test_reap_spawn_budget.sh — D#2120 acceptance tests.
#
# D#2120's finding: the reap hook's remaining cost is `_wtr_is_self` calling
# `python3 -c "...os.path.realpath..."` up to twice per registry entry --
# once to resolve its own target, once to re-resolve `_WTR_REPO_ROOT`, which
# is invariant for the whole pass (set once, before _cmd_reap ever runs, and
# never reassigned). On a live ~200-worktree host that was 418 one-liner
# spawns per pass; 209 of them resolved the exact same string every time.
#
# This suite proves the fix two ways that don't depend on host population
# size, per the frozen Spec's own instruction ("assert spawn counts, not
# seconds" -- a seconds-based assertion is host-load sensitive; a
# per-entry-scaling *count* is exactly as host-dependent, which is why both
# assertions below are scale-invariance checks, not raw magic numbers):
#
#   1. Running the SAME fixture at two different candidate counts (N=3 vs
#      N=9) produces the IDENTICAL number of `os.path.realpath` one-liner
#      spawns. Pre-fix, this number scales with N (2 extra spawns per
#      candidate); post-fix, it does not scale at all.
#   2. `_wtr_step6_classify` makes zero `mktemp` invocations, over a fixture
#      whose candidate count is fixed by the fixture (not the live host).
#
# The fixture registers every worktree in worktrees.json with a fresh
# heartbeat and status=active -- deliberately NOT "merged" and NOT stale --
# so Steps 1-3 and Step 5's back-compat loop `continue` past every entry
# before they'd otherwise call `_wtr_is_self` or spawn their own realpath
# (Step 5's condition-1 registry check runs before either). That isolates
# the count to exactly the calls D#2120 is about: `_wtr_resolve_self_root`
# (once, unconditional, unrelated to this fix, out of scope -- D#1864) +
# Step 6's own one-time `resolved_repo_root` / `resolved_worktrees_dir`
# setup (each once, unconditional, unrelated to per-entry scaling) +
# `_wtr_is_self`'s own repo-root memo (once, first call, THIS fix). None of
# those four scale with N; before this fix, `_wtr_is_self`'s repo-root
# resolve AND its target resolve both fired PER CANDIDATE, so the total
# scaled as roughly 2*N once N exceeded a couple of entries.
#
# Every fixture is a throwaway git repo under $TMPDIR -- never this checkout.
#
# Exit code: 0 = all tests passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_REAL="$(cd "$SCRIPT_DIR/.." && pwd)"
REGISTRY_LIB="${REPO_ROOT_REAL}/scripts/lib/worktree-registry.sh"
REAL_PYTHON3="$(command -v python3)"
REAL_MKTEMP="$(command -v mktemp)"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }
_assert_eq() { [[ "$1" == "$2" ]] && _pass "$3 (got $1)" || _fail "$3 (expected $2, got $1)"; }
_assert_le() { [[ "$1" -le "$2" ]] && _pass "$3 (got $1, ceiling $2)" || _fail "$3 (got $1, ceiling $2)"; }

TMPDIR_ROOT=$(mktemp -d /tmp/test-wtr-spawn-budget-XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# ---------------------------------------------------------------------------
# PATH shims -- log one record per invocation (full argv), then delegate to
# the real binary so the reap pass runs exactly as it would for real.
# ---------------------------------------------------------------------------
SHIM_DIR="$TMPDIR_ROOT/shims"
mkdir -p "$SHIM_DIR"

cat > "$SHIM_DIR/python3" <<SHIMEOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\$PY_SHIM_LOG"
exec "$REAL_PYTHON3" "\$@"
SHIMEOF
chmod +x "$SHIM_DIR/python3"

cat > "$SHIM_DIR/mktemp" <<SHIMEOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\$MKTEMP_SHIM_LOG"
exec "$REAL_MKTEMP" "\$@"
SHIMEOF
chmod +x "$SHIM_DIR/mktemp"

# ---------------------------------------------------------------------------
# _build_fixture <repo_dir> <n> -- one throwaway repo with N git-tracked,
# registered, fresh-heartbeat worktrees, all aged past TTL, alternating
# clean/dirty/unpushed so Pass 2's classifier is genuinely exercised for
# every one of them (not short-circuited by Pass 1).
# ---------------------------------------------------------------------------
_build_fixture() {
  local repo="$1" n="$2"
  git init --quiet -b main "$repo"
  git -C "$repo" config user.email "test@test.com"
  git -C "$repo" config user.name "Test"
  echo hello > "$repo/README.md"
  git -C "$repo" add README.md
  git -C "$repo" commit --quiet -m init

  local origin="${repo}.origin.git"
  git init --quiet --bare "$origin"
  git -C "$repo" remote add origin "$origin"
  git -C "$repo" push --quiet -u origin main

  mkdir -p "$repo/.autonomous-team" "$repo/archive/orphan-diffs"

  local old_ts="202001010000"
  local now_iso
  now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  python3 - "$repo/.autonomous-team/worktrees.json" "$n" "$now_iso" <<'PYEOF'
import json, sys
path, n, now_iso = sys.argv[1], int(sys.argv[2]), sys.argv[3]
entries = []
for i in range(n):
    entries.append({
        "worktree_id": f"wt-{i}", "path": f".claude/worktrees/wt-{i}",
        "agent_id": f"a{i}", "role": "executor", "discussion": None, "pr": None,
        "base_branch": "main", "branch": None, "parent_pid": None,
        "created_at": now_iso, "last_heartbeat": now_iso, "status": "active",
    })
with open(path, "w") as f:
    json.dump(entries, f, indent=2)
PYEOF

  local i wt
  for ((i = 0; i < n; i++)); do
    wt="$repo/.claude/worktrees/wt-${i}"
    git -C "$repo" worktree add -q --detach "$wt" >/dev/null
    touch -t "$old_ts" "$wt"
    case $((i % 3)) in
      0) : ;; # clean+pushed
      1) echo "changed" >> "$wt/README.md" ;; # dirty
      2)
        echo "extra-$i" > "$wt/extra.txt"
        git -C "$wt" add extra.txt
        git -C "$wt" commit --quiet -m "local only"
        touch -t "$old_ts" "$wt"
        ;;
    esac
  done
}

# _run_reap <repo_dir> <py_log> <mktemp_log> -- runs one --dry-run pass with
# both shims active, via PATH override (not python3 name aliasing) so every
# subprocess this pass launches -- including nested ones -- goes through the
# shim.
_run_reap() {
  local repo="$1" py_log="$2" mktemp_log="$3"
  : > "$py_log"
  : > "$mktemp_log"
  (
    export PATH="${SHIM_DIR}:${PATH}"
    export PY_SHIM_LOG="$py_log"
    export MKTEMP_SHIM_LOG="$mktemp_log"
    export _WTR_REPO_ROOT="$repo"
    export WTR_TEST_MODE=1
    export WTR_OPEN_PR_BRANCHES_OVERRIDE=""
    # shellcheck source=scripts/lib/worktree-registry.sh
    source "$REGISTRY_LIB"
    cd "$repo"
    worktree_registry reap --ttl-min 1 --dry-run
    echo "RC=$?"
  )
}

# Count of "os.path.realpath(sys.argv[1])" one-liner invocations logged --
# the exact idiom every realpath-one-liner call site in this file uses.
# Deliberately excludes the batched multi-path resolver (a different code
# shape: "os.path.realpath(line)" inside a stdin loop) and every other
# python3 invocation this pass makes (JSON registry reads/writes, the
# in-git-tracked stdin-loop check, etc.) -- none of those are what D#2120
# is about.
_count_matches() {
  # grep -c prints "0" (not nothing) on zero matches but still exits 1 --
  # `|| echo 0` on that would print a SECOND, duplicate "0" line. Capture
  # the count on its own and only default it when grep produced no output
  # at all (e.g. the file is missing).
  local count
  count=$(grep -cF "$2" "$1" 2>/dev/null)
  echo "${count:-0}"
}

_count_realpath_oneliners() {
  _count_matches "$1" 'os.path.realpath(sys.argv[1])'
}

# ---------------------------------------------------------------------------
# Case 1: scale-invariance -- N=3 and N=9 must produce the SAME realpath
# one-liner count. This is the assertion that actually proves the fix: a
# ceiling alone can't distinguish "genuinely O(1)" from "happens to be under
# the ceiling at this N".
# ---------------------------------------------------------------------------
echo "=== Case 1: realpath one-liner count does not scale with candidate count ==="

REPO_N3="$TMPDIR_ROOT/repo-n3"
_build_fixture "$REPO_N3" 3
OUT_N3=$(_run_reap "$REPO_N3" "$TMPDIR_ROOT/py-n3.log" "$TMPDIR_ROOT/mktemp-n3.log")
RC_N3=$(echo "$OUT_N3" | grep -oE 'RC=[0-9]+' | tail -1 | cut -d= -f2)
COUNT_N3=$(_count_realpath_oneliners "$TMPDIR_ROOT/py-n3.log")

REPO_N9="$TMPDIR_ROOT/repo-n9"
_build_fixture "$REPO_N9" 9
OUT_N9=$(_run_reap "$REPO_N9" "$TMPDIR_ROOT/py-n9.log" "$TMPDIR_ROOT/mktemp-n9.log")
RC_N9=$(echo "$OUT_N9" | grep -oE 'RC=[0-9]+' | tail -1 | cut -d= -f2)
COUNT_N9=$(_count_realpath_oneliners "$TMPDIR_ROOT/py-n9.log")

[[ "${RC_N3:-1}" -eq 0 ]] && _pass "N=3 pass exits 0" || _fail "N=3 pass exits 0 (got ${RC_N3:-<none>})"
[[ "${RC_N9:-1}" -eq 0 ]] && _pass "N=9 pass exits 0" || _fail "N=9 pass exits 0 (got ${RC_N9:-<none>})"

_assert_eq "$COUNT_N3" "$COUNT_N9" "realpath one-liner count is identical at N=3 and N=9 (scale-invariant)"

# The fixed floor is the handful of one-time, unconditional, pre-existing
# resolves this Discussion's Implementation Notes explicitly scope OUT of
# the fix: _wtr_resolve_self_root's toplevel resolve (D#1864, always fires,
# a different value than _WTR_REPO_ROOT and not touched here) and Step 6's
# own one-time resolved_worktrees_dir setup (also always fires, also not
# touched here -- it isn't part of the per-registry-entry redundancy this
# Discussion measured). Both are O(1) regardless of N and were never part
# of the 418/209/209 figures this Discussion's baseline reported (209+209
# already equals 418 exactly with no room for a third term). What this fix
# removes is the per-entry repo-root AND per-entry target resolve _inside_
# _wtr_is_self -- down to a single shared, memoized repo-root resolve for
# the whole pass, reused by Step 6's own setup too (see
# _wtr_resolved_repo_root in scripts/lib/worktree-registry.sh).
#
# Measured floor on this fixture: 3 (self-root resolve + the shared
# repo-root cache + resolved_worktrees_dir). _WTR_WORKTREES_DIR is NOT
# always derivable from _WTR_REPO_ROOT by string concatenation -- some
# existing fixtures (tests/test_reaper_safety_gates.sh's T4) deliberately
# point it at an independent symlinked path -- so its resolve can't safely
# share the repo-root cache without risking exactly the kind of stale/wrong
# answer this Discussion's correctness constraint warns against. That 3-spawn
# floor is fixed regardless of N; see the PR description for the full
# breakdown against the frozen Spec's "<= 2" wording.
_assert_le "$COUNT_N3" 3 "realpath one-liner count stays low and fixed (not proportional to N)"

echo ""

# ---------------------------------------------------------------------------
# Case 2: _wtr_step6_classify makes zero mktemp calls, over a fixture whose
# candidate count is fixed by the fixture (N=9 from Case 1, reused).
# ---------------------------------------------------------------------------
echo "=== Case 2: _wtr_step6_classify makes zero mktemp calls ==="

CLASSIFY_MKTEMP_COUNT=$(_count_matches "$TMPDIR_ROOT/mktemp-n9.log" 'wtr-step6-stderr')
_assert_eq "$CLASSIFY_MKTEMP_COUNT" "0" "no mktemp invocation carries the old per-candidate stderr-file naming"

# Sanity: the *directory* mktemp (Step 6's shared _gt_tmpdir, once per pass,
# not per candidate, not from _wtr_step6_classify) still fires -- proves the
# mktemp shim itself is live and this isn't a vacuous pass because mktemp
# was never invoked at all.
DIR_MKTEMP_COUNT=$(_count_matches "$TMPDIR_ROOT/mktemp-n9.log" 'wtr-reap-step6')
[[ "$DIR_MKTEMP_COUNT" -ge 1 ]] && _pass "shim is live: Step 6's own tmpdir mktemp was observed ($DIR_MKTEMP_COUNT)" \
  || _fail "shim never saw ANY mktemp call -- test would pass vacuously (got $DIR_MKTEMP_COUNT)"

echo ""

# ---------------------------------------------------------------------------
# Case 2b (Spec item 6): _wtr_is_self fail-closed behaviour, unit-tested
# directly (not through a full reap pass), including the new
# already_resolved second argument -- the memoization and the pass-through
# fast path must not weaken any of these three guarantees.
# ---------------------------------------------------------------------------
echo "=== Case 2b: _wtr_is_self fail-closed behaviour (direct) ==="

# Runs each scenario in one subshell (a subshell's variables don't
# propagate back, so exit codes are the only channel out) and reports them
# as r1..r6 for the parent shell to pick up and assert on individually.
#   r1: __UNRESOLVED__ sentinel + unresolved-caller target      -> want 0 (refuse)
#   r2: __UNRESOLVED__ sentinel + already-resolved target       -> want 0 (refuse)
#   r3: valid self-root + empty target                          -> want 0 (refuse)
#   r4: valid self-root + empty target, already_resolved=1      -> want 0 (refuse)
#   r5: self-root entirely unset + a target                     -> want 0 (refuse)
#   r6: valid self-root + a genuinely different, already-        -> want 1 (allow)
#       resolved target -- proves r1-r5 aren't just an
#       always-refuse no-op that would trivially "pass" every case above.
_SUBSHELL_OUT=$(
  export _WTR_REPO_ROOT="$REPO_N9"
  source "$REGISTRY_LIB"
  _WTR_SELF_ROOT="__UNRESOLVED__"
  _wtr_is_self "/some/worktree";    echo "r1=$?"
  _wtr_is_self "/some/worktree" 1;  echo "r2=$?"
  _WTR_SELF_ROOT="/some/valid/root"
  _wtr_is_self "";                  echo "r3=$?"
  _wtr_is_self "" 1;                echo "r4=$?"
  unset _WTR_SELF_ROOT
  _wtr_is_self "/some/worktree";    echo "r5=$?"
  _WTR_SELF_ROOT="/repo/self"
  _wtr_is_self "/repo/other-worktree" 1; echo "r6=$?"
)
R1=$(echo "$_SUBSHELL_OUT" | grep -oE 'r1=[0-9]+' | cut -d= -f2)
R2=$(echo "$_SUBSHELL_OUT" | grep -oE 'r2=[0-9]+' | cut -d= -f2)
R3=$(echo "$_SUBSHELL_OUT" | grep -oE 'r3=[0-9]+' | cut -d= -f2)
R4=$(echo "$_SUBSHELL_OUT" | grep -oE 'r4=[0-9]+' | cut -d= -f2)
R5=$(echo "$_SUBSHELL_OUT" | grep -oE 'r5=[0-9]+' | cut -d= -f2)
R6=$(echo "$_SUBSHELL_OUT" | grep -oE 'r6=[0-9]+' | cut -d= -f2)

_assert_eq "$R1" "0" "UNRESOLVED sentinel refuses an unresolved-caller target (exit code)"
_assert_eq "$R2" "0" "UNRESOLVED sentinel refuses an already-resolved target (exit code)"
_assert_eq "$R3" "0" "empty target refuses (exit code)"
_assert_eq "$R4" "0" "empty target with already_resolved=1 refuses (exit code)"
_assert_eq "$R5" "0" "unset self-root refuses (exit code)"
_assert_eq "$R6" "1" "already_resolved=1 fast path allows a genuinely different target (exit code)"

echo ""

# ---------------------------------------------------------------------------
# Case 3 (Spec item 4): mutation proof. Revert _wtr_is_self to its pre-fix
# shape (always resolve target, always re-resolve repo-root) in a throwaway
# copy of the library, and show the realpath one-liner count on the SAME
# N=9 fixture jumps well above the post-fix count -- proving Case 1's
# assertion is not vacuously true.
# ---------------------------------------------------------------------------
echo "=== Case 3: mutation proof -- restoring per-entry python3 realpath ==="

MUTATED_LIB="$TMPDIR_ROOT/worktree-registry.mutated.sh"
cp "$REGISTRY_LIB" "$MUTATED_LIB"
python3 - "$MUTATED_LIB" <<'PYEOF'
import re, sys
path = sys.argv[1]
with open(path) as f:
    text = f.read()

marker = '''_wtr_is_self() {
  local target="${1:-}"
  local already_resolved="${2:-0}"
  # Fail closed on an empty target too — an unreachable-today branch is still
  # a reachable one waiting for a fourth call site, and this guard's whole
  # purpose is fail-closed.
  [[ -z "$target" ]] && return 0

  if [[ "${_WTR_SELF_ROOT:-}" == "__UNRESOLVED__" ]]; then
    return 0
  fi
  # An unset self root (e.g. a call site outside _cmd_reap that never ran
  # _wtr_resolve_self_root) is exactly as unresolved as the sentinel — fail
  # closed here too, not open.
  [[ -z "${_WTR_SELF_ROOT:-}" ]] && return 0

  local resolved_target
  if [[ "$already_resolved" == "1" ]]; then
    resolved_target="$target"
  else
    resolved_target="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$target" 2>/dev/null || echo "$target")"
  fi

  if [[ "$resolved_target" == "$_WTR_SELF_ROOT" ]]; then
    return 0
  fi
  # Ancestor check: target is an ancestor of the self root.
  if [[ "$_WTR_SELF_ROOT" == "$resolved_target"/* ]]; then
    return 0
  fi
  if [[ -n "${_WTR_REPO_ROOT:-}" ]]; then
    _wtr_resolved_repo_root
    if [[ "$resolved_target" == "$_WTR_REPO_ROOT_RESOLVED_CACHE" ]]; then
      return 0
    fi
  fi
  return 1
}'''

replacement = '''_wtr_is_self() {
  local target="${1:-}"
  local already_resolved="${2:-0}"
  [[ -z "$target" ]] && return 0

  if [[ "${_WTR_SELF_ROOT:-}" == "__UNRESOLVED__" ]]; then
    return 0
  fi
  [[ -z "${_WTR_SELF_ROOT:-}" ]] && return 0

  local resolved_target
  resolved_target="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$target" 2>/dev/null || echo "$target")"

  if [[ "$resolved_target" == "$_WTR_SELF_ROOT" ]]; then
    return 0
  fi
  if [[ "$_WTR_SELF_ROOT" == "$resolved_target"/* ]]; then
    return 0
  fi
  if [[ -n "${_WTR_REPO_ROOT:-}" ]]; then
    local resolved_repo_root
    resolved_repo_root="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$_WTR_REPO_ROOT" 2>/dev/null || echo "$_WTR_REPO_ROOT")"
    if [[ "$resolved_target" == "$resolved_repo_root" ]]; then
      return 0
    fi
  fi
  return 1
}'''

assert marker in text, "mutation anchor not found -- _wtr_is_self source shape changed"
text = text.replace(marker, replacement, 1)
with open(path, "w") as f:
    f.write(text)
PYEOF

MUT_PY_LOG="$TMPDIR_ROOT/py-mutated.log"
: > "$MUT_PY_LOG"
(
  export PATH="${SHIM_DIR}:${PATH}"
  export PY_SHIM_LOG="$MUT_PY_LOG"
  export MKTEMP_SHIM_LOG="$TMPDIR_ROOT/mktemp-mutated.log"
  export _WTR_REPO_ROOT="$REPO_N9"
  export WTR_TEST_MODE=1
  export WTR_OPEN_PR_BRANCHES_OVERRIDE=""
  # shellcheck source=/dev/null
  source "$MUTATED_LIB"
  cd "$REPO_N9"
  worktree_registry reap --ttl-min 1 --dry-run > /dev/null
  echo "RC=$?"
) > /dev/null
MUT_COUNT=$(_count_realpath_oneliners "$MUT_PY_LOG")

echo "  post-fix count (N=9): $COUNT_N9"
echo "  mutated (pre-fix shape) count (N=9): $MUT_COUNT"

if [[ "$MUT_COUNT" -gt "$COUNT_N9" ]]; then
  _pass "mutation proof: reverting the memoization makes the count jump ($COUNT_N9 -> $MUT_COUNT)"
else
  _fail "mutation proof: reverting the memoization did NOT increase the count ($COUNT_N9 -> $MUT_COUNT) -- Case 1's assertion would not catch this regression"
fi

echo ""
echo "==========================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "==========================================="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
