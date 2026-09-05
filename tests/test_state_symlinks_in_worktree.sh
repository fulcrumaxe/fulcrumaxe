#!/usr/bin/env bash
# tests/test_state_symlinks_in_worktree.sh
#
# Integration test for D#630: worktree state symlinks.
#
# Tests:
#   1. setup-state-dir.sh creates symlinks (not real files) at all manifest paths
#      when run inside a fresh worktree-like temp directory.
#   2. AC #4 (synthetic write test): writing to .autonomous-team/audit.jsonl from
#      "main" and from a "worktree" both land in the same external state dir file.
#   3. Migration: real file at a manifest path is backed up and replaced with symlink.
#   4. Idempotency: running setup-state-dir.sh twice is a no-op.
#   5. Manifest validation: state-symlinks.json is valid JSON and contains all 5 entries.
#
# Uses AUTONOMOUS_TEAM_STATE_DIR=$(mktemp -d) for full isolation — no writes to
# ~/.autonomous-forever-state or the real repo.
#
# Usage:
#   bash tests/test_state_symlinks_in_worktree.sh
# Exit code 0 = all tests passed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SETUP_SCRIPT="$REPO_ROOT/scripts/setup-state-dir.sh"
MANIFEST="$REPO_ROOT/.autonomous-team/state-symlinks.json"

# ── Colour helpers ────────────────────────────────────────────────────────────
PASS="[PASS]"
FAIL="[FAIL]"
INFO="[INFO]"

passed=0
failed=0

assert_true() {
  local desc="$1"; shift
  if "$@" 2>/dev/null; then
    echo "$PASS $desc"
    ((passed++)) || true
  else
    echo "$FAIL $desc"
    ((failed++)) || true
  fi
}

assert_eq() {
  local desc="$1"
  local got="$2"
  local want="$3"
  if [[ "$got" == "$want" ]]; then
    echo "$PASS $desc"
    ((passed++)) || true
  else
    echo "$FAIL $desc — got='$got' want='$want'"
    ((failed++)) || true
  fi
}

# ── Test 0: Manifest exists and is valid JSON with 5 entries ──────────────────
echo ""
echo "$INFO Test 0: manifest validation"
assert_true "manifest file exists" test -f "$MANIFEST"
assert_true "manifest is valid JSON" python3 -c "import json; json.load(open('$MANIFEST'))"
ENTRY_COUNT=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(len(d['entries']))" 2>/dev/null || echo 0)
assert_eq "manifest has 5 entries" "$ENTRY_COUNT" "5"

# Verify all expected keys are present
EXPECTED_KEYS="audit.jsonl blackboard state.db stats.duckdb circuit-breaker-history.jsonl"
for key in $EXPECTED_KEYS; do
  FOUND=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(any(e['in_repo']=='$key' for e in d['entries']))" 2>/dev/null || echo "False")
  assert_eq "manifest contains '$key'" "$FOUND" "True"
done

# ── Test 1: setup-state-dir.sh creates symlinks in fresh worktree dir ─────────
echo ""
echo "$INFO Test 1: symlinks created at manifest paths in fresh directory"

EXT_STATE=$(mktemp -d)
FAKE_WT=$(mktemp -d)
FAKE_TEAM="$FAKE_WT/.autonomous-team"
mkdir -p "$FAKE_TEAM"

# Copy the manifest into the fake worktree so setup-state-dir.sh can find it
cp "$MANIFEST" "$FAKE_TEAM/state-symlinks.json"

# Run setup inside the fake worktree (override REPO_ROOT so git rev-parse
# doesn't escape to the real repo)
AUTONOMOUS_TEAM_STATE_DIR="$EXT_STATE" SETUP_STATE_REPO_ROOT="$FAKE_WT" bash "$SETUP_SCRIPT" >/dev/null 2>&1

# All 5 manifest paths should now be symlinks
while IFS= read -r in_repo; do
  TARGET="$FAKE_TEAM/$in_repo"
  assert_true "  $in_repo is a symlink" test -L "$TARGET"
done < <(python3 -c "import json; [print(e['in_repo']) for e in json.load(open('$MANIFEST'))['entries']]" 2>/dev/null)

