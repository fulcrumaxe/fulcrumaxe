#!/usr/bin/env bash
# tests/test_discussion_merge_count.sh — acceptance fixtures for
# scripts/lib/discussion-merge-count.sh (D#2590).
#
# `resolve_merged_count` in scripts/post-merge-hook.sh used to read pr_state
# rows for one plane only — the plane the CURRENT merge happened on — so a
# Discussion whose PRs straddled both planes, or whose earlier merge predated
# repo-scoped pr_state rows (D#2379), could never reach its own planned_prs
# and stayed open forever (D#2148, D#2524, D#2558, D#2585 — all had to be
# closed by hand). discussion_merge_count fixes it by asking GitHub directly,
# on both planes, and unioning that with repo-scoped pr_state rows on both
# planes and the PR the current hook run is for, keyed on (plane, PR number)
# so the same PR number on different planes never collapses into one.
#
# Fixtures F1-F3 and F6 are the real cases, with real numbers. Each of F1-F3
# stubs `gh` and pr_state exactly as the live state store looked when this
# was measured (2026-09-27) and is REQUIRED to return 2 — not 1.
#
# Run: bash tests/test_discussion_merge_count.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DMC_LIB="$REPO_ROOT/scripts/lib/discussion-merge-count.sh"

# shellcheck source=lib/blackboard-fixture.sh
source "$REPO_ROOT/tests/lib/blackboard-fixture.sh"
# D#2283: redirect AUTONOMOUS_TEAM_STATE_DIR to a scratch dir for the life of
# this suite, so the pr_state rows this test writes never land in
# ~/.autonomous-forever-state.
blackboard_scratch_state_dir || {
  echo "FATAL: could not create scratch state dir" >&2
  exit 1
}
SCRATCH_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$SCRATCH_STATE_DIR" "$STUB_DIR"' EXIT
# Resolved for its side effect (fails loudly if BLACKBOARD_DIR can't resolve)
# — the actual writes below go through the pr_state.py CLI, which resolves
# the same root itself.
blackboard_pr_state_dir "$REPO_ROOT" >/dev/null || {
  echo "FATAL: could not resolve blackboard pr_state dir" >&2
  exit 1
}

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    pass "$label"
  else
    fail "$label — expected '$expected', got '$actual'"
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label — expected to find '$needle' in: $haystack"
  fi
}

CODE_REPO="fulcrumaxe/fulcrumaxe"
DISC_REPO="autonomous-agent-7/fulcrumaxe"

# ── gh stub ──────────────────────────────────────────────────────────────────
# Reads a JSON fixture file per plane, and can be told to fail for either
# plane independently (DMC_TEST_FAIL_CODE / DMC_TEST_FAIL_DISC) — that's what
# F5 exercises. Any other `gh` invocation is unsupported by this stub, and
# discussion_merge_count never issues one.
cat > "$STUB_DIR/gh" <<'STUBEOF'
#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "list" ]]; then
  repo=""
  prev=""
  for a in "$@"; do
    if [[ "$prev" == "--repo" ]]; then repo="$a"; fi
    prev="$a"
  done
  if [[ "$repo" == "${DMC_TEST_CODE_REPO:-}" ]]; then
    if [[ "${DMC_TEST_FAIL_CODE:-0}" == "1" ]]; then
      echo "stub gh: simulated failure for code plane" >&2
      exit 1
    fi
    cat "${DMC_TEST_FIXTURE_CODE:?}"
    exit 0
  fi
  if [[ "$repo" == "${DMC_TEST_DISC_REPO:-}" ]]; then
    if [[ "${DMC_TEST_FAIL_DISC:-0}" == "1" ]]; then
      echo "stub gh: simulated failure for discussion plane" >&2
      exit 1
    fi
    cat "${DMC_TEST_FIXTURE_DISC:?}"
    exit 0
  fi
  echo "[]"
  exit 0
fi
echo "unsupported stub gh invocation: $*" >&2
exit 1
STUBEOF
chmod +x "$STUB_DIR/gh"

export DMC_TEST_CODE_REPO="$CODE_REPO"
export DMC_TEST_DISC_REPO="$DISC_REPO"
export PATH="$STUB_DIR:$PATH"

FIXTURE_CODE="$STUB_DIR/code.json"
FIXTURE_DISC="$STUB_DIR/disc.json"
export DMC_TEST_FIXTURE_CODE="$FIXTURE_CODE"
export DMC_TEST_FIXTURE_DISC="$FIXTURE_DISC"

_reset_gh_fixtures() {
  echo "[]" > "$FIXTURE_CODE"
  echo "[]" > "$FIXTURE_DISC"
  unset DMC_TEST_FAIL_CODE DMC_TEST_FAIL_DISC 2>/dev/null || true
}

# _pr_row <pr> <disc> [<repo>] — repo omitted writes the LEGACY unnamespaced
# key. Safe to call more than once for the same (pr, repo): a duplicate
# `init` is swallowed (stderr discarded) and `set` just re-applies merged=true
# on the existing row.
_pr_row() {
  local pr="$1" disc="$2" repo="${3:-}"
  if [[ -n "$repo" ]]; then
    python3 "$REPO_ROOT/backend/pr_state.py" init "$pr" --discussion "$disc" --repo "$repo" >/dev/null 2>&1
    python3 "$REPO_ROOT/backend/pr_state.py" set "$pr" --field "merged=true" --repo "$repo" >/dev/null 2>&1
  else
    python3 "$REPO_ROOT/backend/pr_state.py" init "$pr" --discussion "$disc" >/dev/null 2>&1
    python3 "$REPO_ROOT/backend/pr_state.py" set "$pr" --field "merged=true" >/dev/null 2>&1
  fi
}

