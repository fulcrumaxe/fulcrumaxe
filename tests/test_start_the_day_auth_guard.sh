#!/usr/bin/env bash
# tests/test_start_the_day_auth_guard.sh — tests for D#1787:
# scripts/start-the-day.sh reporting fabricated data when gh can't see the repo.
#
# Run: bash tests/test_start_the_day_auth_guard.sh
# Expects: all assertions pass, exit 0
#
# Covers:
#   - Criterion 1: scripts/lib/gh-precondition.sh's assert_gh_can_see_repo
#     exists and works against the real (healthy) repo and a real bogus one.
#   - Criterion 2: with gh unable to resolve the repo, start-the-day.sh
#     exits non-zero, names the active account + the recovery command, and
#     prints none of the three sweep markers. Full script run — safe,
#     because the assert fires and exits before section 1 ever touches git.
#   - Criterion 3: with a healthy account, the precondition lets the script
#     continue past it (not a guard that fires unconditionally).
#   - Criterion 4: the PR-sweep failure path prints the real captured gh
#     error text, not a guessed cause ("rate limit?").
#   - Criterion 5: no gh error body can be word-split into a fabricated
#     `D#<token>` line.
#   - Criterion 6: the plan-freshness tick only appears when the sweep
#     actually got data — negative under a broken sweep, positive under a
#     stub that reports everything genuinely fresh.
#   - A mutation/negative-direction check: a deliberately neutered
#     assert_gh_can_see_repo (always returns success) is caught failing
#     the same broken-gh condition our real fix catches — proof the check
#     is load-bearing, not a guard that always passes.
#
# IMPORTANT METHODOLOGY NOTE (learned the hard way while writing this file):
# start-the-day.sh's "## 1. Sync to fresh main" section unconditionally runs
# `git fetch`, `git symbolic-ref HEAD refs/heads/main` (if not already on
# main), and `git reset --mixed origin/main`. That is correct behavior for
# its intended caller (Team Lead, in the primary checkout) but it is NOT
# guarded against being run from an executor worktree — running the real,
# unstubbed script end-to-end from a worktree flips that worktree's own
# HEAD to main. Criteria 4/5/6 only need the "Open PRs" and "plan
# staleness" blocks, which live AFTER that section, so this file never
# invokes the full script except for the one case that's actually safe
# (criterion 2's broken-auth run, whose assert exits before section 1 is
# ever reached). For criteria 4/5/6 it extracts the exact shipped line
# ranges for those two blocks with `sed` — the real bytes, not a
# hand-copied re-transcription — and runs just that, with a scratch
# PLAN_FILE and PATH-stubbed `gh`, entirely under /tmp. This is a
# behavior-preserving discovery, not a scope change; see the PR body.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_SLUG="autonomous-agent-7/fulcrumaxe"

# shellcheck source=scripts/lib/gh-precondition.sh
source "${REAL_REPO_ROOT}/scripts/lib/gh-precondition.sh"

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

assert_true()  { if [[ "$2" == "0" ]]; then ok "$1"; else bad "$1" "expected success, got rc=$2"; fi; }
assert_false() { if [[ "$2" != "0" ]]; then ok "$1"; else bad "$1" "expected failure, got rc=0"; fi; }
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain [$2]" "$3"; fi
}
assert_not_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then bad "$1" "expected NOT to contain [$2]"; else ok "$1"; fi
}
assert_no_regex_match() {
  # $1=label $2=regex $3=haystack
  if printf '%s' "$3" | grep -qE -- "$2"; then
    bad "$1" "regex [$2] unexpectedly matched"
  else
    ok "$1"
  fi
}

