#!/usr/bin/env bash
# tests/test_post_merge_hook_unmerged_paths.sh — hermetic tests for the
# unmerged-paths branch of the auto_pull step (scripts/lib/auto-pull-step.sh),
# driven through the shipping auto_pull_step function directly (D#1976).
#
# The previous version of this file built a ~200-line heredoc copy of the
# auto_pull logic and ran it behind a PATH shim that `exec`'d a hardcoded
# absolute git binary path that does not exist under nix. The shim
# intercepted every git call, including the very first
# `git branch --show-current`, so the suite never
# reached the branch it exists to cover: it reported 7 passed / 7 failed
# unconditionally, before and after a mutation to the shipping code's warning
# string. Not one assertion tracked the real scripts/lib/auto-pull-step.sh.
#
# This rewrite follows tests/test_post_merge_hook_pull.sh: source the
# shipping lib and call auto_pull_step against a throwaway git fixture built
# under mktemp -d. For this branch specifically, the fixture is a genuinely
# conflicted (UU/AA) index rather than a faked git-diff output: an add/add
# merge conflict between two branches in the fixture's local clone leaves a
# real unmerged path with the current branch still "main" (verified with
# `git status --porcelain` -> `AA conflict.txt` and
# `git diff --name-only --diff-filter=U` -> `conflict.txt` before this file
# was written). No git stub is needed anywhere. If a future change to this
# file genuinely needs one, it must resolve git through PATH/`command -v`
# and must never hardcode an absolute git interpreter path.
#
# Untracked-collision and modified-file-collision coverage for this same
# shipping function already lives in tests/test_post_merge_hook_pull.sh
# (Tests 2-13) and is not duplicated here.
#
# Run: bash tests/test_post_merge_hook_unmerged_paths.sh

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The shipping lib, sourced as-is — no copy, no heredoc.
# shellcheck source=scripts/lib/auto-pull-step.sh
source "${REAL_REPO_ROOT}/scripts/lib/auto-pull-step.sh"

PASS=0
FAIL=0
ERRORS=()
FIXTURES=()

TMP_STATE="$(mktemp -d)"
FIXTURES+=("$TMP_STATE")
export AUTONOMOUS_TEAM_STATE_DIR="$TMP_STATE/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"

# _REPO is what the unmerged-paths guard passes to `gh --repo`. Set it to a
# fixture value so a stub-miss is loud rather than silently reaching a real
# repo — see tests/test_post_merge_hook_pull.sh for the fuller rationale.
_REPO="fixture-org/fixture-repo"

# gh is stubbed on PATH for the whole file — no network, no real API (item 16).
GH_STUB_DIR="$TMP_STATE/bin"
mkdir -p "$GH_STUB_DIR"
GH_CALL_LOG="$TMP_STATE/gh-calls.txt"
: > "$GH_CALL_LOG"
cat > "$GH_STUB_DIR/gh" <<'GHSTUB'
#!/usr/bin/env bash
echo "$*" >> "${GH_CALL_LOG:?GH_CALL_LOG must be set for the gh stub}"
if [[ "$*" == *"issue create"* ]]; then
  echo "https://github.com/fixture-org/fixture-repo/issues/1"
  exit 0
fi
if [[ "$*" == *"issue list"* ]]; then
  echo "null"
  exit 0
fi
if [[ "$*" == *"issue comment"* ]]; then
  exit 0
fi
exit 0
GHSTUB
chmod +x "$GH_STUB_DIR/gh"
export GH_CALL_LOG
export PATH="$GH_STUB_DIR:$PATH"

TEAMLOG=""

# The seam. Overrides the lib's definition for the rest of this process.
auto_pull_step_teamlog() { printf 'TEAMLOG: %s\n' "$1" >> "$TEAMLOG"; }

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

# ── Assertions ────────────────────────────────────────────────────────────

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label — expected to find: $needle"
    echo "    Output was: $haystack" >&2
  fi
}

assert_rc() {
  local label="$1" rc="$2" want="$3"
  if [[ "$rc" == "$want" ]]; then
    pass "$label"
  else
    fail "$label — return code was $rc (expected $want)"
  fi
}

assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then
    pass "$label"
  else
    fail "$label — got [$got], expected [$want]"
  fi
}

assert_file() {
  local label="$1" path="$2"
  if [[ -f "$path" ]]; then
    pass "$label"
  else
    fail "$label — missing file: $path"
  fi
}

# ── Fixture ───────────────────────────────────────────────────────────────

