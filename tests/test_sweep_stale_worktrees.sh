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

# ── Fixture additions (D#2041 AC-1..AC-5): unpushed-commit guard ───────────
# Adds FOUR detached worktrees to an already-built _build_fixture repo, all
# planted on wt-stale-branch's tip (the same >20-behind, aged-eligible
# commit _build_fixture already established for wt-stale itself) so each
# only needs its own distinguishing trait added:
#   wt-unpushed — one commit made INSIDE the worktree after checkout, on no
#                 remote ref (AC-1: must be skipped under the new bucket)
#   wt-fresh    — zero new commits, checkout point itself IS on a remote ref
#                 (AC-2: the inert-guard tripwire — must still be removed)
#   wt-squash   — checked out directly at a commit that is on NO remote ref,
#                 but zero commits added after that checkout (AC-3: the
#                 squash-merge/deleted-branch trap a naive
#                 `rev-list HEAD --not --remotes` would protect forever —
#                 must still be removed)
#   wt-dirty    — an uncommitted tracked-file edit, no new commit (AC-4:
#                 must land in the pre-existing dirty bucket, unaffected)
# All four run through ONE sweep invocation together (AC-5): that is what
# proves the buckets stay distinguishable rather than collapsing into one.
_add_ac_fixtures() {
  local repo="$1"

  # wt-unpushed (AC-1)
  (
    cd "$repo" || exit 1
    git worktree add --quiet --detach ".claude/worktrees/wt-unpushed" wt-stale-branch
  )
  echo "local-only work" > "$repo/.claude/worktrees/wt-unpushed/unpushed.txt"
  (
    cd "$repo/.claude/worktrees/wt-unpushed" || exit 1
    git add unpushed.txt
    git commit --quiet -m "local commit, never pushed"
  )
  touch -d "2 hours ago" "$repo/.claude/worktrees/wt-unpushed" 2>/dev/null || true

  # wt-fresh (AC-2)
  (
    cd "$repo" || exit 1
    git worktree add --quiet --detach ".claude/worktrees/wt-fresh" wt-stale-branch
  )
  touch -d "2 hours ago" "$repo/.claude/worktrees/wt-fresh" 2>/dev/null || true

  # wt-squash (AC-3): the orphan commit is built directly in the main repo
  # checkout, NOT inside a worktree, then the worktree is detached onto it —
  # so the worktree's own HEAD reflog has exactly one entry (the checkout),
  # matching "zero commits added after the worktree was created."
  local orphan_sha
  (
    cd "$repo" || exit 1
    git checkout --quiet -b orphan-branch wt-stale-branch
    echo "pre-squash work" > orphan.txt
    git add orphan.txt
    git commit --quiet -m "commit that lands on no remote ref"
  )
  orphan_sha=$(cd "$repo" && git rev-parse orphan-branch)
  (
    cd "$repo" || exit 1
    git checkout --quiet main
    git branch -D orphan-branch >/dev/null
    git worktree add --quiet --detach ".claude/worktrees/wt-squash" "$orphan_sha"
  )
  touch -d "2 hours ago" "$repo/.claude/worktrees/wt-squash" 2>/dev/null || true

  # wt-dirty (AC-4)
  (
    cd "$repo" || exit 1
    git worktree add --quiet --detach ".claude/worktrees/wt-dirty" wt-stale-branch
  )
  echo "uncommitted edit" >> "$repo/.claude/worktrees/wt-dirty/f.txt"
  touch -d "2 hours ago" "$repo/.claude/worktrees/wt-dirty" 2>/dev/null || true
}

