#!/usr/bin/env bash
# tests/test_code_plane_pr.sh — hermetic tests for scripts/lib/code-plane-pr.sh
# and its two spawn-surface wirings (D#2442 PR-a).
#
# Every fixture is built with git plumbing (init / hash-object / mktree /
# commit-tree / update-ref) in a private mktemp -d — never checkout/switch/
# branch/reset/clean/worktree/restore, and never `gh` or a network push.
#
# Cases (names match the six disciplines in D#2442 Spec items 4-9, plus the
# prompt-reaches-the-executor check in item 10 and the worktree-registry
# check in item 14):
#   1. helper file exists and is executable   (item 1)
#   2. helper passes bash -n                  (item 2)
#   3. byte-identity divergence refuses        (item 4)
#   4. absent path accepted as new file        (item 5)
#   5. mode is read from ls-tree, not guessed  (item 6)
#   6. scratch path is private and per-call    (item 7)
#   7. no local ref moves, working tree intact (item 8)
#   8. no always-blocked verb in the helper    (item 9)
#   9. the route reaches an assembled executor prompt (item 10)
#  10. worktree registry records the code-plane branch alongside the
#      worktree's own branch (item 14)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HELPER="$REPO_ROOT/scripts/lib/code-plane-pr.sh"

PASS=0
FAIL=0
_pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

# A private scratch area for THIS test run's own fixture files (never a
# fixed path — mktemp -d, same discipline the helper itself enforces).
TEST_SCRATCH="$(mktemp -d)"
trap 'rm -rf "$TEST_SCRATCH"' EXIT

# ── Case 1/2: file exists, executable, syntactically valid ───────────────────
echo ""
echo "=== Case: helper exists, is executable, and parses ==="
if [[ -x "$HELPER" ]]; then
  _pass "scripts/lib/code-plane-pr.sh exists and is executable"
else
  _fail "scripts/lib/code-plane-pr.sh missing or not executable"
fi

if bash -n "$HELPER" 2>"$TEST_SCRATCH/bashn.err"; then
  _pass "bash -n scripts/lib/code-plane-pr.sh"
else
  _fail "bash -n scripts/lib/code-plane-pr.sh: $(cat "$TEST_SCRATCH/bashn.err")"
fi

# shellcheck source=scripts/lib/code-plane-pr.sh
source "$HELPER"

# ── Fixture builder: a bare, hermetic git repo with two divergent commits ────
# All construction is plumbing-only (no checkout/switch/branch/reset/clean/
# worktree/restore), per the same constraint the helper itself is under.
_fixture_repo() {
  local dir
  dir="$(mktemp -d)"
  git -C "$dir" init -q
  printf '%s\n' "$dir"
}

# ── Case: byte-identity divergence refuses (item 4) ──────────────────────────
echo ""
echo "=== Case: byte-identity divergence refuses ==="
FX4="$(_fixture_repo)"
BASE_BLOB_4="$(printf 'shared content, round one\n' | git -C "$FX4" hash-object -w --stdin)"
TARGET_BLOB_4="$(printf 'shared content, moved on the code plane\n' | git -C "$FX4" hash-object -w --stdin)"
TREE_BASE_4="$(printf '100644 blob %s\tshared.txt\n' "$BASE_BLOB_4" | git -C "$FX4" mktree)"
TREE_TARGET_4="$(printf '100644 blob %s\tshared.txt\n' "$TARGET_BLOB_4" | git -C "$FX4" mktree)"
COMMIT_BASE_4="$(git -C "$FX4" commit-tree "$TREE_BASE_4" -m base)"
COMMIT_TARGET_4="$(git -C "$FX4" commit-tree "$TREE_TARGET_4" -m target)"

LOCAL_EDIT_4="$TEST_SCRATCH/case4-local.txt"
printf 'an edit drafted against the stale base\n' > "$LOCAL_EDIT_4"

OUT4="$(cd "$FX4" && code_plane_pr build --base-ref "$COMMIT_BASE_4" --target-ref "$COMMIT_TARGET_4" \
  --branch test-branch --message "test divergence" "shared.txt=$LOCAL_EDIT_4" 2>"$TEST_SCRATCH/case4.err")"
RC4=$?

SHORT_BASE_4="${BASE_BLOB_4:0:7}"
SHORT_TARGET_4="${TARGET_BLOB_4:0:7}"

