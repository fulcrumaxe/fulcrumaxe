#!/usr/bin/env bash
# tests/test_sweep_stale_worktrees.sh — unit tests for scripts/sweep-stale-worktrees.sh
#
# Covers the D#1616 security-review fix:
#   1. Bare (no-flag) invocation is dry-run by default — zero worktrees removed.
#   2. --apply performs real removal AND appends one audit.jsonl row per removal.
#   3. Registry active-worktree exclusion reads the correct `worktree_id` key
#      (previously read the wrong key `id` and never actually protected anything)
#      and genuinely protects a registered-active worktree from --apply removal.
#
# Entirely self-contained: builds a throwaway git repo + worktrees under a
# mktemp dir per test. Never touches the real .claude/worktrees/ registry,
# the real audit.jsonl, or performs a live bulk removal against this repo.
#
# Usage:
#   bash tests/test_sweep_stale_worktrees.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; ((PASS++)); }
fail() { echo "  FAIL: $1"; ((FAIL++)); ERRORS+=("$1"); }

# ── Fixture: throwaway repo with one stale, clean, aged worktree ───────────
# Produces (all under $1, a fresh mktemp -d):
#   $1/repo                              — main working copy; acts as
#                                           REPO_ROOT for the copied script
#   $1/repo/.claude/worktrees/wt-stale   — linked worktree: clean, >20 commits
#                                           behind, mtime aged 2h
#
# D#1809 Lane A: wt-stale lives under .claude/worktrees/ so it stays eligible
# once the path-scope guard lands — a worktree that lives OUTSIDE that dir is
# its own dedicated case (see test_path_scope_refuses_outside_tree_but_still_removes_intree).
_build_fixture() {
  local base="$1"
  local origin="$base/origin.git"
  local repo="$base/repo"

  git init --quiet --bare "$origin"
  git clone --quiet "$origin" "$repo"
  (
    cd "$repo" || exit 1
    git config user.email "test@example.com"
    git config user.name "test"
    echo "init" > f.txt
    git add f.txt
    git commit --quiet -m init
    git branch -M main
    git push --quiet origin main
  )

  mkdir -p "$repo/.claude/worktrees"

  # Linked worktree on its own branch, pinned at the initial commit.
  (
    cd "$repo" || exit 1
    git worktree add --quiet -b wt-stale-branch "$repo/.claude/worktrees/wt-stale" main
  )

  # Advance origin/main by >20 commits so the worktree is stale.
  (
    cd "$repo" || exit 1
    for i in $(seq 1 25); do
      echo "line $i" >> f.txt
      git add f.txt
      git commit --quiet -m "advance $i"
    done
    git push --quiet origin main
    git fetch --quiet origin main
  )

  # Age the worktree dir past the 1h guard.
  touch -d "2 hours ago" "$repo/.claude/worktrees/wt-stale" 2>/dev/null || true

  mkdir -p "$repo/scripts/lib" "$repo/.autonomous-team"
  cp "$REPO_ROOT/scripts/sweep-stale-worktrees.sh" "$repo/scripts/"
  cp "$REPO_ROOT/scripts/lib/worktree-registry.sh" "$repo/scripts/lib/"
  cp "$REPO_ROOT/scripts/lib/worktree-claims.sh" "$repo/scripts/lib/"
  cp "$REPO_ROOT/scripts/lib/repo-resolve.sh" "$repo/scripts/lib/"
}

# ── Test 1: bare invocation defaults to dry-run, zero changes ──────────────
test_default_is_dry_run() {
  local base; base=$(mktemp -d)
  _build_fixture "$base"
  local state_dir="$base/state"
  mkdir -p "$state_dir"

  local out
  out=$(cd "$base/repo" && AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/sweep-stale-worktrees.sh 2>&1)

  if [[ -d "$base/repo/.claude/worktrees/wt-stale" ]] \
     && echo "$out" | grep -q "dry_run=true" \
     && echo "$out" | grep -q "would remove: wt-stale" \
     && [[ ! -f "$state_dir/audit.jsonl" ]]; then
    pass "bare invocation (no flags) defaults to dry-run: zero removals, no audit row"
  else
    fail "bare invocation did not behave as dry-run"
    echo "$out" | sed 's/^/    /'
  fi

  rm -rf "$base"
}

# ── Test 2: --apply performs real removal and writes an audit.jsonl row ────
test_apply_removes_and_audits() {
  local base; base=$(mktemp -d)
  _build_fixture "$base"
  local state_dir="$base/state"
  mkdir -p "$state_dir"

  local out
  out=$(cd "$base/repo" && AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/sweep-stale-worktrees.sh --apply 2>&1)

  if [[ ! -d "$base/repo/.claude/worktrees/wt-stale" ]] \
     && [[ -f "$state_dir/audit.jsonl" ]] \
     && grep -q '"kind":"stale_worktree_removed"' "$state_dir/audit.jsonl" \
     && grep -q '"worktree_id":"wt-stale"' "$state_dir/audit.jsonl" \
     && grep -q '"reason":"stale-worktree-sweep"' "$state_dir/audit.jsonl" \
     && grep -qE '"behind":[0-9]+' "$state_dir/audit.jsonl" \
     && grep -qE '"timestamp":"[0-9TZ:-]+"' "$state_dir/audit.jsonl"; then
    pass "--apply removes the eligible worktree and writes a complete audit.jsonl row"
  else
    fail "--apply removal or audit row was incomplete"
    echo "$out" | sed 's/^/    /'
    echo "-- audit.jsonl --"
    cat "$state_dir/audit.jsonl" 2>/dev/null | sed 's/^/    /'
  fi

  rm -rf "$base"
}