# ── fixture: a fake `gh` on PATH, built fresh per test, deleted after ───────
# Modes (via GH_STUB_MODE):
#   broken   — every call fails: the NOT_FOUND-shaped body a gh account that
#              can't resolve a repo actually produces, on STDOUT, exit 1.
#   degraded — the one call Fix A's precondition makes (`api repos/<slug>`)
#              succeeds; every OTHER call still fails the same way. Models
#              an account that can see the repo but some other call still
#              fails afterward — what Fix B/C defend against on their own.
#   fresh    — every call succeeds with data saying nothing is stale and
#              nothing is missing — positive control for Fix C's tick.
make_gh_stub() {
  local dir
  dir=$(mktemp -d)
  FIXTURES+=("$dir")
  cat > "$dir/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
ARGS="$*"
MODE="${GH_STUB_MODE:-broken}"
NOT_FOUND_BODY='{"data":{"repository":null},"errors":[{"type":"NOT_FOUND","path":["query","repository"],"message":"Could not resolve to a Repository with the name (gh-stub)."}]}'

fail() { echo "$NOT_FOUND_BODY"; exit 1; }

is_repo_check() { [[ "$ARGS" == "api repos/"* ]]; }
repo_check_reply() { echo "${ARGS#api repos/}" | awk '{print $1}'; }

case "$MODE" in
  broken)
    fail
    ;;
  degraded)
    if is_repo_check; then repo_check_reply; exit 0; fi
    fail
    ;;
  fresh)
    if is_repo_check; then repo_check_reply; exit 0; fi
    case "$ARGS" in
      *"pr list"*"--state open"*)   echo "[]" ;;
      *"pr list"*"--state merged"*) echo "0" ;;
      *"discussion(number:"*"closed"*) echo "false" ;;
      *"discussion(number:"*"title"*)  echo "stub title" ;;
      *"discussions(first:50"*)  echo '{"data":{"repository":{"discussions":{"nodes":[]}}}}' ;;
      *"discussions(first:100"*) echo "" ;;
      *) echo "" ;;
    esac
    exit 0
    ;;
  *)
    echo "gh-stub: unknown GH_STUB_MODE=$MODE" >&2
    exit 2
    ;;
esac
STUB
  chmod +x "$dir/gh"
  echo "$dir"
}

# Runs the FULL real script. Only ever call this with mode=broken — that's
# the one case where Fix A's assert exits before section 1 touches git at
# all. See the methodology note at the top of this file.
run_full_script_broken() {
  local stubdir out rc
  stubdir=$(make_gh_stub)
  out=$(cd "$REAL_REPO_ROOT" && PATH="$stubdir:$PATH" GH_STUB_MODE=broken timeout 60 bash scripts/start-the-day.sh 2>&1)
  rc=$?
  printf '%s\x1e%s' "$rc" "$out"
}

# Extracts the exact shipped bytes of one line range from start-the-day.sh
# and runs them standalone with the given variables and gh stub mode. This
# tests the real shipping lines (not a hand-copied re-transcription) without
# ever touching section 1's git-sync logic.
run_snippet() {
  local start="$1" end="$2" mode="$3" plan_file="$4"
  local stubdir snippet out rc
  stubdir=$(make_gh_stub)
  snippet=$(mktemp -d)/snippet.sh
  FIXTURES+=("$(dirname "$snippet")")
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    echo "REPO='${REAL_SLUG}'"
    echo "REPO_OWNER='autonomous-agent-7'"
    echo "REPO_NAME='fulcrumaxe'"
    echo "PLAN_FILE='${plan_file}'"
    sed -n "${start},${end}p" "${REAL_REPO_ROOT}/scripts/start-the-day.sh"
  } > "$snippet"
  out=$(PATH="$stubdir:$PATH" GH_STUB_MODE="$mode" timeout 30 bash "$snippet" 2>&1)
  rc=$?
  printf '%s\x1e%s' "$rc" "$out"
}

echo "== Criterion 1: assert_gh_can_see_repo exists and works (real gh, read-only) =="

GREP_COUNT=$(grep -c 'gh api' "${REAL_REPO_ROOT}/scripts/lib/gh-precondition.sh")
if [[ "$GREP_COUNT" -ge 1 ]]; then ok "gh-precondition.sh calls 'gh api' at least once"; else bad "gh-precondition.sh calls 'gh api' at least once" "count=$GREP_COUNT"; fi

assert_gh_can_see_repo "$REAL_SLUG"
assert_true "assert_gh_can_see_repo succeeds against the real, healthy slug" "$?"

# Safe negative: a repo name that does not exist in OUR OWN namespace — never
# a different account, never a mutating call.
assert_gh_can_see_repo "autonomous-agent-7/fulcrumaxe-nonexistent-probe-d1787"
assert_false "assert_gh_can_see_repo fails against a repo that doesn't exist in our namespace" "$?"

echo ""
echo "== Criterion 2: broken auth is fatal and silent about everything else (full script run) =="

