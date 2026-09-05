#!/usr/bin/env bash
# test_triage_orphan_diffs.sh — integration tests for triage-orphan-diffs.sh
#
# Uses a temp directory as a fake git repo to avoid touching real archive/.
# All test helper invocations capture output for assertion rather than
# running against the live repo.
#
# Exit code: 0 = all tests passed, 1 = one or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TRIAGE_SCRIPT="${REPO_ROOT}/scripts/triage-orphan-diffs.sh"

# ---------------------------------------------------------------------------
# Test framework (minimal)
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

_assert_exit0()  { [[ "$1" -eq 0 ]]  && _pass "$2" || _fail "$2 (exit=$1)"; }
_assert_exit2()  { [[ "$1" -eq 2 ]]  && _pass "$2" || _fail "$2 (expected exit=2, got=$1)"; }
_assert_contains() { echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (missing: '$2')"; }
_assert_not_contains() { ! echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (unexpected: '$2')"; }
_assert_file_exists()  { [[ -f "$1" ]] && _pass "$2" || _fail "$2 (missing file: $1)"; }
_assert_file_missing() { [[ ! -f "$1" ]] && _pass "$2" || _fail "$2 (file should not exist: $1)"; }

# ---------------------------------------------------------------------------
# Setup — temp git repo with fixture patches
# ---------------------------------------------------------------------------
TMPDIR_ROOT=$(mktemp -d /tmp/test-triage-XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# Keep a reference to the real repo root before overriding REPO_ROOT
REAL_REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export REAL_REPO_ROOT

cd "$TMPDIR_ROOT"
git init --quiet
git config user.email "test@test.com"
git config user.name "Test"

mkdir -p archive/orphan-diffs .autonomous-team

# Helper — create a minimal valid git diff patch (not necessarily applicable)
_make_patch() {
  local name="$1"
  cat > "archive/orphan-diffs/${name}" <<'PATCH'
diff --git a/backend/server.py b/backend/server.py
index abc1234..def5678 100644
--- a/backend/server.py
+++ b/backend/server.py
@@ -1,3 +1,5 @@
 # server
+import os
+import sys
 def run():
-    pass
+    print("running")
PATCH
}

TODAY=$(date +%Y-%m-%d)
# fixture 1: recent untriaged
PATCH1="agent-fixture1aa-${TODAY}.patch"
_make_patch "$PATCH1"

# fixture 2: will be marked salvaged
PATCH2="agent-fixture2bb-${TODAY}.patch"
_make_patch "$PATCH2"

# fixture 3: 35 days old untriaged — should be discarded by discard-older-than 30d
PATCH3_DATE=$(date -d "-35 days" +%Y-%m-%d 2>/dev/null || python3 -c "import datetime; print((datetime.date.today()-datetime.timedelta(days=35)).strftime('%Y-%m-%d'))")
PATCH3="agent-fixture3cc-${PATCH3_DATE}.patch"
_make_patch "$PATCH3"
# Force mtime to 35 days ago
touch -t "$(date -d '-35 days' +%Y%m%d%H%M 2>/dev/null || python3 -c "import datetime; d=datetime.date.today()-datetime.timedelta(days=35); print(d.strftime('%Y%m%d0000'))")" "archive/orphan-diffs/${PATCH3}" 2>/dev/null || true

# Commit fixtures so git mv works
git add archive/
git commit --quiet -m "test fixtures"

# Override ORPHAN_DIFF_DIR in our env by setting REPO_ROOT to tmpdir
export REPO_ROOT="$TMPDIR_ROOT"

# ---------------------------------------------------------------------------
# Test 1: default list shows all 3 patches, all untriaged
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 1: default list ==="
OUT=$(bash "$TRIAGE_SCRIPT" list 2>&1); RC=$?
_assert_exit0 $RC "list exits 0"
_assert_contains "$OUT" "$PATCH1" "list shows patch1"
_assert_contains "$OUT" "$PATCH2" "list shows patch2"
_assert_contains "$OUT" "$PATCH3" "list shows patch3"
_assert_contains "$OUT" "untriaged" "list shows untriaged status"

# ---------------------------------------------------------------------------
# Test 2: list --json valid JSON with count=3
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 2: list --json ==="
JSON_OUT=$(bash "$TRIAGE_SCRIPT" list --json 2>&1); RC=$?
_assert_exit0 $RC "list --json exits 0"
COUNT=$(echo "$JSON_OUT" | jq -e '.count' 2>/dev/null || echo "ERR")
[[ "$COUNT" -eq 3 ]] && _pass "list --json count=3" || _fail "list --json count expected 3, got $COUNT"
# Validate JSON is parseable
echo "$JSON_OUT" | jq -e '.' > /dev/null 2>&1 && _pass "list --json is valid JSON" || _fail "list --json invalid JSON"

# ---------------------------------------------------------------------------
# Test 3: set --status salvaged on patch2
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 3: set status ==="
OUT=$(bash "$TRIAGE_SCRIPT" set "$PATCH2" --status salvaged --note "test note" 2>&1); RC=$?
_assert_exit0 $RC "set exits 0"
META_FILE="archive/orphan-diffs/${PATCH2}.meta.json"
_assert_file_exists "$META_FILE" "meta.json created"
META_STATUS=$(jq -r '.status' "$META_FILE" 2>/dev/null || echo "")
[[ "$META_STATUS" == "salvaged" ]] && _pass "meta status=salvaged" || _fail "meta status expected salvaged, got $META_STATUS"
META_NOTE=$(jq -r '.note' "$META_FILE" 2>/dev/null || echo "")
[[ "$META_NOTE" == "test note" ]] && _pass "meta note correct" || _fail "meta note expected 'test note', got $META_NOTE"
TAGGED_AT=$(jq -r '.tagged_at' "$META_FILE" 2>/dev/null || echo "")
[[ "$TAGGED_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ]] && _pass "meta tagged_at is ISO8601" || _fail "meta tagged_at not ISO8601: $TAGGED_AT"

# ---------------------------------------------------------------------------
# Test 4: set with invalid status exits 2
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 4: set invalid status ==="
bash "$TRIAGE_SCRIPT" set "$PATCH1" --status bogus 2>&1; RC=$?
_assert_exit2 $RC "set bogus status exits 2"

# ---------------------------------------------------------------------------
# Test 5: discard-older-than 30d --dry-run shows PATCH3, no moves
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 5: discard-older-than --dry-run ==="
OUT=$(bash "$TRIAGE_SCRIPT" discard-older-than 30d --dry-run 2>&1); RC=$?
_assert_exit0 $RC "discard dry-run exits 0"
_assert_contains "$OUT" "$PATCH3" "dry-run lists old patch"
_assert_not_contains "$OUT" "Discarded" "dry-run does not actually discard"
# Confirm file still exists
_assert_file_exists "archive/orphan-diffs/${PATCH3}" "dry-run does not move file"

# ---------------------------------------------------------------------------
# Test 6: discard-older-than 30d moves PATCH3, keeps PATCH1 and PATCH2
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 6: discard-older-than (live) ==="
OUT=$(bash "$TRIAGE_SCRIPT" discard-older-than 30d 2>&1); RC=$?
_assert_exit0 $RC "discard exits 0"
_assert_contains "$OUT" "Discarded" "discard prints summary"

DISCARD_DIR="archive/orphan-diffs-discarded-${TODAY}"
_assert_file_exists "${DISCARD_DIR}/${PATCH3}" "old patch moved to discard dir"
_assert_file_exists "${DISCARD_DIR}/README.md" "README.md generated in discard dir"
_assert_file_missing "archive/orphan-diffs/${PATCH3}" "old patch removed from source dir"
_assert_file_exists "archive/orphan-diffs/${PATCH1}" "recent patch not moved"
_assert_file_exists "archive/orphan-diffs/${PATCH2}" "salvaged patch not moved"

# Check README.md content
README_CONTENT=$(cat "${DISCARD_DIR}/README.md" 2>/dev/null || echo "")
_assert_contains "$README_CONTENT" "git mv" "README has restore command"
_assert_contains "$README_CONTENT" "riginal path" "README has original path field"

# Verify sidecar status was updated to discarded
DISCARDED_META="${DISCARD_DIR}/${PATCH3}.meta.json"
if [[ -f "$DISCARDED_META" ]]; then
  DISC_STATUS=$(jq -r '.status' "$DISCARDED_META" 2>/dev/null || echo "")
  [[ "$DISC_STATUS" == "discarded" ]] && _pass "moved sidecar status=discarded" || _fail "moved sidecar status expected discarded, got $DISC_STATUS"
fi

# ---------------------------------------------------------------------------
# Test 7: stats --json after discard
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 7: stats --json ==="
STATS=$(bash "$TRIAGE_SCRIPT" stats --json 2>&1); RC=$?
_assert_exit0 $RC "stats --json exits 0"
echo "$STATS" | jq -e '.' > /dev/null 2>&1 && _pass "stats --json valid JSON" || _fail "stats --json invalid JSON"
TOTAL=$(echo "$STATS" | jq -r '.total' 2>/dev/null || echo "0")
UNTRIAGED=$(echo "$STATS" | jq -r '.untriaged' 2>/dev/null || echo "0")
SALVAGED=$(echo "$STATS" | jq -r '.salvaged' 2>/dev/null || echo "0")
# After discard: PATCH1 (untriaged), PATCH2 (salvaged) remain in archive/orphan-diffs
[[ "${TOTAL:-0}" -eq 2 ]] && _pass "stats total=2 after discard" || _fail "stats total expected 2, got ${TOTAL:-0}"
[[ "${UNTRIAGED:-0}" -eq 1 ]] && _pass "stats untriaged=1" || _fail "stats untriaged expected 1, got ${UNTRIAGED:-0}"
[[ "${SALVAGED:-0}" -eq 1 ]] && _pass "stats salvaged=1" || _fail "stats salvaged expected 1, got ${SALVAGED:-0}"

# ---------------------------------------------------------------------------
# Test 8: grep script source — no git rm or bare rm on patches
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 8: source code safety checks ==="
# Check for actual invocations of 'git rm' — look for executable git calls only.
# Exclude: comment lines (#), lines inside heredocs used for documentation.
# A real git rm invocation looks like: git rm, git -C ... rm, $(git rm)
# Method: check that no line matches 'git' followed by optional flags then 'rm' as command,
# excluding lines that are clearly documentation strings or comments.
TRIAGE_GIT_RM=$(grep -E '^\s*(git rm|git -C.*\brm\b|\$\(git rm)' "$TRIAGE_SCRIPT" 2>/dev/null | grep -v '^\s*#' || true)
[[ -z "$TRIAGE_GIT_RM" ]] && _pass "triage script has no 'git rm' command" || _fail "triage script contains 'git rm' command: $TRIAGE_GIT_RM"
LIB_GIT_RM=$(grep -E '^\s*(git rm|git -C.*\brm\b|\$\(git rm)' "${REPO_ROOT}/scripts/lib/orphan-triage.sh" 2>/dev/null | grep -v '^\s*#' || true)
[[ -z "$LIB_GIT_RM" ]] && _pass "orphan-triage.sh has no 'git rm' command" || _fail "orphan-triage.sh contains 'git rm' command: $LIB_GIT_RM"

# ---------------------------------------------------------------------------
# Test 9: Reaper nudge — emit when untriaged > threshold, not when same count
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 9: reaper nudge idempotency ==="

# Add 2 more untriaged patches to exceed threshold=2
PATCH4="agent-fixture4dd-${TODAY}.patch"
PATCH5="agent-fixture5ee-${TODAY}.patch"
_make_patch "$PATCH4"
_make_patch "$PATCH5"
git add archive/ && git commit --quiet -m "more fixtures"

# Source the lib with low threshold
NUDGE_STATE="${TMPDIR_ROOT}/.autonomous-team/orphan-diff-nudge-state.json"
rm -f "$NUDGE_STATE"

# We test the _ot_nudge_if_over_threshold helper directly
# Stub rotate-team-log.sh to capture calls
mkdir -p "${TMPDIR_ROOT}/scripts"
MOCK_LOG="${TMPDIR_ROOT}/mock-team-log.txt"
cat > "${TMPDIR_ROOT}/scripts/rotate-team-log.sh" <<'MOCK'
#!/usr/bin/env bash
echo "NUDGE: $*" >> "$MOCK_LOG_FILE"
MOCK
chmod +x "${TMPDIR_ROOT}/scripts/rotate-team-log.sh"
export MOCK_LOG_FILE="$MOCK_LOG"

# Copy real lib to tmpdir for sourcing.
# REAL_REPO_ROOT is the actual repo, set before REPO_ROOT was overridden.
mkdir -p "${TMPDIR_ROOT}/scripts/lib"
cp "${REAL_REPO_ROOT}/scripts/lib/orphan-triage.sh" "${TMPDIR_ROOT}/scripts/lib/" 2>/dev/null || true
cp "${REAL_REPO_ROOT}/scripts/lib/orphan-triage.sh" "${TMPDIR_ROOT}/scripts/" 2>/dev/null || true

# Run nudge with threshold=2, untriaged=3 (PATCH1, PATCH4, PATCH5)
(
  export ORPHAN_DIFF_NUDGE_THRESHOLD=2
  export REPO_ROOT="$TMPDIR_ROOT"
  source "${TMPDIR_ROOT}/scripts/lib/orphan-triage.sh"
  _ot_nudge_if_over_threshold
) 2>&1

NUDGE_COUNT=$(grep -c "NUDGE:" "$MOCK_LOG" 2>/dev/null || echo 0)
[[ "$NUDGE_COUNT" -ge 1 ]] && _pass "nudge fires when over threshold" || _fail "nudge did not fire (count=$NUDGE_COUNT)"

# Run again — same count, should NOT nudge again
(
  export ORPHAN_DIFF_NUDGE_THRESHOLD=2
  export REPO_ROOT="$TMPDIR_ROOT"
  source "${TMPDIR_ROOT}/scripts/lib/orphan-triage.sh"
  _ot_nudge_if_over_threshold
) 2>&1

NUDGE_COUNT2=$(grep -c "NUDGE:" "$MOCK_LOG" 2>/dev/null || echo 0)
[[ "$NUDGE_COUNT2" -eq "$NUDGE_COUNT" ]] && _pass "nudge does not re-fire at same count" || _fail "nudge re-fired (count went from $NUDGE_COUNT to $NUDGE_COUNT2)"

# ---------------------------------------------------------------------------
# Test 10: help flag exits 0 and prints subcommands
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 10: help ==="
HELP_OUT=$(bash "$TRIAGE_SCRIPT" --help 2>&1); RC=$?
_assert_exit0 $RC "help exits 0"
_assert_contains "$HELP_OUT" "list" "help mentions list"
_assert_contains "$HELP_OUT" "set" "help mentions set"
_assert_contains "$HELP_OUT" "discard-older-than" "help mentions discard-older-than"
_assert_contains "$HELP_OUT" "stats" "help mentions stats"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "==========================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "==========================================="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
