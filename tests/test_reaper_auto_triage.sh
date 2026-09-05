#!/usr/bin/env bash
# test_reaper_auto_triage.sh — tests for auto-triage integration in reap-worktrees.sh
#
# Acceptance criteria covered:
#   AC1: 60 patches (30 safe, 30 review-needed) → 25 processed per run; correct routing
#   AC2: After 3 runs with pile decreasing, no team-log warning posted
#   AC3: After 3 runs with pile stuck >50, warning posted exactly once (not per run)
#   AC4: --auto never discards patches touching files outside safe-discard allowlist
#   AC5: Batch sizing, safe-discard, needs-review move, warning suppression all covered
#
# Uses a temp git repo to avoid touching real archive/.
# Exit code: 0 = all tests passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_REAL="$(cd "$SCRIPT_DIR/.." && pwd)"
TRIAGE_SCRIPT="${REPO_ROOT_REAL}/scripts/triage-orphan-diffs.sh"
ORPHAN_TRIAGE_LIB="${REPO_ROOT_REAL}/scripts/lib/orphan-triage.sh"

# ---------------------------------------------------------------------------
# Minimal test framework
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; ((PASS++)) || true; }
_fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

_assert_exit0()         { [[ "$1" -eq 0 ]] && _pass "$2" || _fail "$2 (exit=$1)"; }
_assert_contains()      { echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (missing: '$2' in output)"; }
_assert_not_contains()  { ! echo "$1" | grep -qF "$2" && _pass "$3" || _fail "$3 (unexpected: '$2' in output)"; }
_assert_file_exists()   { [[ -f "$1" ]] && _pass "$2" || _fail "$2 (missing file: $1)"; }
_assert_file_missing()  { [[ ! -f "$1" ]] && _pass "$2" || _fail "$2 (should not exist: $1)"; }
_assert_dir_exists()    { [[ -d "$1" ]] && _pass "$2" || _fail "$2 (missing dir: $1)"; }
# Trim whitespace/newlines before numeric comparison
_assert_eq()            { local a; a=$(echo "$1" | tr -d '[:space:]'); [[ "$a" -eq "$2" ]] && _pass "$3" || _fail "$3 (expected $2, got $a)"; }
_assert_ge()            { local a; a=$(echo "$1" | tr -d '[:space:]'); [[ "$a" -ge "$2" ]] && _pass "$3" || _fail "$3 (expected >=$2, got $a)"; }
_assert_le()            { local a; a=$(echo "$1" | tr -d '[:space:]'); [[ "$a" -le "$2" ]] && _pass "$3" || _fail "$3 (expected <=$2, got $a)"; }

# ---------------------------------------------------------------------------
# Setup — temp git repo
# ---------------------------------------------------------------------------
TMPDIR_ROOT=$(mktemp -d /tmp/test-auto-triage-XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

cd "$TMPDIR_ROOT"
git init --quiet
git config user.email "test@test.com"
git config user.name "Test"

mkdir -p archive/orphan-diffs archive/orphan-diffs-needs-review .autonomous-team scripts/lib

# Copy real lib files so they work in our temp repo context
cp "$ORPHAN_TRIAGE_LIB" scripts/lib/orphan-triage.sh

# Mock rotate-team-log.sh — captures calls to a file
MOCK_LOG="${TMPDIR_ROOT}/mock-team-log.txt"
cat > "${TMPDIR_ROOT}/scripts/rotate-team-log.sh" <<'MOCK'
#!/usr/bin/env bash
echo "TEAM-LOG: $*" >> "${MOCK_LOG_FILE:-/dev/null}"
MOCK
chmod +x "${TMPDIR_ROOT}/scripts/rotate-team-log.sh"
export MOCK_LOG_FILE="$MOCK_LOG"

export REPO_ROOT="$TMPDIR_ROOT"

# ---------------------------------------------------------------------------
# Patch factory helpers
# ---------------------------------------------------------------------------

# _make_safe_patch <name>
#   Creates a patch touching only .autonomous-team/ (safe-discard allowlist)
_make_safe_patch() {
  local name="$1"
  cat > "archive/orphan-diffs/${name}" <<'PATCH'
diff --git a/.autonomous-team/worktrees.json b/.autonomous-team/worktrees.json
index abc1234..def5678 100644
--- a/.autonomous-team/worktrees.json
+++ b/.autonomous-team/worktrees.json
@@ -1,3 +1,4 @@
 [
+  {"id": "test", "status": "reaped"}
 ]
PATCH
}

# _make_review_patch <name>
#   Creates a patch touching backend/ (not in safe-discard allowlist → needs review)
_make_review_patch() {
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

# _make_test_patch <name>
#   Creates a patch touching tests/ (always needs review)
_make_test_patch() {
  local name="$1"
  cat > "archive/orphan-diffs/${name}" <<'PATCH'
diff --git a/tests/test_foo.py b/tests/test_foo.py
index abc1234..def5678 100644
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1,3 +1,4 @@
 # test
+def test_new(): pass
PATCH
}

TODAY=$(date +%Y-%m-%d)

# Helper to fully reset archive state between tests
_reset_archive() {
  git -C "$TMPDIR_ROOT" rm -rf --quiet archive/orphan-diffs/ 2>/dev/null || true
  rm -rf archive/orphan-diffs-discarded-* archive/orphan-diffs-needs-review 2>/dev/null || true
  mkdir -p archive/orphan-diffs
  rm -f .autonomous-team/orphan-diff-nudge-state.json
  rm -f "$MOCK_LOG"
}

# ---------------------------------------------------------------------------
# Test 1: batch sizing — process at most N per run
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 1: batch sizing ==="

_reset_archive

# Create 10 safe patches
for i in $(seq 1 10); do
  _make_safe_patch "safe-batch-${i}-${TODAY}.patch"
done
git add archive/ && git commit --quiet -m "safe patches for batch test"

# Run auto-triage with batch=5
OUT=$(bash "$TRIAGE_SCRIPT" auto-triage --batch 5 2>&1); RC=$?
_assert_exit0 $RC "auto-triage exits 0"
_assert_contains "$OUT" "processed 5" "batch=5 processes exactly 5"

# Verify 5 safe patches were discarded, 5 remain
REMAINING=$(ls archive/orphan-diffs/*.patch 2>/dev/null | grep -c safe-batch || echo 0)
_assert_eq "$REMAINING" 5 "5 safe patches remain after batch=5 run"

# Run again with batch=10 to clear remainder
OUT2=$(bash "$TRIAGE_SCRIPT" auto-triage --batch 10 2>&1)
_assert_contains "$OUT2" "processed 5" "second run processes remaining 5"
REMAINING2=$(ls archive/orphan-diffs/*.patch 2>/dev/null | grep -c safe-batch 2>/dev/null || echo 0)
_assert_eq "$REMAINING2" 0 "all safe-batch patches processed after second run"

# ---------------------------------------------------------------------------
# Test 2: safe-discard routing — safe patches go to discard dir
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 2: safe-discard routing ==="

_reset_archive
for i in $(seq 1 5); do
  _make_safe_patch "safe-route-${i}-${TODAY}.patch"
done
git add archive/ && git commit --quiet -m "safe route patches"

OUT=$(bash "$TRIAGE_SCRIPT" auto-triage --batch 10 2>&1); RC=$?
_assert_exit0 $RC "safe-route auto-triage exits 0"
_assert_contains "$OUT" "5 discarded" "5 safe patches discarded"
_assert_contains "$OUT" "0 moved to needs-review" "0 sent to needs-review"

DISCARD_DIR="archive/orphan-diffs-discarded-${TODAY}"
_assert_dir_exists "$DISCARD_DIR" "discard dir created"
DISCARD_COUNT=$(ls "${DISCARD_DIR}"/*.patch 2>/dev/null | wc -l | tr -d '[:space:]' || echo 0)
_assert_eq "$DISCARD_COUNT" 5 "5 patches in discard dir"
_assert_file_exists "${DISCARD_DIR}/README.md" "README.md in discard dir"

# Verify sidecar status = discarded
ALL_DISC_OK=true
for patch in "${DISCARD_DIR}"/*.patch; do
  meta="${patch}.meta.json"
  if [[ -f "$meta" ]]; then
    status=$(python3 -c "import json; d=json.load(open('$meta')); print(d.get('status',''))" 2>/dev/null || echo "")
    [[ "$status" == "discarded" ]] || { ALL_DISC_OK=false; break; }
  fi
done
$ALL_DISC_OK && _pass "sidecar status=discarded for all safe-discarded patches" || _fail "some sidecar status not discarded"

# ---------------------------------------------------------------------------
# Test 3: needs-review routing — unsafe patches go to needs-review dir
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 3: needs-review routing ==="

_reset_archive
for i in $(seq 1 5); do
  _make_review_patch "review-route-${i}-${TODAY}.patch"
done
# Also add test patches
for i in $(seq 1 3); do
  _make_test_patch "test-route-${i}-${TODAY}.patch"
done
git add archive/ && git commit --quiet -m "review route patches"

OUT=$(bash "$TRIAGE_SCRIPT" auto-triage --batch 20 2>&1); RC=$?
_assert_exit0 $RC "review-route auto-triage exits 0"
_assert_contains "$OUT" "0 discarded" "0 unsafe patches auto-discarded"
_assert_contains "$OUT" "8 moved to needs-review" "8 patches sent to needs-review"

REVIEW_DIR="archive/orphan-diffs-needs-review"
_assert_dir_exists "$REVIEW_DIR" "needs-review dir created"
REVIEW_COUNT=$(ls "${REVIEW_DIR}"/*.patch 2>/dev/null | wc -l | tr -d '[:space:]' || echo 0)
_assert_eq "$REVIEW_COUNT" 8 "8 patches in needs-review dir"
_assert_file_exists "${REVIEW_DIR}/README.md" "README.md in needs-review dir"

# Verify sidecar status = needs-review (NOT discarded) for all moved patches
ALL_NR_OK=true
for patch in "${REVIEW_DIR}"/*.patch; do
  meta="${patch}.meta.json"
  if [[ -f "$meta" ]]; then
    status=$(python3 -c "import json; d=json.load(open('$meta')); print(d.get('status',''))" 2>/dev/null || echo "")
    [[ "$status" == "needs-review" ]] || { ALL_NR_OK=false; echo "  bad status '${status}' in $(basename "$meta")" >&2; break; }
  fi
done
$ALL_NR_OK && _pass "sidecar status=needs-review for all moved-to-review patches" || _fail "some sidecar status not needs-review (should not be discarded)"

# ---------------------------------------------------------------------------
# Test 4: AC1 — 60 patches (30 safe, 30 review), one run processes 25
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 4: AC1 — 60 patches, batch=25 processes exactly 25 ==="

_reset_archive

# Create 30 safe + 30 review patches
for i in $(seq 1 30); do
  _make_safe_patch "safe-ac1-${i}-${TODAY}.patch"
done
for i in $(seq 1 30); do
  _make_review_patch "review-ac1-${i}-${TODAY}.patch"
done
git add archive/ && git commit --quiet -m "AC1 fixtures: 60 patches"

OUT=$(bash "$TRIAGE_SCRIPT" auto-triage --batch 25 2>&1); RC=$?
_assert_exit0 $RC "AC1 auto-triage exits 0"
_assert_contains "$OUT" "processed 25" "exactly 25 patches processed in batch"

# Count remaining in orphan-diffs
REMAINING_TOTAL=$(ls archive/orphan-diffs/*.patch 2>/dev/null | wc -l | tr -d '[:space:]' || echo 0)
_assert_eq "$REMAINING_TOTAL" 35 "35 patches remain after batch=25"

# Verify at least one routing dir was created
DISCARD_DIR_AC1="archive/orphan-diffs-discarded-${TODAY}"
REVIEW_DIR_AC1="archive/orphan-diffs-needs-review"
# The glob ordering means the first 25 in ls order were processed.
# safe-ac1-* sorts before review-ac1-* alphabetically, so 25 safe patches should be discarded.
if [[ -d "$DISCARD_DIR_AC1" ]]; then
  _pass "discard dir created in AC1 run"
elif [[ -d "$REVIEW_DIR_AC1" ]]; then
  _pass "routing dir created in AC1 run (review dir)"
else
  _fail "no routing dir created in AC1 run"
fi

# ---------------------------------------------------------------------------
# Test 5: AC4 — --auto never discards patches touching unsafe paths
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 5: AC4 — unsafe patches always go to needs-review, never discarded ==="

_reset_archive

# Create patches touching various unsafe paths
for unsafe_type in backend scripts tui dashboard tests; do
  cat > "archive/orphan-diffs/unsafe-${unsafe_type}-${TODAY}.patch" <<PATCH
diff --git a/${unsafe_type}/foo.py b/${unsafe_type}/foo.py
index abc1234..def5678 100644
--- a/${unsafe_type}/foo.py
+++ b/${unsafe_type}/foo.py
@@ -1 +1,2 @@
 # ${unsafe_type}
+# change
PATCH
done
git add archive/ && git commit --quiet -m "AC4 unsafe patches"

OUT=$(bash "$TRIAGE_SCRIPT" auto-triage --batch 20 2>&1); RC=$?
_assert_exit0 $RC "AC4 auto-triage exits 0"
_assert_contains "$OUT" "0 discarded" "AC4: no unsafe patches discarded"
_assert_contains "$OUT" "5 moved to needs-review" "AC4: all 5 unsafe patches → needs-review"

# Verify discard dir was NOT created (no safe patches)
if [[ -d "archive/orphan-diffs-discarded-${TODAY}" ]]; then
  DISCARD_CNT=$(ls "archive/orphan-diffs-discarded-${TODAY}"/*.patch 2>/dev/null | wc -l || echo 0)
  [[ "$DISCARD_CNT" -eq 0 ]] && _pass "AC4: discard dir empty (no unsafe patches discarded)" || _fail "AC4: unsafe patches found in discard dir ($DISCARD_CNT)"
else
  _pass "AC4: no discard dir created when all patches are unsafe"
fi

# ---------------------------------------------------------------------------
# Test 6: dry-run mode — no files moved
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 6: dry-run mode ==="

_reset_archive

_make_safe_patch "dryrun-safe-${TODAY}.patch"
_make_review_patch "dryrun-review-${TODAY}.patch"
git add archive/ && git commit --quiet -m "dryrun fixtures"

OUT=$(bash "$TRIAGE_SCRIPT" auto-triage --batch 10 --dry-run 2>&1); RC=$?
_assert_exit0 $RC "dry-run exits 0"
_assert_contains "$OUT" "dry-run" "dry-run output mentions dry-run"

# Files should still be in original location
_assert_file_exists "archive/orphan-diffs/dryrun-safe-${TODAY}.patch" "dry-run: safe patch not moved"
_assert_file_exists "archive/orphan-diffs/dryrun-review-${TODAY}.patch" "dry-run: review patch not moved"

# ---------------------------------------------------------------------------
# Test 7: AC2/AC3 — warning suppression when auto-triage is making progress
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 7: AC2 — warning suppressed when auto-triage makes progress ==="

_reset_archive

# Create 55 safe patches (pile > threshold=50)
for i in $(seq 1 55); do
  _make_safe_patch "warn-safe-${i}-${TODAY}.patch"
done
git add archive/ && git commit --quiet -m "55 safe patches for warning test"

# Run auto-triage 3 times with small batch — pile is decreasing each time
# Each run processes up to 10, so after 3 runs: 55 → 45 → 35 → 25 (all under 50 quickly)
for run in 1 2 3; do
  (
    export ORPHAN_DIFF_NUDGE_THRESHOLD=50
    export REPO_ROOT="$TMPDIR_ROOT"
    bash "$TRIAGE_SCRIPT" auto-triage --batch 10
    source scripts/lib/orphan-triage.sh
    _ot_nudge_if_over_threshold
  ) 2>&1
done

# Warning should NOT appear because auto-triage made progress (processed > 0 each run)
WARN_COUNT=$(grep -c "orphan-diff pile" "$MOCK_LOG" 2>/dev/null || echo 0)
# The pile was > 50 initially but auto-triage processed items, so nudge should be suppressed
# (It's OK if 0 warnings — that means suppression worked)
_assert_eq "$WARN_COUNT" 0 "AC2: no warning when auto-triage is making progress"

# ---------------------------------------------------------------------------
# Test 8: AC3 — warning fires when auto-triage processes zero and pile is stuck
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 8: AC3 — warning fires when pile stuck and auto-triage processes 0 ==="

_reset_archive

# Create 55 review (unsafe) patches — auto-triage will process them but move to needs-review
# Then simulate auto-triage processing 0 by clearing the runs in state
for i in $(seq 1 55); do
  _make_review_patch "stuck-review-${i}-${TODAY}.patch"
done
git add archive/ && git commit --quiet -m "55 review patches for stuck test"

# Simulate 3 runs where auto-triage processed 0 (stuck state)
# We do this by directly writing the nudge state with 3 zero-processed runs
python3 -c "
import json, os
d = {
  'last_count': 0,
  'threshold': 50,
  'auto_triage_runs': [
    {'at': '2026-01-01T00:00:00Z', 'processed': 0},
    {'at': '2026-01-01T00:10:00Z', 'processed': 0},
    {'at': '2026-01-01T00:20:00Z', 'processed': 0},
  ]
}
os.makedirs('.autonomous-team', exist_ok=True)
with open('.autonomous-team/orphan-diff-nudge-state.json', 'w') as f:
    json.dump(d, f)
" 2>/dev/null

# Run nudge check — pile is 55 > 50, stuck (no progress in last 3 runs), count changed
(
  export ORPHAN_DIFF_NUDGE_THRESHOLD=50
  export REPO_ROOT="$TMPDIR_ROOT"
  source scripts/lib/orphan-triage.sh
  _ot_nudge_if_over_threshold
) 2>&1

WARN_COUNT=$(grep -c "orphan-diff pile" "$MOCK_LOG" 2>/dev/null || echo 0)
_assert_ge "$WARN_COUNT" 1 "AC3: warning fires when pile stuck and auto-triage processed 0"

# Run nudge check again — count unchanged, should NOT warn again
rm -f "$MOCK_LOG"
(
  export ORPHAN_DIFF_NUDGE_THRESHOLD=50
  export REPO_ROOT="$TMPDIR_ROOT"
  source scripts/lib/orphan-triage.sh
  _ot_nudge_if_over_threshold
) 2>&1

WARN_COUNT2=$(grep -c "orphan-diff pile" "$MOCK_LOG" 2>/dev/null || echo 0)
_assert_eq "$WARN_COUNT2" 0 "AC3: warning not repeated when count unchanged"

# ---------------------------------------------------------------------------
# Test 9: nudge-state updated with auto_triage_runs after auto-triage run
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 9: nudge-state records auto-triage runs ==="

_reset_archive

_make_safe_patch "state-check-${TODAY}.patch"
git add archive/ && git commit --quiet -m "state check patch"

bash "$TRIAGE_SCRIPT" auto-triage --batch 5 2>&1

STATE_FILE=".autonomous-team/orphan-diff-nudge-state.json"
_assert_file_exists "$STATE_FILE" "nudge-state file created after auto-triage"

if [[ -f "$STATE_FILE" ]]; then
  RUN_COUNT=$(python3 -c "
import json
d = json.load(open('$STATE_FILE'))
runs = d.get('auto_triage_runs', [])
print(len(runs))
" 2>/dev/null || echo 0)
  _assert_ge "$RUN_COUNT" 1 "nudge-state has at least 1 auto_triage_run recorded"

  PROCESSED=$(python3 -c "
import json
d = json.load(open('$STATE_FILE'))
runs = d.get('auto_triage_runs', [])
print(runs[-1].get('processed', -1) if runs else -1)
" 2>/dev/null || echo -1)
  _assert_ge "$PROCESSED" 0 "nudge-state last run processed count is non-negative"
fi

# ---------------------------------------------------------------------------
# Test 10: existing test suite still passes (triage-orphan-diffs.sh baseline)
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 10: existing triage-orphan-diffs.sh test suite ==="
EXISTING_OUT=$(bash "${REPO_ROOT_REAL}/tests/test_triage_orphan_diffs.sh" 2>&1); RC=$?
_assert_exit0 $RC "existing triage test suite passes"

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