RESULT=$(run_full_script_broken)
RC="${RESULT%%$'\x1e'*}"
OUT="${RESULT#*$'\x1e'}"

assert_false "start-the-day.sh exits non-zero when gh can't resolve the repo" "$RC"
assert_contains "names the active account" "autonomous-agent-7" "$OUT"
# D#2186: the recovery line used to hardcode "--user autonomous-agent-7" —
# runnable advice that told every adopter (and, on our own broken-auth
# runs, the operator who just failed as that very account) to switch TO
# the account that just failed. It's now a placeholder-account command
# plus a pointer to `gh auth status` for the real list, not a literal
# repeat of the account name that already appears one line above.
assert_contains "prints a generic (non-account-specific) recovery command" "gh auth switch --hostname github.com --user <account>" "$OUT"
assert_contains "points at 'gh auth status' to list real accounts" "gh auth status" "$OUT"
assert_not_contains "recovery command itself no longer names our account" "--user autonomous-agent-7" "$OUT"
assert_not_contains "does not print 'Open PRs:'" "Open PRs:" "$OUT"
assert_not_contains "does not print 'SPEC_READY Discussions'" "SPEC_READY Discussions" "$OUT"
assert_not_contains "does not print 'Open Discussions NOT in plan'" "Open Discussions NOT in plan" "$OUT"

echo ""
echo "== Criterion 3: healthy account is not blocked by a fires-unconditionally guard =="
echo "  (covered directly above: assert_gh_can_see_repo returns 0 for the real healthy slug."
echo "   Full-script re-verification is skipped here — see methodology note; running the"
echo "   unstubbed script's section 1 against a live worktree is what caused the incident"
echo "   below, and repeating it would just flip HEAD again.)"

echo ""
echo "== Criterion 4: PR-sweep failure prints the real captured error, not a guess =="

GUESS_COUNT=$(grep -c 'rate limit' "${REAL_REPO_ROOT}/scripts/start-the-day.sh" || true)
assert_true "'rate limit' guess text is gone from start-the-day.sh" "$([[ "${GUESS_COUNT:-0}" -eq 0 ]] && echo 0 || echo 1)"

# Locate the exact "Open PRs:" block by content, not a hardcoded line number,
# so this test doesn't silently start testing the wrong lines if the file
# shifts.
PR_BLOCK_START=$(grep -n '^  echo "  Open PRs:"$' "${REAL_REPO_ROOT}/scripts/start-the-day.sh" | head -1 | cut -d: -f1)
PR_BLOCK_START=$((PR_BLOCK_START - 1))
PR_BLOCK_END=$(awk -v s="$PR_BLOCK_START" 'NR>s && /^  fi$/{print NR; exit}' "${REAL_REPO_ROOT}/scripts/start-the-day.sh")

RESULT=$(run_snippet "$PR_BLOCK_START" "$PR_BLOCK_END" degraded "/dev/null")
RC="${RESULT%%$'\x1e'*}"
OUT="${RESULT#*$'\x1e'}"

assert_not_contains "degraded run's PR-list failure text has no 'rate limit' guess" "rate limit" "$OUT"
assert_contains "degraded run's PR-list failure prints the real captured gh output" "gh pr list failed" "$OUT"
assert_contains "the captured text is drawn from the actual stub error body" "NOT_FOUND" "$OUT"

echo ""
echo "== Criterion 5 & 6: plan-staleness block — D# validation and the freshness tick =="

# Scratch plan file under /tmp with known-fresh D# refs — never written into
# the real checkout.
PLAN_SCRATCH_DIR=$(mktemp -d)
FIXTURES+=("$PLAN_SCRATCH_DIR")
PLAN_SCRATCH="$PLAN_SCRATCH_DIR/PLAN-TEST.md"
cat > "$PLAN_SCRATCH" <<'EOF'
# Scratch test plan
- D#90001 some fake fresh item
- D#90002 another fake fresh item
EOF

STALE_BLOCK_START=$(grep -n '^echo "## 5. Plan staleness check"$' "${REAL_REPO_ROOT}/scripts/start-the-day.sh" | head -1 | cut -d: -f1)
STALE_BLOCK_START=$((STALE_BLOCK_START - 1))
STALE_BLOCK_END=$(awk -v s="$STALE_BLOCK_START" 'NR>s && /^fi$/{print NR; exit}' "${REAL_REPO_ROOT}/scripts/start-the-day.sh")

