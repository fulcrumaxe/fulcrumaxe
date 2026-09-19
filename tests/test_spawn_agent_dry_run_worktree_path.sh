#!/usr/bin/env bash
# tests/test_spawn_agent_dry_run_worktree_path.sh — verify the
# `--dry-run-env-dump --pr N --isolation worktree` lane in spawn-agent.sh
# exports a clean `worktree_path` (D#2547).
#
# The dry-run lane calls `pr_tree_provision` (scripts/lib/pr-tree.sh), which
# correctly writes its human-readable log line to stderr and the resolved
# path to stdout. The caller used to capture that call with `2>&1`, which
# merges the stderr log line into the captured value — on the success path
# `worktree_path` became "pr-tree: provisioned ...\n<path>" instead of just
# `<path>`. A consumer of that exported var (an acceptance check asserting
# the emitted path's HEAD) gets a corrupted string, not a directory.
#
# This suite stubs `gh` and `pr-tree.sh` so it never touches the network or
# a real git worktree — it exercises the real spawn-agent.sh file (copied,
# not reimplemented), same convention as
# tests/test_spawn_agent_pr_branch_validation.sh.
#
# AC1  success path: worktree_path is exported, contains no "pr-tree:"
#      substring, is a single line, and is a directory `[ -d ]` accepts.
# AC2  failure path: worktree_path is never exported, and the failure
#      reason (pr-tree's own log line) still reaches stderr via the
#      existing WARN — the fix must not delete that diagnostic.
# AC3  mutation check: restoring the removed `2>&1` on a copy of the fixed
#      script makes AC1's "no pr-tree: prefix" assertion go red again,
#      proving AC1 actually discriminates this bug rather than passing
#      unconditionally.
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and stub gh / pr-tree.sh — no real API calls,
# no network, no real git worktree.
#
# Usage:
#   bash tests/test_spawn_agent_dry_run_worktree_path.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_SCRIPT="$REPO_ROOT/scripts/spawn-agent.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

# ── Setup ─────────────────────────────────────────────────────────────────────

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

SCRIPTS_DIR="$TEST_DIR/scripts"
LIB_DIR="$SCRIPTS_DIR/lib"
mkdir -p "$LIB_DIR"

cp "$SPAWN_SCRIPT" "$SCRIPTS_DIR/spawn-agent.sh"
SPAWN_COPY="$SCRIPTS_DIR/spawn-agent.sh"

# pr-plane.sh and repo-resolve.sh are unmodified — they are not the file
# under test, and pr_plane_resolve's own PR_PLANE_RESOLVE_OVERRIDE_NAME/REPO
# escape hatch (documented in the real file) is what lets this suite skip
# plane resolution's own gh calls entirely.
cp "$REPO_ROOT/scripts/lib/pr-plane.sh" "$LIB_DIR/pr-plane.sh"
cp "$REPO_ROOT/scripts/lib/repo-resolve.sh" "$LIB_DIR/repo-resolve.sh"
# spawn-agent.sh sources these two unconditionally (with `|| true`) before
# arg parsing even starts. They are irrelevant to the dry-run lane this
# suite exercises, but copying them (unmodified) keeps stderr free of
# "No such file" noise that has nothing to do with what this suite checks.
cp "$REPO_ROOT/scripts/lib/gh-token.sh" "$LIB_DIR/gh-token.sh"
cp "$REPO_ROOT/scripts/lib/state-dir.sh" "$LIB_DIR/state-dir.sh"

# Stub pr-tree.sh: mirrors the REAL file's stdout/stderr split (log to
# stderr, path to stdout) without touching git or the network. This is the
# file the Spec says must NOT be modified — stubbing it here is how the test
# proves the caller's handling of that split, not a change to the real file.
cat > "$LIB_DIR/pr-tree.sh" <<'STUB'
#!/usr/bin/env bash
pr_tree_provision() {
  local pr_number="$1" head_sha="$2" dest="$3" plane="$4"
  if [[ "${TEST_PRT_FAIL:-0}" == "1" ]]; then
    echo "pr-tree: git worktree add failed: simulated failure for test" >&2
    return 1
  fi
  mkdir -p "$dest"
  echo "pr-tree: provisioned $dest at $head_sha (PR #$pr_number, plane=$plane, remote=fake, origin=fake)" >&2
  printf '%s\n' "$dest"
}
STUB
chmod +x "$LIB_DIR/pr-tree.sh"

# Stub gh — only the "resolve PR head sha/ref" call this lane makes needs a
# response; plane resolution itself is short-circuited by the override env
# vars below, so it never calls gh at all.
cat > "$TEST_DIR/gh" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "api" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == *"/pulls/"* ]]; then
      printf 'deadbeefsha\tfake-branch\n'
      exit 0
    fi
  done
fi
exit 0
STUB
chmod +x "$TEST_DIR/gh"