setup_fake_origin() {
  local origin_dir="$1"
  git -C "$origin_dir" init --initial-branch=main -q
  git -C "$origin_dir" config user.email "test@test.com"
  git -C "$origin_dir" config user.name "Test"
  echo "file1" > "$origin_dir/file1.txt"
  git -C "$origin_dir" add .
  git -C "$origin_dir" commit -m "init" -q
}

setup_fake_local() {
  local local_dir="$1" origin_dir="$2"
  git clone "$origin_dir" "$local_dir" -q --local
  git -C "$local_dir" config user.email "test@test.com"
  git -C "$local_dir" config user.name "Test"
}

# Builds a fixture with a genuine conflicted (AA/unmerged) index in $T_LOCAL:
# origin advances past local (so LOCAL != REMOTE, the guard's precondition),
# and local gets a real add/add merge conflict on conflict.txt from a side
# branch — no git stub, no faked diff output. The current branch stays
# "main" throughout, exactly like a merge a human left mid-conflict.
new_unmerged_fixture() {
  T="$(mktemp -d)"
  FIXTURES+=("$T")
  T_ORIGIN="$T/origin"
  T_LOCAL="$T/local"
  TEAMLOG="$T/teamlog.txt"
  mkdir -p "$T_ORIGIN"
  : > "$TEAMLOG"
  setup_fake_origin "$T_ORIGIN"
  setup_fake_local "$T_LOCAL" "$T_ORIGIN"

  echo "origin-advance" > "$T_ORIGIN/newfile.txt"
  git -C "$T_ORIGIN" add .
  git -C "$T_ORIGIN" commit -m "advance origin" -q

  git -C "$T_LOCAL" checkout -b conflict-side -q
  echo "side-version" > "$T_LOCAL/conflict.txt"
  git -C "$T_LOCAL" add conflict.txt
  git -C "$T_LOCAL" commit -m "side adds conflict.txt" -q
  git -C "$T_LOCAL" checkout main -q
  echo "main-version" > "$T_LOCAL/conflict.txt"
  git -C "$T_LOCAL" add conflict.txt
  git -C "$T_LOCAL" commit -m "main adds conflict.txt" -q
  git -C "$T_LOCAL" merge conflict-side >/dev/null 2>&1 || true

  local uu_check
  uu_check="$(git -C "$T_LOCAL" diff --name-only --diff-filter=U)"
  if [[ "$uu_check" != "conflict.txt" ]]; then
    echo "FIXTURE SETUP FAILED: expected a UU conflict on conflict.txt, got: [$uu_check]" >&2
    exit 90
  fi
}

# Runs the shipping function against the fixture. Sets RC, OUT, COMBINED.
run_step() {
  OUT="$(auto_pull_step "$T_LOCAL" 2>&1)" && RC=0 || RC=$?
  COMBINED="$OUT
$(cat "$TEAMLOG" 2>/dev/null || true)"
}

# ── Test 1: unmerged-paths detected — loud warning + marker ────────────────
echo "Test 1: Unmerged-paths detected — loud warning + marker"
new_unmerged_fixture
: > "$GH_CALL_LOG"

run_step
assert_rc "test1: auto_pull_step returns 1 on the unmerged-paths branch (documented contract)" "$RC" "1"
assert_contains "test1: team-log warning tagged needs-boss" "$COMBINED" "needs-boss"
assert_contains "test1: team-log names the conflicted file" "$COMBINED" "conflict.txt"
assert_file "test1: auto-pull-blocked marker written" "${AUTONOMOUS_TEAM_STATE_DIR}/auto-pull-blocked"
assert_contains "test1: Bug Issue creation attempted" "$(cat "$GH_CALL_LOG")" "issue create"

# ── Test 2: second run with marker present — duplicate suppressed ──────────
echo "Test 2: Second run with marker present — duplicate suppressed"
FIRST_CALL_COUNT="$(wc -l < "$GH_CALL_LOG" | tr -d ' ')"
run_step
assert_rc "test2: second call still returns 1" "$RC" "1"
assert_contains "test2: second call suppresses the duplicate warning" "$COMBINED" "already reported"
SECOND_CALL_COUNT="$(wc -l < "$GH_CALL_LOG" | tr -d ' ')"
assert_eq "test2: second call opened no additional Bug Issue" "$SECOND_CALL_COUNT" "$FIRST_CALL_COUNT"
assert_file "test2: marker still present after suppression run" "${AUTONOMOUS_TEAM_STATE_DIR}/auto-pull-blocked"

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
