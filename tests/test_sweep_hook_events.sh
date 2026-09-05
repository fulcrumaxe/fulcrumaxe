#!/usr/bin/env bash
# tests/test_sweep_hook_events.sh — verify retention sweep for hook-events/
#
# Creates synthetic old and new marker/lock/done/blocks files in a temp directory,
# runs sweep-hook-events.sh, and asserts that only the right files are removed.
#
# Run from repo root:
#   bash tests/test_sweep_hook_events.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SWEEP="$REPO_ROOT/scripts/sweep-hook-events.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; ((PASS++)) || true; }
fail() { echo "  FAIL: $1 — $2"; ((FAIL++)) || true; }

assert_exists()  { [[ -e "$1" ]] && pass "$2" || fail "$2" "expected to exist: $1"; }
assert_missing() { [[ ! -e "$1" ]] && pass "$2" || fail "$2" "expected to be gone: $1"; }

# ── Setup temp hook-events directory ──────────────────────────────────────────
TMPDIR_BASE=$(mktemp -d "/tmp/test-sweep-hook-events-XXXXXX")
HOOK_DIR="$TMPDIR_BASE/hook-events"
DONE_DIR="$HOOK_DIR/done"
mkdir -p "$HOOK_DIR" "$DONE_DIR"
trap 'rm -rf "$TMPDIR_BASE"' EXIT

# Helper: create a file with an artificially old mtime
make_old_file() {
  local path="$1"
  local days_old="$2"
  touch "$path"
  # touch -d "-Nd" sets mtime to N days ago
  touch -d "-${days_old} days" "$path"
}

echo "=== sweep-hook-events retention tests ==="
echo "HOOK_DIR: $HOOK_DIR"

# ── Create test fixtures ───────────────────────────────────────────────────────

# 1. Stale lock files (> 24h) → should be deleted
make_old_file "$HOOK_DIR/stale-lock-aaaa1111.lock"  2   # 2 days old
make_old_file "$HOOK_DIR/stale-lock-bbbb2222.lock"  1   # 1 day old (just over 24h)

# 2. Fresh lock file (< 24h) → must be preserved
touch         "$HOOK_DIR/fresh-lock-cccc3333.lock"       # just created (< 1 hour)

# 3. Orphan markers (> 48h, no done/ entry, no active lock) → should be deleted
make_old_file "$HOOK_DIR/orphan-aaaa1111.json"  3        # 3 days old, no done/ no lock
make_old_file "$HOOK_DIR/orphan-bbbb2222.json"  2        # 2 days old, no done/ no lock

# 4. Orphan marker with fresh lock → must NOT be deleted (hook still running)
make_old_file "$HOOK_DIR/active-dddd4444.json"  3        # marker is old
touch         "$HOOK_DIR/active-dddd4444.lock"           # but lock is fresh → skip

# 5. Recent marker (< 48h) with no done/ entry → must be preserved
make_old_file "$HOOK_DIR/recent-eeee5555.json"  1        # only 1 day old

# 6. done/ entries older than 7 days → should be deleted
make_old_file "$DONE_DIR/old-done-aaaa1111.json" 10      # 10 days old
make_old_file "$DONE_DIR/old-done-bbbb2222.json"  8      # 8 days old

# 7. done/ entries younger than 7 days → must be preserved
make_old_file "$DONE_DIR/recent-done-cccc3333.json" 5    # 5 days old
touch         "$DONE_DIR/fresh-done-dddd4444.json"       # just created

# 8. blocks-YYYY-MM-DD.jsonl older than 7 days → should be gzipped
make_old_file "$HOOK_DIR/blocks-2026-05-01.jsonl" 17     # 17 days old
make_old_file "$HOOK_DIR/blocks-2026-05-08.jsonl"  9     # 9 days old

# 9. Recent blocks file → must stay uncompressed
make_old_file "$HOOK_DIR/blocks-2026-05-17.jsonl"  1     # 1 day old

# ── Run the sweep ──────────────────────────────────────────────────────────────
bash "$SWEEP" --hook-dir "$HOOK_DIR"

echo ""
echo "--- Post-sweep assertions ---"