# ── Test 7 (D#2041 AC-1..AC-5): unpushed-commit guard and its neighbors ────
test_unpushed_guard_and_related_buckets() {
  local base; base=$(mktemp -d)
  _build_fixture "$base"
  local repo="$base/repo"
  _add_ac_fixtures "$repo"

  local unpushed_sha
  unpushed_sha=$(cd "$repo/.claude/worktrees/wt-unpushed" && git rev-parse HEAD)

  local state_dir="$base/state"
  mkdir -p "$state_dir"

  local out
  out=$(cd "$repo" && AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/sweep-stale-worktrees.sh --apply 2>&1)

  # AC-1: wt-unpushed survives, its commit is still reachable, and it is
  # reported under the new bucket — not dirty, not removed.
  if [[ -d "$repo/.claude/worktrees/wt-unpushed" ]] \
     && [[ "$(cd "$repo/.claude/worktrees/wt-unpushed" && git rev-parse HEAD)" == "$unpushed_sha" ]] \
     && echo "$out" | grep -q "unpushed ids:.*wt-unpushed"; then
    pass "AC-1: worktree with a locally-added unpushed commit is skipped, commit survives"
  else
    fail "AC-1: unpushed-commit worktree was removed, lost its commit, or wasn't reported"
    echo "$out" | sed 's/^/    /'
  fi

  # AC-2: the inert-guard tripwire — a detached worktree with zero new
  # commits, checked out at a commit on a remote ref, must still be removed.
  if [[ ! -d "$repo/.claude/worktrees/wt-fresh" ]]; then
    pass "AC-2: detached worktree with no new commits is still removed (guard is not inert)"
  else
    fail "AC-2: detached worktree with no new commits was NOT removed"
    echo "$out" | sed 's/^/    /'
  fi

  # AC-3: the squash-merge/deleted-branch trap — checkout point on no
  # remote ref, but zero commits added inside the worktree — must still be
  # removed. A naive `rev-list HEAD --not --remotes` (no checkout-sha
  # exclusion) would wrongly keep this forever.
  if [[ ! -d "$repo/.claude/worktrees/wt-squash" ]]; then
    pass "AC-3: worktree checked out at an off-remote commit with no new work is still removed"
  else
    fail "AC-3: squash-merge-shaped worktree was NOT removed — naive unpushed-check regression"
    echo "$out" | sed 's/^/    /'
  fi

  # AC-4: ordinary dirty behaviour is untouched by the new guard.
  if [[ -d "$repo/.claude/worktrees/wt-dirty" ]] \
     && echo "$out" | grep -q "dirty ids:.*wt-dirty"; then
    pass "AC-4: worktree with an uncommitted edit still lands in the pre-existing dirty bucket"
  else
    fail "AC-4: dirty worktree was removed, or not reported under the dirty bucket"
    echo "$out" | sed 's/^/    /'
  fi

  # AC-5: the four states are distinguishable in the SAME run's summary —
  # not all folded into one bucket. wt-stale (from _build_fixture) is also
  # eligible and removed here, so removed=3 (wt-stale, wt-fresh, wt-squash).
  if echo "$out" | grep -q "removed (or would-remove): 3" \
     && echo "$out" | grep -q "skipped (dirty, tracked changes): 1" \
     && echo "$out" | grep -q "skipped (unpushed, locally-added commit on no remote ref): 1"; then
    pass "AC-5: removed/dirty/unpushed buckets each carry their own distinct count in one run"
  else
    fail "AC-5: buckets did not carry the expected distinct counts"
    echo "$out" | sed 's/^/    /'
  fi

  rm -rf "$base"
}

# ── Fixture: two worktrees whose HEAD reflog carries no readable checkout ──
# entry (D#2041 code-review fix). The unpushed-commit guard originally fell
# through to the removal-eligible path when the reflog was empty or
# unreadable — the exact silent-loss failure mode this guard exists to
# close. Both ordinary causes are reproduced here, since they arrive by
# different routes:
#   wt-expired — reflog existed, then expired (git's own
#                gc.reflogExpireUnreachable=30 days default)
#   wt-nolog   — reflog was never written at all (core.logAllRefUpdates=false)
_add_missing_reflog_fixtures() {
  local repo="$1"

  # wt-expired: commit normally (reflog gets an entry), then expire it away.
  (
    cd "$repo" || exit 1
    git worktree add --quiet --detach ".claude/worktrees/wt-expired" wt-stale-branch
  )
  echo "local-only work, expired reflog" > "$repo/.claude/worktrees/wt-expired/expired.txt"
  (
    cd "$repo/.claude/worktrees/wt-expired" || exit 1
    git add expired.txt
    git commit --quiet -m "local commit, reflog will be expired"
    # Scoped to this worktree's own HEAD only. `--all` instead of a bare
    # `HEAD` here would also expire refs/heads/* reflogs, which are SHARED
    # across every worktree in this repo (a detached worktree's HEAD reflog
    # is the only per-worktree one) — that collaterally wiped wt-stale's own
    # reflog (wt-stale-branch) the first time this fixture was written.
    git reflog expire --expire=now HEAD
  )
  touch -d "2 hours ago" "$repo/.claude/worktrees/wt-expired" 2>/dev/null || true

  # wt-nolog: flip logAllRefUpdates off BEFORE creating this worktree, so no
  # reflog entry is ever written for it — not even the initial checkout one.
  # This is a repo-wide setting shared by every worktree, so it is set here
  # (after wt-expired's own commit/expire above, which does not depend on
  # it) rather than in _build_fixture, which every other test also uses.
  (
    cd "$repo" || exit 1
    git config core.logAllRefUpdates false
    git worktree add --quiet --detach ".claude/worktrees/wt-nolog" wt-stale-branch
  )
  echo "local-only work, no reflog" > "$repo/.claude/worktrees/wt-nolog/nolog.txt"
  (
    cd "$repo/.claude/worktrees/wt-nolog" || exit 1
    git add nolog.txt
    git commit --quiet -m "local commit, never logged"
  )
  touch -d "2 hours ago" "$repo/.claude/worktrees/wt-nolog" 2>/dev/null || true
}