if [[ "$RC4" -eq 3 ]]; then
  _pass "build exits 3 (documented divergence code) on a diverged touched path"
else
  _fail "build: expected exit 3 on divergence, got $RC4 (stdout='$OUT4')"
fi
if [[ -z "$OUT4" ]]; then
  _pass "build prints no commit sha to stdout on divergence"
else
  _fail "build: expected empty stdout on divergence, got '$OUT4'"
fi
if grep -q "shared.txt" "$TEST_SCRATCH/case4.err"; then
  _pass "build's stderr names the diverged path"
else
  _fail "build's stderr does not name the diverged path: $(cat "$TEST_SCRATCH/case4.err")"
fi
if grep -q "$SHORT_BASE_4" "$TEST_SCRATCH/case4.err" && grep -q "$SHORT_TARGET_4" "$TEST_SCRATCH/case4.err"; then
  _pass "build's stderr names both short hashes"
else
  _fail "build's stderr missing one or both short hashes: $(cat "$TEST_SCRATCH/case4.err")"
fi

# ── Case: absent path accepted as new file, not a divergence (item 5) ────────
echo ""
echo "=== Case: absent path is accepted as new, not a divergence ==="
LOCAL_NEW_5="$TEST_SCRATCH/case5-new.txt"
printf 'brand new file, never seen on either plane\n' > "$LOCAL_NEW_5"

OUT5="$(cd "$FX4" && code_plane_pr build --base-ref "$COMMIT_BASE_4" --target-ref "$COMMIT_TARGET_4" \
  --branch test-branch --message "test new file" "brand-new.txt=$LOCAL_NEW_5" 2>"$TEST_SCRATCH/case5.err")"
RC5=$?

if [[ "$RC5" -eq 0 ]]; then
  _pass "build exits 0 for a path absent on the target ref"
else
  _fail "build: expected exit 0 for a new file, got $RC5: $(cat "$TEST_SCRATCH/case5.err")"
fi
if [[ "$OUT5" =~ ^[0-9a-f]{40}$ ]]; then
  _pass "build prints a commit sha for the new-file case"
else
  _fail "build: expected a commit sha on stdout, got '$OUT5'"
fi
NEW_LINE_5="$(git -C "$FX4" ls-tree "$OUT5" -- brand-new.txt 2>/dev/null)"
if [[ -n "$NEW_LINE_5" ]]; then
  _pass "the built commit actually contains the new path"
else
  _fail "the built commit does not contain brand-new.txt"
fi

# A new (absent-on-target) path has no target mode to read, so `build` falls
# back to the local source file's own executable bit rather than a blind
# 100644 default. Prove that fallback with a second new path, chmod +x'd.
LOCAL_NEW_EXE_5="$TEST_SCRATCH/case5-new-exe.sh"
printf '#!/bin/sh\necho new and executable\n' > "$LOCAL_NEW_EXE_5"
chmod +x "$LOCAL_NEW_EXE_5"

OUT5B="$(cd "$FX4" && code_plane_pr build --base-ref "$COMMIT_BASE_4" --target-ref "$COMMIT_TARGET_4" \
  --branch test-branch --message "test new executable file" "brand-new-exe.sh=$LOCAL_NEW_EXE_5" 2>"$TEST_SCRATCH/case5b.err")"
RC5B=$?
if [[ "$RC5B" -eq 0 ]]; then
  _pass "build exits 0 for a new executable path"
else
  _fail "build failed for a new executable path: $(cat "$TEST_SCRATCH/case5b.err")"
fi
MODE_LINE_5B="$(git -C "$FX4" ls-tree "$OUT5B" -- brand-new-exe.sh 2>/dev/null)"
if [[ "$MODE_LINE_5B" == 100755\ * ]]; then
  _pass "a new path whose local source is chmod +x is written 100755, not defaulted 100644"
else
  _fail "a new executable path was not written 100755: '$MODE_LINE_5B'"
fi

# ── Case: mode is read from ls-tree, never guessed (item 6) ──────────────────
echo ""
echo "=== Case: file mode is read from git ls-tree, not guessed ==="
FX6="$(_fixture_repo)"
EXE_BLOB_6="$(printf '#!/bin/sh\necho original\n' | git -C "$FX6" hash-object -w --stdin)"
TREE_TARGET_6="$(printf '100755 blob %s\trun.sh\n' "$EXE_BLOB_6" | git -C "$FX6" mktree)"
COMMIT_TARGET_6="$(git -C "$FX6" commit-tree "$TREE_TARGET_6" -m "executable target")"