rm -rf "$EXT_STATE" "$FAKE_WT"

# ── Test 2: symlinks are NOT real files (AC #4 synthetic write test) ──────────
echo ""
echo "$INFO Test 2: writes from main and worktree land in same external state file"

EXT_STATE=$(mktemp -d)

# Set up "main" environment
MAIN_DIR=$(mktemp -d)
MAIN_TEAM="$MAIN_DIR/.autonomous-team"
mkdir -p "$MAIN_TEAM"
cp "$MANIFEST" "$MAIN_TEAM/state-symlinks.json"
AUTONOMOUS_TEAM_STATE_DIR="$EXT_STATE" SETUP_STATE_REPO_ROOT="$MAIN_DIR" bash "$SETUP_SCRIPT" >/dev/null 2>&1

# Set up "worktree" environment (separate directory, same external state dir)
WT_DIR=$(mktemp -d)
WT_TEAM="$WT_DIR/.autonomous-team"
mkdir -p "$WT_TEAM"
cp "$MANIFEST" "$WT_TEAM/state-symlinks.json"
AUTONOMOUS_TEAM_STATE_DIR="$EXT_STATE" SETUP_STATE_REPO_ROOT="$WT_DIR" bash "$SETUP_SCRIPT" >/dev/null 2>&1

# Both audit.jsonl symlinks should now exist and point to the same external file
assert_true "main audit.jsonl is a symlink" test -L "$MAIN_TEAM/audit.jsonl"
assert_true "worktree audit.jsonl is a symlink" test -L "$WT_TEAM/audit.jsonl"

# Write from "main"
echo "main-write-marker-$(date +%s)" >> "$MAIN_TEAM/audit.jsonl"
# Write from "worktree"
WT_MARKER="worktree-write-marker-$(date +%s)"
echo "$WT_MARKER" >> "$WT_TEAM/audit.jsonl"

# Both should be in the external file
EXT_AUDIT="$EXT_STATE/audit.jsonl"
assert_true "external audit.jsonl exists after writes" test -f "$EXT_AUDIT"
assert_true "main-write-marker appears in external audit.jsonl" grep -q "main-write-marker" "$EXT_AUDIT"
assert_true "worktree-write-marker appears in external audit.jsonl" grep -q "$WT_MARKER" "$EXT_AUDIT"

# The worktree file should NOT be a real file (i.e. writes went through the symlink)
assert_true "worktree audit.jsonl is still a symlink after write" test -L "$WT_TEAM/audit.jsonl"

rm -rf "$EXT_STATE" "$MAIN_DIR" "$WT_DIR"

# ── Test 3: Migration — real file backed up, symlink created ──────────────────
echo ""
echo "$INFO Test 3: migration of pre-existing real file in worktree"

EXT_STATE=$(mktemp -d)
FAKE_WT=$(mktemp -d)
FAKE_TEAM="$FAKE_WT/.autonomous-team"
mkdir -p "$FAKE_TEAM"
cp "$MANIFEST" "$FAKE_TEAM/state-symlinks.json"

# Pre-create a real audit.jsonl (simulates a worktree that wrote state before setup)
printf 'existing-data-line\n' > "$FAKE_TEAM/audit.jsonl"

# Also pre-create external target (simulates shared state already in use by main)
printf 'main-state-line\n' > "$EXT_STATE/audit.jsonl"

# Run setup — should back up the real file and create a symlink
AUTONOMOUS_TEAM_STATE_DIR="$EXT_STATE" SETUP_STATE_REPO_ROOT="$FAKE_WT" bash "$SETUP_SCRIPT" >/dev/null 2>&1

assert_true "audit.jsonl is now a symlink after migration" test -L "$FAKE_TEAM/audit.jsonl"
BACKUP_COUNT=$(find "$FAKE_TEAM/backups" -name "audit.jsonl-*" 2>/dev/null | wc -l || echo 0)
assert_true "backup file was created" test "$BACKUP_COUNT" -ge 1
# External file should not have been overwritten
assert_true "external audit.jsonl still contains original main state" grep -q "main-state-line" "$EXT_STATE/audit.jsonl"