# ── Test 8 (D#2041 code-review fix): missing/expired reflog fails CLOSED ───
# Reproduces the exact defect the reviewer found end-to-end against the real
# script: an empty or unreadable HEAD reflog must route to the unpushed
# bucket (protect), never fall through to removal. Both worktrees below
# carry a real committed-but-unpushed commit with no readable reflog, by
# the two ordinary routes that produce one (see _add_missing_reflog_fixtures).
test_unpushed_guard_fails_closed_on_missing_reflog() {
  local base; base=$(mktemp -d)
  _build_fixture "$base"
  local repo="$base/repo"
  _add_missing_reflog_fixtures "$repo"

  # Sanity-check the fixtures actually produce an empty reflog — if this
  # failed, the assertions below would pass for the wrong reason (a broken
  # fixture, not a correct guard).
  local expired_reflog nolog_reflog
  expired_reflog=$(cd "$repo/.claude/worktrees/wt-expired" && git reflog show --format=%H HEAD 2>/dev/null | tail -1)
  nolog_reflog=$(cd "$repo/.claude/worktrees/wt-nolog" && git reflog show --format=%H HEAD 2>/dev/null | tail -1)
  if [[ -n "$expired_reflog" || -n "$nolog_reflog" ]]; then
    fail "fixture setup: expected both wt-expired and wt-nolog to have an EMPTY reflog before the sweep runs"
    rm -rf "$base"
    return
  fi

  local expired_sha nolog_sha
  expired_sha=$(cd "$repo/.claude/worktrees/wt-expired" && git rev-parse HEAD)
  nolog_sha=$(cd "$repo/.claude/worktrees/wt-nolog" && git rev-parse HEAD)

  local state_dir="$base/state"
  mkdir -p "$state_dir"

  local out
  out=$(cd "$repo" && AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/sweep-stale-worktrees.sh --apply 2>&1)

  if [[ -d "$repo/.claude/worktrees/wt-expired" ]] \
     && [[ "$(cd "$repo/.claude/worktrees/wt-expired" && git rev-parse HEAD)" == "$expired_sha" ]] \
     && echo "$out" | grep -q "unpushed ids:.*wt-expired"; then
    pass "expired-reflog worktree is protected, not removed"
  else
    fail "expired-reflog worktree was removed or lost its commit — fail-open regression"
    echo "$out" | sed 's/^/    /'
  fi

  if [[ -d "$repo/.claude/worktrees/wt-nolog" ]] \
     && [[ "$(cd "$repo/.claude/worktrees/wt-nolog" && git rev-parse HEAD)" == "$nolog_sha" ]] \
     && echo "$out" | grep -q "unpushed ids:.*wt-nolog"; then
    pass "never-logged (core.logAllRefUpdates=false) worktree is protected, not removed"
  else
    fail "never-logged worktree was removed or lost its commit — fail-open regression"
    echo "$out" | sed 's/^/    /'
  fi

  if echo "$out" | grep -q "skipped (unpushed, locally-added commit on no remote ref): 2"; then
    pass "both missing-reflog worktrees are counted in the unpushed bucket"
  else
    fail "unpushed bucket count did not reflect both missing-reflog worktrees"
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
test_unpushed_guard_and_related_buckets
test_unpushed_guard_fails_closed_on_missing_reflog

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