LOCAL_EDIT_6="$TEST_SCRATCH/case6-run.sh"
printf '#!/bin/sh\necho edited\n' > "$LOCAL_EDIT_6"

OUT6="$(cd "$FX6" && code_plane_pr build --target-ref "$COMMIT_TARGET_6" \
  --branch test-branch --message "edit an executable" "run.sh=$LOCAL_EDIT_6" 2>"$TEST_SCRATCH/case6.err")"
RC6=$?

if [[ "$RC6" -eq 0 ]]; then
  _pass "build succeeds when editing an existing 100755 path"
else
  _fail "build failed editing an existing 100755 path: $(cat "$TEST_SCRATCH/case6.err")"
fi
MODE_LINE_6="$(git -C "$FX6" ls-tree "$OUT6" -- run.sh 2>/dev/null)"
if [[ "$MODE_LINE_6" == 100755\ * ]]; then
  _pass "the built commit records mode 100755, read from the target ref (not guessed 100644)"
else
  _fail "the built commit does not preserve mode 100755: '$MODE_LINE_6'"
fi

# ── Case: self-verify scope refuses when the built commit doesn't actually
#          touch every requested path (exit 4) ───────────────────────────────
echo ""
echo "=== Case: scope self-check refuses when a requested path produces no diff ==="
FX7B="$(_fixture_repo)"
UNCHANGED_CONTENT_7B="content that will not actually change on disk\n"
UNCHANGED_BLOB_7B="$(printf "$UNCHANGED_CONTENT_7B" | git -C "$FX7B" hash-object -w --stdin)"
TREE_7B="$(printf '100644 blob %s\tunchanged.txt\n' "$UNCHANGED_BLOB_7B" | git -C "$FX7B" mktree)"
COMMIT_7B="$(git -C "$FX7B" commit-tree "$TREE_7B" -m "case 7b target")"

# The local file passed to `build` is byte-identical to what's already on the
# target ref for this path, so the built tree is identical to the target tree
# and the path drops out of `git diff --name-only` entirely — exactly the
# "requested but produced no diff" case the scope check (code-plane-pr.sh
# ~222-234) exists to catch.
LOCAL_UNCHANGED_7B="$TEST_SCRATCH/case7b-unchanged.txt"
printf "$UNCHANGED_CONTENT_7B" > "$LOCAL_UNCHANGED_7B"

OUT7B="$(cd "$FX7B" && code_plane_pr build --target-ref "$COMMIT_7B" \
  --branch test-branch --message "no-op write" "unchanged.txt=$LOCAL_UNCHANGED_7B" 2>"$TEST_SCRATCH/case7b.err")"
RC7B=$?

if [[ "$RC7B" -eq 4 ]]; then
  _pass "build exits 4 (documented scope-check code) when a requested path produces no diff"
else
  _fail "build: expected exit 4 on an empty-diff requested path, got $RC7B (stdout='$OUT7B')"
fi
if [[ -z "$OUT7B" ]]; then
  _pass "build prints no commit sha when the scope check fails"
else
  _fail "build: expected empty stdout on scope-check failure, got '$OUT7B'"
fi
if grep -qi "scope check" "$TEST_SCRATCH/case7b.err"; then
  _pass "build's stderr names the scope check as the reason for the refusal"
else
  _fail "build's stderr does not mention the scope check: $(cat "$TEST_SCRATCH/case7b.err")"
fi
rm -rf "$FX7B"

# ── Case: scratch path is private and per-invocation (item 7) ────────────────
echo ""
echo "=== Case: scratch/extraction path is private and per-invocation ==="
if grep -nE '(^|[^A-Za-z0-9_])/tmp/[A-Za-z0-9._-]+' "$HELPER" >/dev/null; then
  _fail "helper source contains a fixed /tmp/ literal"
else
  _pass "helper source contains no fixed /tmp/ literal (grep -nE confirms none)"
fi

