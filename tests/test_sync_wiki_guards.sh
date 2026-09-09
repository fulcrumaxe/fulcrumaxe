#!/usr/bin/env bash
# tests/test_sync_wiki_guards.sh — tests for scripts/sync-wiki.sh (D#2508)
#
# Run: bash tests/test_sync_wiki_guards.sh
# Expects: all assertions pass, exit 0
#
# Covers:
#   - the push-race retry (mandatory): a genuine competing push lands on the
#     wiki's bare repo between this script's own clone and its own push;
#     asserts both commits actually arrive in the bare repo afterwards, not
#     merely that a retry code path was exercised.
#   - the detached-HEAD and non-main-branch premise checks (conditional on
#     what's actually in scripts/sync-wiki.sh, not assumed from a different
#     script — see the guard's comment there). Both were found, by running
#     the real generator (backend/status_page.py) against a detached and a
#     non-main checkout, to silently produce a "Recent commits" section from
#     the wrong point in history. Each condition gets its own test here: the
#     warning fires, distinctly, and the sync still completes and content
#     still arrives — a guard that blocked the sync would fail these too.
#   - the no-wiki/ adopter skip path (D#1858) does not regress.
#
# Every fixture is a throwaway set of git repos under mktemp -d. Nothing here
# touches this repo's own git state, and no fixture ever writes into this
# checkout — see the "outside worktree" write-verb rule in hooks/sandbox.py,
# which this test respects by keeping every git write-verb inside mktemp -d
# trees reached only via `cd`/`git -C`, never via a path back into this repo.
#
# Follows the repo's plain-bash test convention (tests/test_auto_pull_recover.sh).
set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_SYNC_WIKI="$REAL_REPO_ROOT/scripts/sync-wiki.sh"

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

assert_true() { if [[ "$2" == "0" ]]; then ok "$1"; else bad "$1" "expected success, got rc=$2"; fi; }
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain [$2], got: $3"; fi
}
assert_not_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then bad "$1" "expected NOT to contain [$2], got: $3"; else ok "$1"; fi
}
assert_file_contains() {
  if [[ -f "$2" ]] && grep -qF -- "$3" "$2"; then ok "$1"; else bad "$1" "file $2 missing or missing content [$3]"; fi
}

# new_bare <path> — a bare repo standing in for the GitHub wiki, on branch
# "main", seeded with one commit so `git pull --rebase` has a history to
# rebase onto.
new_bare() {
  local bare="$1" seed
  git init -q --bare -b main "$bare"
  seed="$(mktemp -d)"
  FIXTURES+=("$seed")
  git clone -q "$bare" "$seed/seed"
  git -C "$seed/seed" config user.email "test@example.invalid"
  git -C "$seed/seed" config user.name "fixture"
  printf 'seed\n' > "$seed/seed/SEED.md"
  git -C "$seed/seed" add -- SEED.md
  git -C "$seed/seed" commit -qm "seed"
  git -C "$seed/seed" push -q origin main
}

# new_source_fixture <dir> — a $REPO_DIR stand-in: a real git checkout
# carrying scripts/sync-wiki.sh (the shipping file, copied as-is — not a
# heredoc, so this test exercises the code that actually ships, the same
# reason tests/test_auto_pull_recover.sh gives) and one hand-authored wiki
# page. backend/status_page.py and backend/changelog.py are deliberately
# absent by default: sync-wiki.sh already tolerates that (`|| true`), and the
# guard under test here lives in sync-wiki.sh itself, not in the generators.
new_source_fixture() {
  local dir="$1"
  mkdir -p "$dir/scripts" "$dir/wiki"
  cp "$REAL_SYNC_WIKI" "$dir/scripts/sync-wiki.sh"
  chmod +x "$dir/scripts/sync-wiki.sh"
  printf '# Home\n\nhand-authored wiki content\n' > "$dir/wiki/Home.md"
  git init -q -b main "$dir"
  git -C "$dir" config user.email "test@example.invalid"
  git -C "$dir" config user.name "fixture"
  git -C "$dir" add -A
  git -C "$dir" commit -qm "seed source checkout"
}

echo "1: push race — a genuine competing push lands mid-sync, and both land"
T1="$(mktemp -d)"; FIXTURES+=("$T1")
BARE1="$T1/wiki.git"
new_bare "$BARE1"
SRC1="$T1/source"
new_source_fixture "$SRC1"
# A slow stub generator widens the clone -> push window so the competing push
# below reliably lands inside it. This is a test-only fixture file dropped
# into the throwaway source checkout above — it never touches the real
# backend/status_page.py.
mkdir -p "$SRC1/backend"
cat > "$SRC1/backend/status_page.py" <<'PYEOF'
import sys
import time
time.sleep(3)
sys.exit(0)
PYEOF