# ── Test 3: --yes is accepted as an alias for --apply ──────────────────────
test_yes_alias_removes() {
  local base; base=$(mktemp -d)
  _build_fixture "$base"
  local state_dir="$base/state"
  mkdir -p "$state_dir"

  local out
  out=$(cd "$base/repo" && AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/sweep-stale-worktrees.sh --yes 2>&1)

  if [[ ! -d "$base/repo/.claude/worktrees/wt-stale" ]] && echo "$out" | grep -q "dry_run=false"; then
    pass "--yes is accepted as a real-removal alias for --apply"
  else
    fail "--yes did not trigger real removal"
    echo "$out" | sed 's/^/    /'
  fi

  rm -rf "$base"
}

# ── Test 4: registry active-check uses worktree_id key and actually protects ─
test_registry_active_protects_via_worktree_id_key() {
  local base; base=$(mktemp -d)
  _build_fixture "$base"
  local state_dir="$base/state"
  mkdir -p "$state_dir"

  (
    cd "$base/repo" || exit 1
    bash scripts/lib/worktree-registry.sh register \
      --id wt-stale --role executor --path "$base/repo/.claude/worktrees/wt-stale" --pid $$ >/dev/null 2>&1
  )

  local out
  out=$(cd "$base/repo" && AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/sweep-stale-worktrees.sh --apply 2>&1)

  if [[ -d "$base/repo/.claude/worktrees/wt-stale" ]] && echo "$out" | grep -q "skipped (active/registered): 1"; then
    pass "registered-active worktree (worktree_id key) is protected from --apply removal"
  else
    fail "registered-active worktree was removed despite active registration — worktree_id key regression"
    echo "$out" | sed 's/^/    /'
  fi

  rm -rf "$base"
}

# ── Test 5 (D#1809 A1 + A3): out-of-tree worktree refused, in-tree eligible ──
# worktree still removed in the SAME run. A guard that refuses everything
# would pass A1 alone and be useless — A3 is what proves it is not inert.
test_path_scope_refuses_outside_tree_but_still_removes_intree() {
  local base; base=$(mktemp -d)
  _build_fixture "$base"
  local state_dir="$base/state"
  mkdir -p "$state_dir"

  # A second, otherwise-eligible worktree OUTSIDE the fixture repo's
  # .claude/worktrees/ — branched off wt-stale-branch (not main, which has
  # since advanced) so it carries the same staleness as wt-stale.
  (
    cd "$base/repo" || exit 1
    git worktree add --quiet -b outside-wt-branch "$base/outside-wt" wt-stale-branch
  )
  touch -d "2 hours ago" "$base/outside-wt" 2>/dev/null || true

  local out
  out=$(cd "$base/repo" && AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/sweep-stale-worktrees.sh --apply 2>&1)

  # A1: the out-of-tree worktree survives and is refused with the greppable marker.
  if [[ -d "$base/outside-wt" ]] \
     && echo "$out" | grep -q "sweep-self-exclusion-refused (path outside worktrees dir): outside-wt"; then
    pass "A1: out-of-tree worktree refused by path-scope guard, not removed"
  else
    fail "A1: out-of-tree worktree was removed, or the refusal marker is missing"
    echo "$out" | sed 's/^/    /'
  fi

  # A3 (anti-inertness): the eligible IN-TREE worktree (wt-stale — neither
  # self nor out-of-tree) is STILL removed, in the same run as the A1 refusal.
  if [[ ! -d "$base/repo/.claude/worktrees/wt-stale" ]] \
     && echo "$out" | grep -q "removed (or would-remove): 1" \
     && echo "$out" | grep -q "1 stale worktrees removed"; then
    pass "A3: eligible in-tree worktree is still removed — guard is not inert"
  else
    fail "A3: eligible in-tree worktree was NOT removed — guard may be refusing everything"
    echo "$out" | sed 's/^/    /'
  fi

  rm -rf "$base"
}

# ── Test 6 (D#1809 A2): self-exclusion — sweep invoked from inside its own ──
# in-tree worktree must refuse to remove that worktree.
test_self_exclusion_refuses_running_worktree() {
  local base; base=$(mktemp -d)
  _build_fixture "$base"
  local state_dir="$base/state"
  mkdir -p "$state_dir"

  # A second in-tree worktree, aged/stale/clean like wt-stale, that the
  # sweep will itself run from — otherwise eligible in every other respect.
  # Branched off wt-stale-branch (not main, which has since advanced) so it
  # carries the same staleness as wt-stale.
  (
    cd "$base/repo" || exit 1
    git worktree add --quiet -b wt-self-branch "$base/repo/.claude/worktrees/wt-self" wt-stale-branch
  )
  touch -d "2 hours ago" "$base/repo/.claude/worktrees/wt-self" 2>/dev/null || true

  local out
  out=$(cd "$base/repo/.claude/worktrees/wt-self" \
    && AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash "$base/repo/scripts/sweep-stale-worktrees.sh" --apply 2>&1)

  if [[ -d "$base/repo/.claude/worktrees/wt-self" ]] \
     && echo "$out" | grep -q "sweep-self-exclusion-refused (self): wt-self"; then
    pass "A2: sweep refuses to remove the worktree it is itself running from"
  else
    fail "A2: self worktree was removed, or the refusal marker is missing"
    echo "$out" | sed 's/^/    /'
  fi

  rm -rf "$base"
}

# ── Run ─────────────────────────────────────────────────────────────────────
echo "Running tests for scripts/sweep-stale-worktrees.sh..."
test_default_is_dry_run
test_apply_removes_and_audits
test_yes_alias_removes
test_registry_active_protects_via_worktree_id_key
test_path_scope_refuses_outside_tree_but_still_removes_intree
test_self_exclusion_refuses_running_worktree

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
exit 0