# Pass 1: stale locks deleted
assert_missing "$HOOK_DIR/stale-lock-aaaa1111.lock" "stale lock (2d) deleted"
assert_missing "$HOOK_DIR/stale-lock-bbbb2222.lock" "stale lock (1d) deleted"

# Fresh lock preserved
assert_exists  "$HOOK_DIR/fresh-lock-cccc3333.lock" "fresh lock preserved"

# Pass 2: orphan markers deleted
assert_missing "$HOOK_DIR/orphan-aaaa1111.json" "orphan marker (3d) deleted"
assert_missing "$HOOK_DIR/orphan-bbbb2222.json" "orphan marker (2d) deleted"

# Active marker with fresh lock preserved
assert_exists  "$HOOK_DIR/active-dddd4444.json" "active marker (fresh lock) preserved"
assert_exists  "$HOOK_DIR/active-dddd4444.lock" "active lock preserved"

# Recent marker preserved
assert_exists  "$HOOK_DIR/recent-eeee5555.json" "recent marker (1d) preserved"

# Pass 3: old done/ entries deleted
assert_missing "$DONE_DIR/old-done-aaaa1111.json" "done entry (10d) deleted"
assert_missing "$DONE_DIR/old-done-bbbb2222.json" "done entry (8d) deleted"

# Recent done/ entries preserved
assert_exists  "$DONE_DIR/recent-done-cccc3333.json" "done entry (5d) preserved"
assert_exists  "$DONE_DIR/fresh-done-dddd4444.json"  "fresh done entry preserved"

# Pass 4: old blocks files gzipped
assert_missing "$HOOK_DIR/blocks-2026-05-01.jsonl"    "old blocks (17d) .jsonl removed (gzipped)"
assert_exists  "$HOOK_DIR/blocks-2026-05-01.jsonl.gz" "old blocks (17d) .gz created"
assert_missing "$HOOK_DIR/blocks-2026-05-08.jsonl"    "old blocks (9d) .jsonl removed (gzipped)"
assert_exists  "$HOOK_DIR/blocks-2026-05-08.jsonl.gz" "old blocks (9d) .gz created"

# Recent blocks file untouched
assert_exists  "$HOOK_DIR/blocks-2026-05-17.jsonl"    "recent blocks (1d) preserved as .jsonl"
assert_missing "$HOOK_DIR/blocks-2026-05-17.jsonl.gz" "recent blocks (1d) NOT gzipped"

# ── Dry-run mode: no changes ──────────────────────────────────────────────────
echo ""
echo "--- Dry-run test ---"

# Create a fresh old lock in a second temp dir
TMPDIR_DRY=$(mktemp -d "/tmp/test-sweep-dry-XXXXXX")
HOOK_DIR_DRY="$TMPDIR_DRY/hook-events"
mkdir -p "$HOOK_DIR_DRY/done"
trap 'rm -rf "$TMPDIR_DRY"' EXIT

make_old_file "$HOOK_DIR_DRY/dry-lock-1234.lock" 2
make_old_file "$HOOK_DIR_DRY/done/dry-done-5678.json" 10
make_old_file "$HOOK_DIR_DRY/blocks-2026-05-01.jsonl" 17

DRY_OUT=$(bash "$SWEEP" --hook-dir "$HOOK_DIR_DRY" --dry-run 2>&1)

# Files must still exist
assert_exists  "$HOOK_DIR_DRY/dry-lock-1234.lock"         "dry-run: lock not deleted"
assert_exists  "$HOOK_DIR_DRY/done/dry-done-5678.json"    "dry-run: done entry not deleted"
assert_exists  "$HOOK_DIR_DRY/blocks-2026-05-01.jsonl"    "dry-run: blocks not gzipped"
assert_missing "$HOOK_DIR_DRY/blocks-2026-05-01.jsonl.gz" "dry-run: no .gz created"

# Dry-run output mentions what would happen
if echo "$DRY_OUT" | grep -q "DRY-RUN"; then
  pass "dry-run output mentions DRY-RUN"
else
  fail "dry-run output mentions DRY-RUN" "no 'DRY-RUN' in output: $DRY_OUT"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "=== Results ==="
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [[ "$FAIL" -gt 0 ]]; then
  echo "FAILED" >&2
  exit 1
else
  echo "ALL TESTS PASSED"
  exit 0
fi
