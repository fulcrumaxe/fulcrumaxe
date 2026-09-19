#!/usr/bin/env bash
# tests/test_docs_writer_push_plane.sh — write-side companion to
# tests/test_spawn_template_repo_plane.sh (D#1940's read-side fix).
#
# D#2481: backend/spawn_templates/docs-writer.tmpl and .claude/agents/docs-writer.md
# pushed the docs-writer's wiki commit with `git push origin ...`. `$DEST` is a
# worktree that shares the parent checkout's remotes, and "origin" always
# names the Discussion plane by this project's convention
# (scripts/lib/pr-tree.sh) — so a commit built on code-plane content for a
# code-plane PR was landing on the wrong remote. Not an exposure (the
# Discussion plane is private) but silent loss: the docs commit never
# reaches the PR it was written for.
#
# This test pulls the ACTUAL composed commit+push statement out of both
# files (never reimplements it) and executes it for real against two local
# bare repos standing in for the two planes, then asserts on WHICH ONE the
# push actually landed on. That is the discipline the Spec calls out: the
# adjacent finding from the read-side fix's review was that swapping the
# plane in three templates left all 204 pre-existing tests green, so an
# assertion that does not discriminate on the plane is not acceptance here.
# Asserting on a helper function's return value in isolation would have the
# same blind spot -- it would not notice if the file still said "origin".
#
# Hermetic: never touches AUTONOMOUS_TEAM_STATE_DIR, sets AUTONOMOUS_TEAM_REPO
# to a fixture slug, and works whether or not the checkout has a
# .autonomous-team/config.json — the code plane never ships one (see
# scripts/lib/repo-resolve.sh's own docstring), so this must not depend on
# one being present.
#
# Usage: bash tests/test_docs_writer_push_plane.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPL="$REPO_ROOT/backend/spawn_templates/docs-writer.tmpl"
AGENT_MD="$REPO_ROOT/.claude/agents/docs-writer.md"

PASS=0
FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; shift; [ $# -gt 0 ] && echo "        $*"; FAIL=$((FAIL + 1)); }

for f in "$TMPL" "$AGENT_MD"; do
  if [ ! -f "$f" ]; then
    echo "FATAL: missing file: $f"
    exit 1
  fi
done

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# A fixture slug + two local bare repos standing in for the two planes.
# CODE_BARE's own path deliberately ends in the fixture slug so
# _resolve_code_plane_remote's URL-suffix match finds it with no override —
# the real matching logic runs, not a mock (same convention as
# tests/test_pr_tree_provisioning.sh's collision fixture).
FIXTURE_SLUG="fixture-owner/fixture-code-repo"
DISCUSSION_BARE="$WORK/remotes/fixture-owner/fixture-discussion-repo.git"
CODE_BARE="$WORK/remotes/$FIXTURE_SLUG.git"
mkdir -p "$(dirname "$DISCUSSION_BARE")"
git init --quiet --bare "$DISCUSSION_BARE"
git init --quiet --bare "$CODE_BARE"

# extract_push_block <file> — prints the 3 physical lines making up the
# commit+push subshell, starting at the line that resolves CODE_REMOTE.
# Fixed-string anchor (grep -F), so this fails loudly (empty output) rather
# than silently matching the wrong thing if the surrounding prose changes.
extract_push_block() {
  local file="$1" anchor start
  anchor='CODE_REMOTE="$(source scripts/lib/repo-resolve.sh && _resolve_code_plane_remote "$DEST")"'
  start="$(grep -Fn "$anchor" "$file" | head -1 | cut -d: -f1)"
  [ -n "$start" ] || return 1
  sed -n "${start},$((start + 2))p" "$file"
}

# run_case <label> <file>
run_case() {
  local label="$1" file="$2"
  local block
  block="$(extract_push_block "$file")"
  if [ -z "$block" ]; then
    bad "$label: locate the commit+push block" "extraction anchor not found in $(basename "$file")"
    return
  fi

  # Mechanical placeholder substitution only — never touch the remote/push
  # logic itself. {{pr_branch}} (tmpl) and {pr_branch} (agent .md) both
  # become a concrete branch name; the "<...>" file-list placeholder and the
  # free-text commit-message placeholder become concrete values.
  block="$(printf '%s\n' "$block" \
    | sed -E 's/\{\{?pr_branch\}\}?/push-plane-test-branch/g' \
    | sed -E 's#git add wiki/<[^>]*>#git add wiki/testfile.md#' \
    | sed -E 's/-m "update docs[^"]*"/-m "test docs update"/')"

  local dest="$WORK/dest-$label"
  git init --quiet "$dest"
  (
    cd "$dest" || exit 1
    git config user.email "test@example.invalid"
    git config user.name "docs-writer test"
    mkdir -p wiki
    echo "stale content" > wiki/testfile.md
    git add -A && git commit --quiet -m "seed"
    git remote add origin "$DISCUSSION_BARE"
    git remote add code-plane-fixture "$CODE_BARE"
    echo "fixed content for $label" > wiki/testfile.md
  ) || { bad "$label: seed the fixture \$DEST" "setup failed"; return; }

  local out rc
  out="$(AUTONOMOUS_TEAM_REPO="$FIXTURE_SLUG" DEST="$dest" \
    bash -c "cd '$REPO_ROOT' && $block" 2>&1)"
  rc=$?

  if [ "$rc" -ne 0 ]; then
    bad "$label: composed push command exits 0" "exit=$rc output: $out"
    return
  fi
  ok "$label: composed push command exits 0"

  local code_head disc_head
  code_head="$(git -C "$CODE_BARE" rev-parse --verify --quiet refs/heads/push-plane-test-branch 2>/dev/null || true)"
  disc_head="$(git -C "$DISCUSSION_BARE" rev-parse --verify --quiet refs/heads/push-plane-test-branch 2>/dev/null || true)"

  if [ -n "$code_head" ]; then
    ok "$label: commit landed on the code-plane fixture remote"
  else
    bad "$label: commit landed on the code-plane fixture remote" \
      "refs/heads/push-plane-test-branch not found on the code-plane bare repo"
  fi

  if [ -z "$disc_head" ]; then
    ok "$label: nothing landed on the Discussion-plane fixture remote"
  else
    bad "$label: nothing landed on the Discussion-plane fixture remote" \
      "refs/heads/push-plane-test-branch found on the Discussion-plane bare repo -- pushed to origin"
  fi

  # Clear the branch so the two run_case invocations (tmpl, .md) can't mask
  # each other's failure to push by reusing a ref the other one created.
  git -C "$CODE_BARE" update-ref -d refs/heads/push-plane-test-branch 2>/dev/null || true
  git -C "$DISCUSSION_BARE" update-ref -d refs/heads/push-plane-test-branch 2>/dev/null || true
}

echo "=== backend/spawn_templates/docs-writer.tmpl ==="
run_case "docs-writer.tmpl" "$TMPL"

echo "=== .claude/agents/docs-writer.md ==="
run_case "docs-writer.md" "$AGENT_MD"

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