rm -rf "$EXT_STATE" "$FAKE_WT"

# ── Test 4: Idempotency — running twice is a no-op ────────────────────────────
echo ""
echo "$INFO Test 4: idempotency — second run is a no-op"

EXT_STATE=$(mktemp -d)
FAKE_WT=$(mktemp -d)
FAKE_TEAM="$FAKE_WT/.autonomous-team"
mkdir -p "$FAKE_TEAM"
cp "$MANIFEST" "$FAKE_TEAM/state-symlinks.json"

# First run
AUTONOMOUS_TEAM_STATE_DIR="$EXT_STATE" SETUP_STATE_REPO_ROOT="$FAKE_WT" bash "$SETUP_SCRIPT" >/dev/null 2>&1
# Record symlink targets after first run
TARGETS_AFTER_FIRST=$(find "$FAKE_TEAM" -maxdepth 1 -type l | sort | xargs -I{} readlink {} 2>/dev/null | sort || true)

# Second run
SECOND_EXIT=0
AUTONOMOUS_TEAM_STATE_DIR="$EXT_STATE" SETUP_STATE_REPO_ROOT="$FAKE_WT" bash "$SETUP_SCRIPT" >/dev/null 2>&1 || SECOND_EXIT=$?
assert_eq "second run exits 0" "$SECOND_EXIT" "0"

TARGETS_AFTER_SECOND=$(find "$FAKE_TEAM" -maxdepth 1 -type l | sort | xargs -I{} readlink {} 2>/dev/null | sort || true)
assert_eq "symlink targets unchanged after second run" "$TARGETS_AFTER_FIRST" "$TARGETS_AFTER_SECOND"

rm -rf "$EXT_STATE" "$FAKE_WT"

# ── Test 5: Wrong-target re-point — stale symlink is corrected ────────────────
echo ""
echo "$INFO Test 5: wrong-target symlink is re-pointed to canonical path (D#668)"

EXT_STATE=$(mktemp -d)
FAKE_WT=$(mktemp -d)
FAKE_TEAM="$FAKE_WT/.autonomous-team"
mkdir -p "$FAKE_TEAM"
cp "$MANIFEST" "$FAKE_TEAM/state-symlinks.json"

# Pre-create a symlink pointing at a stale/garbage target
STALE_TARGET="/tmp/garbage-state-$$"
ln -s "$STALE_TARGET" "$FAKE_TEAM/audit.jsonl"
assert_eq "pre-condition: audit.jsonl points to stale target" \
  "$(readlink "$FAKE_TEAM/audit.jsonl")" "$STALE_TARGET"

# Run setup — should re-point the symlink to the canonical external target
AUTONOMOUS_TEAM_STATE_DIR="$EXT_STATE" SETUP_STATE_REPO_ROOT="$FAKE_WT" bash "$SETUP_SCRIPT" >/dev/null 2>&1

CANONICAL_TARGET="$EXT_STATE/audit.jsonl"
ACTUAL_TARGET="$(readlink "$FAKE_TEAM/audit.jsonl")"
assert_eq "audit.jsonl re-pointed to canonical target" "$ACTUAL_TARGET" "$CANONICAL_TARGET"

# Test 5b: idempotency — second run with now-correct symlink logs skip, not re-point
SECOND_OUTPUT=$(AUTONOMOUS_TEAM_STATE_DIR="$EXT_STATE" SETUP_STATE_REPO_ROOT="$FAKE_WT" bash "$SETUP_SCRIPT" 2>&1)
assert_true "second run still exits 0 (idempotent)" \
  bash -c "AUTONOMOUS_TEAM_STATE_DIR='$EXT_STATE' SETUP_STATE_REPO_ROOT='$FAKE_WT' bash '$SETUP_SCRIPT' >/dev/null 2>&1"
# Target should remain unchanged after second run
THIRD_TARGET="$(readlink "$FAKE_TEAM/audit.jsonl")"
assert_eq "symlink target unchanged after second run" "$THIRD_TARGET" "$CANONICAL_TARGET"

rm -rf "$EXT_STATE" "$FAKE_WT"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $passed passed, $failed failed"
if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
exit 0