DIR_A="$(cd "$FX6" && code_plane_pr extract --ref "$COMMIT_TARGET_6")"
DIR_B="$(cd "$FX6" && code_plane_pr extract --ref "$COMMIT_TARGET_6")"
if [[ -n "$DIR_A" && -n "$DIR_B" && "$DIR_A" != "$DIR_B" ]]; then
  _pass "two invocations of extract produce two distinct scratch directories"
else
  _fail "extract did not produce distinct scratch directories: A='$DIR_A' B='$DIR_B'"
fi
if [[ -f "$DIR_A/run.sh" && -f "$DIR_B/run.sh" ]]; then
  _pass "each extraction independently contains the target ref's tree"
else
  _fail "extraction did not populate the expected file in DIR_A or DIR_B"
fi
rm -rf "$DIR_A" "$DIR_B"

# ── Case: no local ref moves, working tree untouched (item 8) ────────────────
echo ""
echo "=== Case: no local ref moves and the working tree is untouched ==="
FX8="$(_fixture_repo)"
BLOB_8="$(printf 'content for the untouched-ref case\n' | git -C "$FX8" hash-object -w --stdin)"
TREE_8="$(printf '100644 blob %s\tfile8.txt\n' "$BLOB_8" | git -C "$FX8" mktree)"
COMMIT_8="$(git -C "$FX8" commit-tree "$TREE_8" -m "case 8 target")"

LOCAL_EDIT_8="$TEST_SCRATCH/case8-edit.txt"
printf 'edited content for case 8\n' > "$LOCAL_EDIT_8"

HEAD_BEFORE="$(git -C "$FX8" rev-parse HEAD 2>&1)"
HEAD_RC_BEFORE=$?
STATUS_BEFORE="$(git -C "$FX8" status --porcelain 2>&1)"

(cd "$FX8" && code_plane_pr build --target-ref "$COMMIT_8" --branch test-branch --message "case 8" \
  "file8.txt=$LOCAL_EDIT_8" >/dev/null 2>&1) || true

HEAD_AFTER="$(git -C "$FX8" rev-parse HEAD 2>&1)"
HEAD_RC_AFTER=$?
STATUS_AFTER="$(git -C "$FX8" status --porcelain 2>&1)"

if [[ "$HEAD_BEFORE" == "$HEAD_AFTER" && "$HEAD_RC_BEFORE" -eq "$HEAD_RC_AFTER" ]]; then
  _pass "git rev-parse HEAD is identical before and after a build run"
else
  _fail "HEAD changed: before='$HEAD_BEFORE'(rc=$HEAD_RC_BEFORE) after='$HEAD_AFTER'(rc=$HEAD_RC_AFTER)"
fi
if [[ "$STATUS_BEFORE" == "$STATUS_AFTER" ]]; then
  _pass "git status --porcelain is identical before and after a build run"
else
  _fail "working tree status changed: before='$STATUS_BEFORE' after='$STATUS_AFTER'"
fi

# ── Case: no always-blocked git verb appears in the helper (item 9) ──────────
echo ""
echo "=== Case: helper contains none of the seven always-blocked verbs ==="
if grep -nE 'git +(checkout|switch|branch|reset|clean|worktree|restore)\b' "$HELPER" >/dev/null; then
  _fail "helper source contains an always-blocked git verb"
else
  _pass "helper source contains none of the seven always-blocked git verbs"
fi

# ── Case: the route reaches an assembled executor prompt (item 10) ───────────
echo ""
echo "=== Case: the route reaches an assembled executor prompt ==="
RENDER_OUT="$TEST_SCRATCH/render.out"
RENDER_ERR="$TEST_SCRATCH/render.err"
(
  cd "$REPO_ROOT" && \
  SPAWN_PROMPT_JSON='{"role":"executor","discussion":1,"task_prompt":"X"}' \
    PYTHONPATH="$REPO_ROOT" python3 -m backend.prompt_builder render \
    > "$RENDER_OUT" 2> "$RENDER_ERR"
)
RENDER_RC=$?

if [[ "$RENDER_RC" -eq 0 ]]; then
  _pass "backend.prompt_builder render exits 0 for the executor role"
else
  _fail "backend.prompt_builder render failed (exit $RENDER_RC): $(cat "$RENDER_ERR")"
fi
if grep -q 'scripts/lib/code-plane-pr.sh' "$RENDER_OUT"; then
  _pass "the rendered executor prompt names scripts/lib/code-plane-pr.sh"
else
  _fail "the rendered executor prompt does not mention scripts/lib/code-plane-pr.sh"