# shellcheck source=lib/discussion-merge-count.sh
source "$DMC_LIB"

echo "=== F1 (D#2148, real): cross-plane — code #158, disc-plane #2625 ==="
_reset_gh_fixtures
cat > "$FIXTURE_CODE" <<'JSON'
[{"number":158,"title":"add x","body":"Closes D#2148"}]
JSON
cat > "$FIXTURE_DISC" <<'JSON'
[{"number":2625,"title":"add archive","body":"Closes D#2148"}]
JSON
_pr_row 158 2148 "$CODE_REPO"
_pr_row 2625 2148 "$DISC_REPO"
OUT=$(discussion_merge_count 2148 2625 "$DISC_REPO" 2>/dev/null)
assert_eq "F1 D#2148 returns 2" "2" "$OUT"

echo ""
echo "=== F2 (D#2524, real): same-plane — legacy row excluded, prose mention dropped ==="
_reset_gh_fixtures
cat > "$FIXTURE_CODE" <<'JSON'
[
  {"number":150,"title":"batch fix","body":"Closes D#2524"},
  {"number":166,"title":"batch fix 2","body":"Closes D#2524"},
  {"number":167,"title":"unrelated fix","body":"see D#2524 for context, not a closing reference"}
]
JSON
_pr_row 150 2524 ""              # legacy key (predates repo-scoped rows) — must NOT count
_pr_row 166 2524 "$CODE_REPO"
OUT=$(discussion_merge_count 2524 166 "$CODE_REPO" 2>/dev/null)
assert_eq "F2 D#2524 returns 2 (not 1, not 3)" "2" "$OUT"

echo ""
echo "=== F3 (D#2558, real): same-plane — legacy row excluded ==="
_reset_gh_fixtures
cat > "$FIXTURE_CODE" <<'JSON'
[
  {"number":192,"title":"batch a","body":"Closes D#2558"},
  {"number":250,"title":"batch b","body":"Closes D#2558"}
]
JSON
_pr_row 192 2558 ""              # legacy key — must NOT count
_pr_row 250 2558 "$CODE_REPO"
OUT=$(discussion_merge_count 2558 250 "$CODE_REPO" 2>/dev/null)
assert_eq "F3 D#2558 returns 2" "2" "$OUT"

echo ""
echo "=== F4a: number collision — a disc-plane PR closing a DIFFERENT Discussion ==="
_reset_gh_fixtures
cat > "$FIXTURE_CODE" <<'JSON'
[
  {"number":150,"title":"batch fix","body":"Closes D#2524"},
  {"number":166,"title":"batch fix 2","body":"Closes D#2524"},
  {"number":167,"title":"unrelated fix","body":"see D#2524 for context, not a closing reference"}
]
JSON
cat > "$FIXTURE_DISC" <<'JSON'
[{"number":150,"title":"unrelated disc PR","body":"Closes #146"}]
JSON
OUT=$(discussion_merge_count 2524 166 "$CODE_REPO" 2>/dev/null)
assert_eq "F4a D#2524 still returns 2 (disc #150 closes D#146, a different Discussion)" "2" "$OUT"

echo ""
echo "=== F4b: same PR NUMBER on both planes, both closing the same Discussion ==="
_reset_gh_fixtures
cat > "$FIXTURE_CODE" <<'JSON'
[{"number":150,"title":"code side fix","body":"Closes D#424242"}]
JSON
cat > "$FIXTURE_DISC" <<'JSON'
[{"number":150,"title":"disc side fix","body":"Closes D#424242"}]
JSON
OUT=$(discussion_merge_count 424242 150 "$CODE_REPO" 2>/dev/null)
assert_eq "F4b PR #150 on both planes counts as 2 — keyed on plane, never number alone" "2" "$OUT"

echo ""
echo "=== F5: gh failure on the code plane — falls back to pr_state, warns, exits 0 ==="
_reset_gh_fixtures
cat > "$FIXTURE_DISC" <<'JSON'
[{"number":2625,"title":"add archive","body":"Closes D#2148"}]
JSON
_pr_row 158 2148 "$CODE_REPO"
_pr_row 2625 2148 "$DISC_REPO"
export DMC_TEST_FAIL_CODE=1
ERR_FILE="$STUB_DIR/f5-stderr.txt"
OUT=$(discussion_merge_count 2148 2625 "$DISC_REPO" 2>"$ERR_FILE")
RC=$?
unset DMC_TEST_FAIL_CODE
assert_eq "F5 exit code 0 (gh failure never aborts)" "0" "$RC"
assert_eq "F5 still returns 2, from pr_state rows on both planes" "2" "$OUT"
assert_contains "F5 warns on stderr about the failed plane" "dropping GitHub-sourced matches for plane '$CODE_REPO'" "$(cat "$ERR_FILE")"

echo ""
echo "=== F6 (D#2558 index lag, real shape): GitHub misses the newest merge, current PR supplies it ==="
_reset_gh_fixtures
cat > "$FIXTURE_CODE" <<'JSON'
[{"number":192,"title":"batch a","body":"Closes D#2558"}]
JSON
_pr_row 192 2558 ""
_pr_row 250 2558 "$CODE_REPO"
OUT=$(discussion_merge_count 2558 250 "$CODE_REPO" 2>/dev/null)
assert_eq "F6 D#2558 returns 2 (current PR #250 supplies the second)" "2" "$OUT"

echo ""
echo "=== F7: a legacy-only pr_state row is never counted ==="
_reset_gh_fixtures
_pr_row 999 909090 ""            # legacy key, no plane — must be invisible
OUT=$(discussion_merge_count 909090 1 "$CODE_REPO" 2>/dev/null)
assert_eq "F7 legacy row excluded — only the current PR counts" "1" "$OUT"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