DEGRADED_RESULT=$(run_snippet "$STALE_BLOCK_START" "$STALE_BLOCK_END" degraded "$PLAN_SCRATCH")
DEGRADED_RC="${DEGRADED_RESULT%%$'\x1e'*}"
DEGRADED_OUT="${DEGRADED_RESULT#*$'\x1e'}"

assert_true "degraded plan-staleness snippet still runs to completion (rc 0 — a failed sweep is reported, not a crash)" "$DEGRADED_RC"
assert_no_regex_match "degraded run's output has no fabricated D#<non-digit> token" 'D#($|[^0-9])' "$DEGRADED_OUT"
assert_contains "plan-staleness section reports a failed sweep instead of guessing" "sweep failed" "$DEGRADED_OUT"
assert_contains "the 'could not fetch open discussions' path is reachable too" "Could not fetch open discussions" "$DEGRADED_OUT"
assert_not_contains "negative: tick absent when the sweep is degraded" "Plan references look fresh" "$DEGRADED_OUT"

FRESH_RESULT=$(run_snippet "$STALE_BLOCK_START" "$STALE_BLOCK_END" fresh "$PLAN_SCRATCH")
FRESH_RC="${FRESH_RESULT%%$'\x1e'*}"
FRESH_OUT="${FRESH_RESULT#*$'\x1e'}"

assert_true "fresh-stub plan-staleness snippet exits 0" "$FRESH_RC"
assert_contains "positive: tick appears when the sweep genuinely reports everything fresh" "Plan references look fresh" "$FRESH_OUT"

echo ""
echo "== Mutation / negative-direction check: a genuinely mutated FILE is caught, not a shell shadow =="
# The previous version of this check sourced the real file and then
# redefined assert_gh_can_see_repo in the SAME shell before calling it —
# so it was calling its own shadow, not the shipped function, and could
# never fail no matter what the real file contained. Fixed by mutating a
# temp COPY of the file on disk (the success condition, not the message)
# and sourcing only that copy, with nothing redefined afterward. Proof
# requires both directions: the unmodified file must NOT trip the "wrongly
# reports success" check, and the mutated one must.

run_assert_from_file() {
  # $1 = path to a gh-precondition.sh (real or mutated), $2 = GH_STUB_MODE
  local file="$1" mode="$2" stubdir
  stubdir=$(make_gh_stub)
  (
    PATH="$stubdir:$PATH" GH_STUB_MODE="$mode" bash -c '
      source "'"$file"'"
      assert_gh_can_see_repo "'"$REAL_SLUG"'"
    '
  )
  return $?
}

MUTANT_DIR=$(mktemp -d)
FIXTURES+=("$MUTANT_DIR")
cp "${REAL_REPO_ROOT}/scripts/lib/gh-precondition.sh" "$MUTANT_DIR/unmodified.sh"
# Mutate the actual pass/fail condition so the function always takes the
# success branch, regardless of what `gh` really returned.
sed 's/if \[\[ \$rc -eq 0 && "\$out" == "\$slug" \]\]; then/if true; then/' \
  "${REAL_REPO_ROOT}/scripts/lib/gh-precondition.sh" > "$MUTANT_DIR/mutated.sh"
assert_contains "sanity: the mutation actually landed in the copy" "if true; then" "$(cat "$MUTANT_DIR/mutated.sh")"
assert_not_contains "sanity: the unmodified copy is untouched" "if true; then" "$(cat "$MUTANT_DIR/unmodified.sh")"

run_assert_from_file "$MUTANT_DIR/unmodified.sh" broken
UNMODIFIED_RC=$?
assert_false "the real (unmodified) file correctly reports failure under broken gh — this check goes red for 'wrongly succeeds', as it should" "$UNMODIFIED_RC"

run_assert_from_file "$MUTANT_DIR/mutated.sh" broken
MUTATED_RC=$?
assert_true "the mutated copy wrongly reports success under the same broken gh — this check goes green, proving it can actually catch the defect" "$MUTATED_RC"

echo ""
echo "== Criterion 7 (reported, not fixed): sibling occurrence count =="
echo "  (informational — count and exact grep command are stated in the PR body, not asserted here)"

echo ""
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