fi
if grep -q '_resolve_code_repo' "$RENDER_OUT"; then
  _pass "the rendered executor prompt names _resolve_code_repo"
else
  _fail "the rendered executor prompt does not mention _resolve_code_repo"
fi
if grep -q 'gh pr create' "$REPO_ROOT/backend/spawn_templates/executor.tmpl"; then
  _pass "executor.tmpl's step 5 names an actual gh pr create invocation"
else
  _fail "executor.tmpl does not contain a gh pr create invocation"
fi
if grep -q 'code-plane-pr.sh' "$REPO_ROOT/.claude/agents/executor.md"; then
  _pass ".claude/agents/executor.md points at the helper"
else
  _fail ".claude/agents/executor.md does not mention code-plane-pr.sh"
fi
if grep -q 'commit-tree' "$REPO_ROOT/backend/spawn_templates/executor.tmpl"; then
  _fail "executor.tmpl restates the commit-tree recipe instead of pointing at the helper"
else
  _pass "executor.tmpl does not restate commit-tree — the discipline lives in the script"
fi
if grep -q 'commit-tree' "$REPO_ROOT/.claude/agents/executor.md"; then
  _fail ".claude/agents/executor.md restates the commit-tree recipe instead of pointing at the helper"
else
  _pass ".claude/agents/executor.md does not restate commit-tree — the discipline lives in the script"
fi

# ── Case: worktree registry records the code-plane branch (item 14) ──────────
echo ""
echo "=== Case: worktree registry records the code-plane branch alongside the worktree's own branch ==="
REG_TEST_DIR="$(mktemp -d)"
mkdir -p "$REG_TEST_DIR/.autonomous-team" "$REG_TEST_DIR/.claude/worktrees" "$REG_TEST_DIR/archive/orphan-diffs"

(
  export _WTR_REPO_ROOT="$REG_TEST_DIR"
  # shellcheck source=scripts/lib/worktree-registry.sh
  source "$REPO_ROOT/scripts/lib/worktree-registry.sh"

  worktree_registry register \
    --id "agent-cpptest01" \
    --role executor \
    --path ".claude/worktrees/agent-cpptest01" \
    --pid "$$" \
    --discussion 2442 \
    --branch "worktree-agent-cpptest01" \
    --base main >/dev/null

  worktree_registry set-pr "agent-cpptest01" 999 "cpp-helper-fix-branch" >/dev/null
) > "$TEST_SCRATCH/registry.out" 2>&1

ENTRY_JSON="$(python3 -c "
import json
data = json.load(open('$REG_TEST_DIR/.autonomous-team/worktrees.json'))
for e in data:
    if e.get('worktree_id') == 'agent-cpptest01':
        print(json.dumps(e))
        break
" 2>"$TEST_SCRATCH/registry_read.err")"

WT_BRANCH="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('branch',''))" "$ENTRY_JSON" 2>/dev/null)"
CP_BRANCH="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('code_plane_branch',''))" "$ENTRY_JSON" 2>/dev/null)"
PR_NUM="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('pr',''))" "$ENTRY_JSON" 2>/dev/null)"

if [[ -n "$ENTRY_JSON" ]]; then
  _pass "registry entry created for the fixture worktree"
else
  _fail "no registry entry found: $(cat "$TEST_SCRATCH/registry.out") $(cat "$TEST_SCRATCH/registry_read.err")"
fi
if [[ "$PR_NUM" == "999" ]]; then
  _pass "set-pr recorded the PR number"
else
  _fail "set-pr did not record pr=999, got '$PR_NUM'"
fi
if [[ "$WT_BRANCH" == "worktree-agent-cpptest01" && "$CP_BRANCH" == "cpp-helper-fix-branch" && "$WT_BRANCH" != "$CP_BRANCH" ]]; then
  _pass "registry records the worktree's own branch and the code-plane branch as distinct values"
else
  _fail "registry did not keep the two branches distinct: worktree_branch='$WT_BRANCH' code_plane_branch='$CP_BRANCH'"
fi

rm -rf "$REG_TEST_DIR"

# ── Summary ───────────────────────────────────────────────────────────────────
rm -rf "$FX4" "$FX6" "$FX8"

echo ""
echo "=== Results ==="
echo "PASS: $PASS  FAIL: $FAIL"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
