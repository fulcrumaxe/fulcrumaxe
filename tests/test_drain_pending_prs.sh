#!/usr/bin/env bash
# tests/test_drain_pending_prs.sh — unit tests for scripts/drain-pending-prs.sh
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic temp dirs and --dry-run to avoid real GitHub API calls.
#
# Usage:
#   bash tests/test_drain_pending_prs.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DRAIN_SCRIPT="$REPO_ROOT/scripts/drain-pending-prs.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; ((PASS++)); }
fail() { echo "  FAIL: $1"; ((FAIL++)); ERRORS+=("$1"); }

# ── Setup / teardown helpers ──────────────────────────────────────────────────

setup() {
  TEST_DIR=$(mktemp -d)
  mkdir -p "$TEST_DIR/.autonomous-team"
  # Override the repo-root lookup by overriding PENDING_FILE via symlink
  PENDING_FILE_OVERRIDE="$TEST_DIR/.autonomous-team/pending-prs.json"
}

teardown() {
  rm -rf "${TEST_DIR:-}"
}

# ── Test 1: absent pending-prs.json exits 0 with no-op message ───────────────

echo ""
echo "Test 1: absent pending-prs.json — exits 0, prints no-drain message"
setup

# Point script at a non-existent file by patching REPO_ROOT via env
OUT=$(REPO_ROOT="$TEST_DIR" bash "$DRAIN_SCRIPT" 2>&1)
RC=$?

if [ "$RC" -eq 0 ]; then
  pass "exit code 0"
else
  fail "exit code should be 0 (got $RC)"
fi

if echo "$OUT" | grep -q "nothing to drain\|No pending-prs"; then
  pass "no-drain message printed"
else
  fail "expected no-drain message, got: $OUT"
fi

teardown

# ── Test 2: empty array exits 0 and prints queue-is-empty message ─────────────

echo ""
echo "Test 2: empty array in pending-prs.json — exits 0"
setup

echo "[]" > "$TEST_DIR/.autonomous-team/pending-prs.json"

OUT=$(REPO_ROOT="$TEST_DIR" bash "$DRAIN_SCRIPT" 2>&1)
RC=$?

if [ "$RC" -eq 0 ]; then
  pass "exit code 0"
else
  fail "exit code should be 0 (got $RC)"
fi

if echo "$OUT" | grep -q "Queue is empty\|empty\|nothing to drain"; then
  pass "empty-queue message printed"
else
  fail "expected empty-queue message, got: $OUT"
fi

teardown

# ── Test 3: --dry-run processes entries without writing back ───────────────────

echo ""
echo "Test 3: --dry-run with one entry — prints dry-run output, does not modify file"
setup

cat > "$TEST_DIR/.autonomous-team/pending-prs.json" <<'EOF'
[
  {"branch": "disc-99-test", "title": "#99: test PR", "body": "test body", "discussion": "99"}
]
EOF

BEFORE=$(cat "$TEST_DIR/.autonomous-team/pending-prs.json")

OUT=$(REPO_ROOT="$TEST_DIR" bash "$DRAIN_SCRIPT" --dry-run 2>&1)
RC=$?

AFTER=$(cat "$TEST_DIR/.autonomous-team/pending-prs.json" 2>/dev/null || echo "")

if [ "$RC" -eq 0 ]; then
  pass "exit code 0"
else
  fail "exit code should be 0 (got $RC)"
fi

if echo "$OUT" | grep -qi "dry.run\|DRY"; then
  pass "dry-run output printed"
else
  fail "expected dry-run message in output, got: $OUT"
fi

if [ "$BEFORE" = "$AFTER" ]; then
  pass "pending-prs.json not modified by dry-run"
else
  fail "pending-prs.json was modified during dry-run"
fi

teardown

# ── Test 4: malformed JSON in pending-prs.json exits 0 gracefully ─────────────

echo ""
echo "Test 4: malformed JSON — exits 0 without crashing"
setup

echo "THIS IS NOT JSON" > "$TEST_DIR/.autonomous-team/pending-prs.json"

OUT=$(REPO_ROOT="$TEST_DIR" bash "$DRAIN_SCRIPT" 2>&1)
RC=$?

if [ "$RC" -eq 0 ]; then
  pass "exit code 0 on malformed JSON"
else
  fail "should exit 0 on malformed JSON (got $RC)"
fi

teardown

# ── Test 5: entry with missing branch field is skipped ────────────────────────

echo ""
echo "Test 5: entry with no branch — skipped, script exits 0"
setup

cat > "$TEST_DIR/.autonomous-team/pending-prs.json" <<'EOF'
[
  {"title": "no branch here", "body": "body", "discussion": "42"}
]
EOF

OUT=$(REPO_ROOT="$TEST_DIR" bash "$DRAIN_SCRIPT" --dry-run 2>&1)
RC=$?

if [ "$RC" -eq 0 ]; then
  pass "exit code 0"
else
  fail "exit code should be 0 (got $RC)"
fi

if echo "$OUT" | grep -qi "malformed\|missing\|skipping"; then
  pass "malformed-entry message printed"
else
  fail "expected malformed-entry message in output, got: $OUT"
fi

teardown

# ── Test 6: file is removed when all entries successfully processed ────────────

echo ""
echo "Test 6: dry-run of single entry — pending file still exists (dry-run doesn't write)"
setup

cat > "$TEST_DIR/.autonomous-team/pending-prs.json" <<'EOF'
[
  {"branch": "some-branch", "title": "Some PR", "body": "body", "discussion": "5"}
]
EOF

REPO_ROOT="$TEST_DIR" bash "$DRAIN_SCRIPT" --dry-run >/dev/null 2>&1
RC=$?

# In dry-run mode the file is NOT removed — real removal only happens on actual success
if [ -f "$TEST_DIR/.autonomous-team/pending-prs.json" ]; then
  pass "pending-prs.json preserved in dry-run (correct)"
else
  fail "pending-prs.json should NOT be removed in dry-run"
fi

if [ "$RC" -eq 0 ]; then
  pass "exit code 0"
else
  fail "exit code should be 0 (got $RC)"
fi

teardown

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [ "${#ERRORS[@]}" -gt 0 ]; then
  echo "Failed tests:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi

exit 0