LOG1="$T1/sync.log"
SYNC_WIKI_URL_OVERRIDE="$BARE1" bash "$SRC1/scripts/sync-wiki.sh" > "$LOG1" 2>&1 &
SYNC_PID=$!

sleep 0.5
# The competing push: an independent clone lands its own commit on the bare
# repo while the backgrounded sync above is asleep inside the stub generator
# — i.e. strictly after its own clone and strictly before its own push.
COMPETE="$T1/competitor"
git clone -q "$BARE1" "$COMPETE"
git -C "$COMPETE" config user.email "test@example.invalid"
git -C "$COMPETE" config user.name "competitor"
printf 'competing change\n' > "$COMPETE/Competing.md"
git -C "$COMPETE" add -- Competing.md
git -C "$COMPETE" commit -qm "competing update"
git -C "$COMPETE" push -q origin main

wait "$SYNC_PID"
SYNC_RC=$?
assert_true "sync-wiki.sh exits 0 after the push race" "$SYNC_RC"
assert_contains "the log shows a push was actually rejected once (a genuine race, not a no-op)" \
  "push rejected" "$(cat "$LOG1")"

VERIFY1="$T1/verify"
git clone -q "$BARE1" "$VERIFY1"
assert_file_contains "the competing commit's content survives" "$VERIFY1/Competing.md" "competing change"
assert_file_contains "this run's own content also arrives" "$VERIFY1/Home.md" "hand-authored wiki content"

echo "2: detached HEAD — warns, and content still arrives"
T2="$(mktemp -d)"; FIXTURES+=("$T2")
BARE2="$T2/wiki.git"
new_bare "$BARE2"
SRC2="$T2/source"
new_source_fixture "$SRC2"
git -C "$SRC2" checkout -q --detach HEAD
LOG2="$T2/sync.log"
SYNC_WIKI_URL_OVERRIDE="$BARE2" bash "$SRC2/scripts/sync-wiki.sh" > "$LOG2" 2>&1
assert_true "sync-wiki.sh still exits 0 when detached" "$?"
assert_contains "detached-HEAD warning fires" "detached-HEAD state" "$(cat "$LOG2")"
VERIFY2="$T2/verify"
git clone -q "$BARE2" "$VERIFY2"
assert_file_contains "content still arrives despite detached HEAD" "$VERIFY2/Home.md" "hand-authored wiki content"

echo "3: non-main branch — warns distinctly from detached, and content still arrives"
T3="$(mktemp -d)"; FIXTURES+=("$T3")
BARE3="$T3/wiki.git"
new_bare "$BARE3"
SRC3="$T3/source"
new_source_fixture "$SRC3"
git -C "$SRC3" checkout -q -b some-feature-branch
LOG3="$T3/sync.log"
SYNC_WIKI_URL_OVERRIDE="$BARE3" bash "$SRC3/scripts/sync-wiki.sh" > "$LOG3" 2>&1
assert_true "sync-wiki.sh still exits 0 on a non-main branch" "$?"
assert_contains "branch warning fires and names the branch" "some-feature-branch" "$(cat "$LOG3")"
assert_contains "…and says it's not main" "not main" "$(cat "$LOG3")"
assert_not_contains "…and it is NOT the detached-HEAD message — distinct condition, distinct guard" \
  "detached-HEAD state" "$(cat "$LOG3")"
VERIFY3="$T3/verify"
git clone -q "$BARE3" "$VERIFY3"
assert_file_contains "content still arrives on a non-main branch" "$VERIFY3/Home.md" "hand-authored wiki content"

echo "4: attached to main — no false-positive warning"
T4="$(mktemp -d)"; FIXTURES+=("$T4")
BARE4="$T4/wiki.git"
new_bare "$BARE4"
SRC4="$T4/source"
new_source_fixture "$SRC4"
LOG4="$T4/sync.log"
SYNC_WIKI_URL_OVERRIDE="$BARE4" bash "$SRC4/scripts/sync-wiki.sh" > "$LOG4" 2>&1
assert_true "sync-wiki.sh exits 0 when attached to main" "$?"
assert_not_contains "no detached-HEAD warning" "detached-HEAD state" "$(cat "$LOG4")"
assert_not_contains "no non-main-branch warning" "not main" "$(cat "$LOG4")"

echo "5: adopter skip path (D#1858) does not regress — no wiki/ in this repo"
SKIP_OUT="$(bash "$REAL_SYNC_WIKI" 2>&1)"
SKIP_RC=$?
assert_true "exits 0 with no wiki/ directory" "$SKIP_RC"
assert_contains "prints the existing skip message" "no local wiki/ directory to sync" "$SKIP_OUT"
assert_not_contains "no 'fatal:' noise" "fatal:" "$SKIP_OUT"
assert_not_contains "no 'rejected' noise" "rejected" "$SKIP_OUT"

echo
echo "─────────────────────────────────────────"
echo "  passed: $PASS   failed: $FAIL"
if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