run_dry_run() {
  # REPO_ROOT is intentionally left to the copy's own computation
  # ($SCRIPT_DIR/.., i.e. $TEST_DIR) so _DRP_DEST lands under $TEST_DIR and
  # is cleaned up by the trap above — never the real repo.
  PATH="$TEST_DIR:$PATH" \
  AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe" \
  PR_PLANE_RESOLVE_OVERRIDE_NAME="code" \
  PR_PLANE_RESOLVE_OVERRIDE_REPO="fake/repo" \
  TEST_PRT_FAIL="${TEST_PRT_FAIL:-0}" \
    bash "$1" \
      --role executor \
      --discussion 999 \
      --pr 4242 \
      --isolation worktree \
      --dry-run-env-dump
}

# ── AC1: success path — worktree_path is clean ──────────────────────────────

echo ""
echo "AC1: success path exports a clean worktree_path"

OUT=$(run_dry_run "$SPAWN_COPY" 2>/tmp/ac1-stderr.$$)
RC=$?
rm -f /tmp/ac1-stderr.$$

if [[ $RC -ne 0 ]]; then
  fail "AC1 exit code" "expected 0, got $RC"
else
  pass "dry-run-env-dump exits 0 on the success path"
fi

WT_LINE=$(printf '%s\n' "$OUT" | grep -E '^worktree_path=' || true)
if [[ -z "$WT_LINE" ]]; then
  fail "AC1 worktree_path present" "no 'worktree_path=' line found in env dump"
else
  pass "worktree_path line found in env dump"
  WT_VALUE="${WT_LINE#worktree_path=}"
  if [[ "$WT_VALUE" == *"pr-tree:"* ]]; then
    fail "AC1 no pr-tree prefix" "worktree_path still contains 'pr-tree:' — got: $WT_VALUE"
  else
    pass "worktree_path contains no 'pr-tree:' prefix"
  fi
  if [[ -d "$WT_VALUE" ]]; then
    pass "worktree_path ($WT_VALUE) is a directory [ -d ] accepts"
  else
    fail "AC1 worktree_path is a directory" "[ -d \"$WT_VALUE\" ] failed for: $WT_VALUE"
  fi
fi

# A second line starting with the raw dest path (the old bug's second half
# of the merged value, split onto its own line by env) must never appear —
# that shape is exactly what corrupted a naive line-based env consumer.
STRAY_PATH_LINE=$(printf '%s\n' "$OUT" | grep -E '^/' || true)
if [[ -n "$STRAY_PATH_LINE" ]]; then
  fail "AC1 no stray path line" "found a bare path line outside worktree_path=...: $STRAY_PATH_LINE"
else
  pass "no stray bare-path line in env dump (value stayed on one line)"
fi

# ── AC2: failure path — diagnostics still surface, worktree_path unset ─────

echo ""
echo "AC2: failure path surfaces pr-tree's reason and never exports worktree_path"

ERR_FILE="$TEST_DIR/ac2-stderr.log"
OUT2=$(TEST_PRT_FAIL=1 run_dry_run "$SPAWN_COPY" 2>"$ERR_FILE")
RC2=$?

if [[ $RC2 -ne 0 ]]; then
  fail "AC2 exit code" "expected 0 (dry-run-env-dump always exits 0, failure is reported via WARN), got $RC2"
else
  pass "dry-run-env-dump exits 0 even when pr-tree provisioning fails"
fi

if printf '%s\n' "$OUT2" | grep -qE '^worktree_path='; then
  fail "AC2 worktree_path absent on failure" "worktree_path was exported despite provisioning failure"
else
  pass "worktree_path is not exported on provisioning failure"
fi

if grep -qF "git worktree add failed: simulated failure for test" "$ERR_FILE"; then
  pass "pr-tree's own failure reason reached stderr via the WARN line"
else
  fail "AC2 diagnostics preserved" "expected pr-tree's failure message in stderr, got: $(cat "$ERR_FILE")"
fi

# ── AC3: mutation check — reintroducing 2>&1 makes AC1 go red again ─────────

echo ""
echo "AC3: mutation check — restoring the removed 2>&1 reproduces the bug"

MUT_COPY="$SCRIPTS_DIR/spawn-agent-mutated.sh"
cp "$SPAWN_COPY" "$MUT_COPY"
sed -i 's/pr_tree_provision "\$PR_ARG" "\$_DRP_SHA" "\$_DRP_DEST" "\$PR_PLANE_NAME" 2>"\$_DRP_ERR"/pr_tree_provision "$PR_ARG" "$_DRP_SHA" "$_DRP_DEST" "$PR_PLANE_NAME" 2>\&1/' "$MUT_COPY"
chmod +x "$MUT_COPY"

if diff -q "$SPAWN_COPY" "$MUT_COPY" >/dev/null; then
  fail "AC3 mutation applied" "sed did not change the mutated copy — mutation pattern is stale"
else
  pass "mutation applied (2>&1 restored in a private copy)"

  MUT_OUT=$(run_dry_run "$MUT_COPY" 2>/dev/null)
  MUT_WT_LINE=$(printf '%s\n' "$MUT_OUT" | grep -E '^worktree_path=' || true)
  if [[ "$MUT_WT_LINE" == *"pr-tree:"* ]]; then
    pass "mutated copy reproduces the bug — worktree_path carries the 'pr-tree:' prefix again (confirms AC1 is a real, failable check)"
  else
    fail "AC3 mutation reproduces bug" "expected 'pr-tree:' prefix to reappear with 2>&1 restored — got: $MUT_WT_LINE"
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
exit 0
