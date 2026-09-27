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

OUT6="$(cd "$FX6" && code_plane_pr build --target-ref "$COMMIT_TARGET_6" --base-ref "$COMMIT_TARGET_6" \
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

OUT7B="$(cd "$FX7B" && code_plane_pr build --target-ref "$COMMIT_7B" --base-ref "$COMMIT_7B" \
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

(cd "$FX8" && code_plane_pr build --target-ref "$COMMIT_8" --base-ref "$COMMIT_8" --branch test-branch --message "case 8" \
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

# ── D#2498 Case: build refuses (exit 2) when --base-ref is omitted (item 1) ──
# Pre-fix, this silently defaulted base_ref to target_ref and built a commit
# (exit 0) — the two independent resolutions of the moving ref were never
# compared. Post-fix it's a usage error, named and actionable on stderr.
echo ""
echo "=== D#2498 Case: build without --base-ref exits 2, naming the flag (item 1) ==="
FX_D2498_1="$(_fixture_repo)"
BLOB_D2498_1="$(printf 'content for the required-base-ref case\n' | git -C "$FX_D2498_1" hash-object -w --stdin)"
TREE_D2498_1="$(printf '100644 blob %s\tfile.txt\n' "$BLOB_D2498_1" | git -C "$FX_D2498_1" mktree)"
COMMIT_D2498_1="$(git -C "$FX_D2498_1" commit-tree "$TREE_D2498_1" -m "d2498 item1 target")"
LOCAL_D2498_1="$TEST_SCRATCH/d2498-1-edit.txt"
printf 'edited\n' > "$LOCAL_D2498_1"

OUT_D2498_1="$(cd "$FX_D2498_1" && code_plane_pr build --target-ref "$COMMIT_D2498_1" \
  --branch test-branch --message "no base-ref" "file.txt=$LOCAL_D2498_1" 2>"$TEST_SCRATCH/d2498-1.err")"
RC_D2498_1=$?
if [[ "$RC_D2498_1" -eq 2 ]]; then
  _pass "D#2498: build without --base-ref exits 2"
else
  _fail "D#2498: expected exit 2 without --base-ref, got $RC_D2498_1 (stdout='$OUT_D2498_1')"
fi
if [[ -z "$OUT_D2498_1" ]]; then
  _pass "D#2498: build prints no commit sha without --base-ref"
else
  _fail "D#2498: expected empty stdout without --base-ref, got '$OUT_D2498_1'"
fi
if grep -q -- '--base-ref' "$TEST_SCRATCH/d2498-1.err"; then
  _pass "D#2498: build's stderr names --base-ref when omitted"
else
  _fail "D#2498: build's stderr does not name --base-ref: $(cat "$TEST_SCRATCH/d2498-1.err")"
fi
rm -rf "$FX_D2498_1"

# ── D#2498 Case: extract emits the resolved sha on stderr (item 2) ───────────
# stdout must stay byte-for-byte the directory path alone — the resolved
# identity goes out on stderr, never blended into stdout's contract and
# never written into the extracted tree itself.
echo ""
echo "=== D#2498 Case: extract emits the resolved sha on stderr (item 2) ==="
FX_D2498_2="$(_fixture_repo)"
BLOB_D2498_2="$(printf 'extract case content\n' | git -C "$FX_D2498_2" hash-object -w --stdin)"
TREE_D2498_2="$(printf '100644 blob %s\tfile.txt\n' "$BLOB_D2498_2" | git -C "$FX_D2498_2" mktree)"
COMMIT_D2498_2="$(git -C "$FX_D2498_2" commit-tree "$TREE_D2498_2" -m "d2498 item2 target")"

RAW_STDOUT_D2498_2="$TEST_SCRATCH/d2498-2.out"
(cd "$FX_D2498_2" && code_plane_pr extract --ref "$COMMIT_D2498_2" >"$RAW_STDOUT_D2498_2" 2>"$TEST_SCRATCH/d2498-2.err")
RC_D2498_2=$?
DIR_D2498_2="$(cat "$RAW_STDOUT_D2498_2")"

if [[ "$RC_D2498_2" -eq 0 && -n "$DIR_D2498_2" && -d "$DIR_D2498_2" ]]; then
  _pass "D#2498: extract succeeds and prints a directory"
else
  _fail "D#2498: extract failed: rc=$RC_D2498_2 dir='$DIR_D2498_2'"
fi
LINE_COUNT_D2498_2="$(wc -l < "$RAW_STDOUT_D2498_2")"
if [[ "$LINE_COUNT_D2498_2" -eq 1 ]]; then
  _pass "D#2498: extract's stdout is exactly one line — unchanged from current behaviour"
else
  _fail "D#2498: extract's stdout is not exactly one line ($LINE_COUNT_D2498_2 lines): $(cat "$RAW_STDOUT_D2498_2")"
fi
RESOLVED_SHA_D2498_2="$(git -C "$FX_D2498_2" rev-parse "$COMMIT_D2498_2^{commit}")"
if [[ "$RESOLVED_SHA_D2498_2" =~ ^[0-9a-f]{40}$ ]] && grep -qE "\b$RESOLVED_SHA_D2498_2\b" "$TEST_SCRATCH/d2498-2.err"; then
  _pass "D#2498: extract's stderr contains the resolved 40-char sha, equal to git rev-parse"
else
  _fail "D#2498: extract's stderr does not contain the resolved sha $RESOLVED_SHA_D2498_2: $(cat "$TEST_SCRATCH/d2498-2.err")"
fi
rm -rf "$DIR_D2498_2" "$FX_D2498_2"

# ── D#2498 Case: the moving-ref sequence, end to end (item 3, binding) ───────
# extract at commit A; the ref advances to commit B, moving the exact path
# the caller is about to write, out from under them. Two sub-cases share this
# fixture:
#   3a. the historical failure mode — omitting --base-ref in this exact
#       scenario. Pre-fix this silently defaults base_ref=target_ref (B),
#       the divergence check never runs, and the caller's edit lands on top
#       of B's change as if nothing happened: exit 0, a commit is built, and
#       it carries neither A's nor B's content faithfully at that path —
#       exactly the "hash values matching neither old nor current content"
#       damage PR #103 shipped. Post-fix it's the required-flag usage error.
#   3b. the literal moving-ref call — the caller does the right thing and
#       passes the correct --base-ref (A, captured from extract's own
#       stderr) against the now-moved target (B). This must be caught.
echo ""
echo "=== D#2498 Case: moving-ref sequence end to end (item 3) ==="
FX_D2498_3="$(_fixture_repo)"
BLOB_D2498_3A="$(printf 'shared content at commit A\n' | git -C "$FX_D2498_3" hash-object -w --stdin)"
TREE_D2498_3A="$(printf '100644 blob %s\tshared.txt\n' "$BLOB_D2498_3A" | git -C "$FX_D2498_3" mktree)"
COMMIT_D2498_3A="$(git -C "$FX_D2498_3" commit-tree "$TREE_D2498_3A" -m "d2498 item3 commit A")"

RAW_STDOUT_D2498_3="$TEST_SCRATCH/d2498-3-extract.out"
(cd "$FX_D2498_3" && code_plane_pr extract --ref "$COMMIT_D2498_3A" >"$RAW_STDOUT_D2498_3" 2>"$TEST_SCRATCH/d2498-3-extract.err")
DIR_D2498_3A="$(cat "$RAW_STDOUT_D2498_3")"
BASE_SHA_D2498_3="$(grep -oE '[0-9a-f]{40}' "$TEST_SCRATCH/d2498-3-extract.err" | tail -1)"

# the ref advances to commit B while the caller is still editing — shared.txt
# moves out from under them, independently of anything the caller does.
BLOB_D2498_3B="$(printf 'shared content at commit B — moved on the code plane\n' | git -C "$FX_D2498_3" hash-object -w --stdin)"
TREE_D2498_3B="$(printf '100644 blob %s\tshared.txt\n' "$BLOB_D2498_3B" | git -C "$FX_D2498_3" mktree)"
COMMIT_D2498_3B="$(git -C "$FX_D2498_3" commit-tree "$TREE_D2498_3B" -p "$COMMIT_D2498_3A" -m "d2498 item3 commit B")"

LOCAL_EDIT_D2498_3="$TEST_SCRATCH/d2498-3-local-edit.txt"
printf "the caller's own edit, drafted against commit A, unaware of B\n" > "$LOCAL_EDIT_D2498_3"

# 3a
OUT_D2498_3A="$(cd "$FX_D2498_3" && code_plane_pr build --target-ref "$COMMIT_D2498_3B" \
  --branch test-branch --message "moving ref, omitted base-ref" \
  "shared.txt=$LOCAL_EDIT_D2498_3" 2>"$TEST_SCRATCH/d2498-3a.err")"
RC_D2498_3A=$?
if [[ "$RC_D2498_3A" -eq 2 ]]; then
  _pass "D#2498: build refuses (exit 2) a moving-ref call that omits --base-ref"
else
  _fail "D#2498: expected exit 2 omitting --base-ref in the moving-ref case, got $RC_D2498_3A (stdout='$OUT_D2498_3A')"
fi

# 3b — the literal item-3 call
if [[ "$BASE_SHA_D2498_3" =~ ^[0-9a-f]{40}$ ]]; then
  _pass "D#2498: captured a 40-char base sha from extract's stderr to feed into build"
else
  _fail "D#2498: could not capture a base sha from extract's stderr: $(cat "$TEST_SCRATCH/d2498-3-extract.err")"
fi
OUT_D2498_3B="$(cd "$FX_D2498_3" && code_plane_pr build --target-ref "$COMMIT_D2498_3B" --base-ref "$BASE_SHA_D2498_3" \
  --branch test-branch --message "moving ref, explicit base-ref" \
  "shared.txt=$LOCAL_EDIT_D2498_3" 2>"$TEST_SCRATCH/d2498-3b.err")"
RC_D2498_3B=$?
if [[ "$RC_D2498_3B" -eq 3 ]]; then
  _pass "D#2498: build refuses (exit 3) when the target moved past the caller's base-ref"
else
  _fail "D#2498: expected exit 3 on the moving-ref divergence, got $RC_D2498_3B (stdout='$OUT_D2498_3B')"
fi
if [[ -z "$OUT_D2498_3B" ]]; then
  _pass "D#2498: build prints no commit sha on the moving-ref divergence"
else
  _fail "D#2498: expected empty stdout on moving-ref divergence, got '$OUT_D2498_3B'"
fi
if grep -q "shared.txt" "$TEST_SCRATCH/d2498-3b.err"; then
  _pass "D#2498: build's stderr names the diverged path in the moving-ref case"
else
  _fail "D#2498: build's stderr does not name the diverged path: $(cat "$TEST_SCRATCH/d2498-3b.err")"
fi
rm -rf "$DIR_D2498_3A" "$FX_D2498_3"

# ── D#2498 Case: regressions (item 9) ─────────────────────────────────────────
# An invocation that passes --base-ref equal to --target-ref — the genuine
# no-gap case — must stay able to say so explicitly and succeed.
echo ""
echo "=== D#2498 Case: --base-ref equal to --target-ref still succeeds (item 9) ==="
FX_D2498_9="$(_fixture_repo)"
BLOB_D2498_9="$(printf 'no-gap case content\n' | git -C "$FX_D2498_9" hash-object -w --stdin)"
TREE_D2498_9="$(printf '100644 blob %s\tfile.txt\n' "$BLOB_D2498_9" | git -C "$FX_D2498_9" mktree)"
COMMIT_D2498_9="$(git -C "$FX_D2498_9" commit-tree "$TREE_D2498_9" -m "d2498 item9 target")"
LOCAL_D2498_9="$TEST_SCRATCH/d2498-9-edit.txt"
printf 'edited with no gap\n' > "$LOCAL_D2498_9"

OUT_D2498_9="$(cd "$FX_D2498_9" && code_plane_pr build --target-ref "$COMMIT_D2498_9" --base-ref "$COMMIT_D2498_9" \
  --branch test-branch --message "no-gap case" "file.txt=$LOCAL_D2498_9" 2>"$TEST_SCRATCH/d2498-9.err")"
RC_D2498_9=$?
if [[ "$RC_D2498_9" -eq 0 && "$OUT_D2498_9" =~ ^[0-9a-f]{40}$ ]]; then
  _pass "D#2498: --base-ref equal to --target-ref still succeeds"
else
  _fail "D#2498: --base-ref equal to --target-ref failed, got rc=$RC_D2498_9 (stdout='$OUT_D2498_9'): $(cat "$TEST_SCRATCH/d2498-9.err")"
fi
rm -rf "$FX_D2498_9"

# ── D#2578 fixture helpers: derived-files regeneration ───────────────────────
# These build small, hermetic repos containing REAL derived-file infra
# (copied from this checkout's own working tree — never generated ad hoc),
# so the cases below exercise the helper's actual regeneration/guard logic
# rather than a parallel reimplementation of it.

_d2578_stage_and_commit() {
  # _d2578_stage_and_commit <dir> <message> <relpath...>
  local dir="$1" message="$2"; shift 2
  local idx="$TEST_SCRATCH/d2578-idx-$RANDOM-$RANDOM"
  local f blob
  for f in "$@"; do
    blob="$(git -C "$dir" hash-object -w "$dir/$f")"
    GIT_INDEX_FILE="$idx" git -C "$dir" update-index --add --cacheinfo "100644,$blob,$f"
  done
  local tree
  tree="$(GIT_INDEX_FILE="$idx" git -C "$dir" write-tree)"
  git -C "$dir" commit-tree "$tree" -m "$message"
}

RUFF_ON_PATH=true
if ! command -v ruff >/dev/null 2>&1; then
  RUFF_ON_PATH=false
fi
if $RUFF_ON_PATH; then
  RUFF_HOST_VERSION="$(ruff --version | awk '{print $2}')"
  RUFF_BASELINE_VERSION="$(grep -m1 '^# ruff-version:' "$REPO_ROOT/scripts/ruff-known-findings.txt" | sed 's/^# ruff-version: *//')"
  if [[ "$RUFF_HOST_VERSION" != "$RUFF_BASELINE_VERSION" ]]; then
    RUFF_ON_PATH=false
  fi
fi

# ── Case: manifest regeneration (item 3) ──────────────────────────────────────
echo ""
echo "=== D#2578 Case: build regenerates engine/manifest.json for a manifest-scoped write (item 3) ==="
FX_MANIFEST="$(_fixture_repo)"
mkdir -p "$FX_MANIFEST/scripts/engine-sync" "$FX_MANIFEST/scripts/lib" "$FX_MANIFEST/engine"
cp "$REPO_ROOT/scripts/engine-sync/manifest.py" "$FX_MANIFEST/scripts/engine-sync/manifest.py"
cp "$REPO_ROOT/scripts/engine-sync/allowlist.txt" "$FX_MANIFEST/scripts/engine-sync/allowlist.txt"
cp "$REPO_ROOT/engine/VERSION" "$FX_MANIFEST/engine/VERSION"
printf '#!/usr/bin/env bash\necho existing\n' > "$FX_MANIFEST/scripts/lib/existing.sh"
# Generate the manifest for the base tree FIRST (as if a prior commit had
# already regenerated it), so the base tree starts clean.
python3 "$FX_MANIFEST/scripts/engine-sync/manifest.py" generate >/dev/null
COMMIT_MANIFEST_BASE="$(_d2578_stage_and_commit "$FX_MANIFEST" "base, manifest already clean" \
  scripts/engine-sync/manifest.py scripts/engine-sync/allowlist.txt engine/VERSION \
  scripts/lib/existing.sh engine/manifest.json)"

LOCAL_NEWTHING="$TEST_SCRATCH/d2578-newthing.sh"
printf '#!/usr/bin/env bash\necho new\n' > "$LOCAL_NEWTHING"
OUT_MANIFEST="$(cd "$FX_MANIFEST" && code_plane_pr build --target-ref "$COMMIT_MANIFEST_BASE" --base-ref "$COMMIT_MANIFEST_BASE" \
  --branch test --message "add newthing" "scripts/lib/newthing.sh=$LOCAL_NEWTHING" 2>"$TEST_SCRATCH/d2578-manifest.err")"
RC_MANIFEST=$?

if [[ "$RC_MANIFEST" -eq 0 && "$OUT_MANIFEST" =~ ^[0-9a-f]{40}$ ]]; then
  _pass "item3: build exits 0 and prints a commit sha for a manifest-scoped write"
else
  _fail "item3: expected exit 0 with a commit sha, got rc=$RC_MANIFEST stdout='$OUT_MANIFEST': $(cat "$TEST_SCRATCH/d2578-manifest.err")"
fi
if grep -qE '^code-plane-pr\.sh: build: regenerated engine/manifest\.json' "$TEST_SCRATCH/d2578-manifest.err" \
   && grep -q "scripts/lib/newthing.sh" "$TEST_SCRATCH/d2578-manifest.err"; then
  _pass "item3: stderr names engine/manifest.json as regenerated and names the triggering path"
else
  _fail "item3: stderr missing the expected regenerated-manifest line: $(cat "$TEST_SCRATCH/d2578-manifest.err")"
fi
if [[ "$RC_MANIFEST" -eq 0 ]]; then
  BUILT_MANIFEST="$TEST_SCRATCH/d2578-built-manifest.json"
  git -C "$FX_MANIFEST" show "$OUT_MANIFEST:engine/manifest.json" > "$BUILT_MANIFEST" 2>/dev/null
  REGEN_DIR="$(cd "$FX_MANIFEST" && code_plane_pr extract --ref "$OUT_MANIFEST" 2>/dev/null)"
  python3 "$REGEN_DIR/scripts/engine-sync/manifest.py" generate >/dev/null
  if cmp -s "$BUILT_MANIFEST" "$REGEN_DIR/engine/manifest.json"; then
    _pass "item3: built manifest.json is byte-identical to regenerating it fresh against the built commit's own tree"
  else
    _fail "item3: built manifest.json does not match a fresh regeneration of the built commit's tree"
  fi
  rm -rf "$REGEN_DIR"
  CHANGED_MANIFEST="$(git -C "$FX_MANIFEST" diff --name-only "$COMMIT_MANIFEST_BASE" "$OUT_MANIFEST" | sort -u)"
  EXPECTED_MANIFEST="$(printf '%s\n' engine/manifest.json scripts/lib/newthing.sh | sort -u)"
  if [[ "$CHANGED_MANIFEST" == "$EXPECTED_MANIFEST" ]]; then
    _pass "item3: changed-path set is exactly the requested path plus engine/manifest.json"
  else
    _fail "item3: changed-path set was '$CHANGED_MANIFEST', expected '$EXPECTED_MANIFEST'"
  fi
fi

# ── Case: bounded regeneration refuses on out-of-scope drift (item 4) ────────
echo ""
echo "=== D#2578 Case: bounded regeneration refuses when the target's own manifest is already stale (item 4) ==="
FX_BOUNDED="$(_fixture_repo)"
mkdir -p "$FX_BOUNDED/scripts/engine-sync" "$FX_BOUNDED/scripts/lib" "$FX_BOUNDED/engine"
cp "$REPO_ROOT/scripts/engine-sync/manifest.py" "$FX_BOUNDED/scripts/engine-sync/manifest.py"
cp "$REPO_ROOT/scripts/engine-sync/allowlist.txt" "$FX_BOUNDED/scripts/engine-sync/allowlist.txt"
cp "$REPO_ROOT/engine/VERSION" "$FX_BOUNDED/engine/VERSION"
printf '#!/usr/bin/env bash\necho existing\n' > "$FX_BOUNDED/scripts/lib/existing.sh"
python3 "$FX_BOUNDED/scripts/engine-sync/manifest.py" generate >/dev/null
# Drift existing.sh AFTER generating the manifest -- a path the caller below
# never writes, matching D#2578's "target's own manifest is already stale
# for a path the caller did not write" scenario.
printf '#!/usr/bin/env bash\necho existing, drifted unrelated to this change\n' > "$FX_BOUNDED/scripts/lib/existing.sh"
COMMIT_BOUNDED_BASE="$(_d2578_stage_and_commit "$FX_BOUNDED" "existing.sh drifted vs its own pin" \
  scripts/engine-sync/manifest.py scripts/engine-sync/allowlist.txt engine/VERSION \
  scripts/lib/existing.sh engine/manifest.json)"

LOCAL_BOUNDED_NEW="$TEST_SCRATCH/d2578-bounded-newthing.sh"
printf '#!/usr/bin/env bash\necho new\n' > "$LOCAL_BOUNDED_NEW"
OUT_BOUNDED="$(cd "$FX_BOUNDED" && code_plane_pr build --target-ref "$COMMIT_BOUNDED_BASE" --base-ref "$COMMIT_BOUNDED_BASE" \
  --branch test --message "add newthing" "scripts/lib/newthing.sh=$LOCAL_BOUNDED_NEW" 2>"$TEST_SCRATCH/d2578-bounded.err")"
RC_BOUNDED=$?

if [[ "$RC_BOUNDED" -eq 5 ]]; then
  _pass "item4: build exits 5 when the target's own manifest is already stale for an unwritten path"
else
  _fail "item4: expected exit 5, got $RC_BOUNDED (stdout='$OUT_BOUNDED')"
fi
if [[ -z "$OUT_BOUNDED" ]]; then
  _pass "item4: build prints no commit sha on the bounded-regeneration refusal"
else
  _fail "item4: expected empty stdout, got '$OUT_BOUNDED'"
fi
if grep -q "scripts/lib/existing.sh" "$TEST_SCRATCH/d2578-bounded.err"; then
  _pass "item4: stderr names the out-of-scope drifted path"
else
  _fail "item4: stderr does not name scripts/lib/existing.sh: $(cat "$TEST_SCRATCH/d2578-bounded.err")"
fi

# ── Case: agents/ mirror regeneration (item 5) ────────────────────────────────
echo ""
echo "=== D#2578 Case: build regenerates the agents/ mirror for a written .claude/agents/*.md (item 5) ==="
FX_AGENTS="$(_fixture_repo)"
mkdir -p "$FX_AGENTS/scripts/lib" "$FX_AGENTS/.claude/agents" "$FX_AGENTS/agents"
cp "$REPO_ROOT/scripts/lib/agents-plugin-mirror.sh" "$FX_AGENTS/scripts/lib/agents-plugin-mirror.sh"
printf 'You ONLY interact with autonomous-agent-7/fulcrumaxe.\n' > "$FX_AGENTS/.claude/agents/roleA.md"
printf 'STALE OLD MIRROR CONTENT\n' > "$FX_AGENTS/agents/roleA.md"
COMMIT_AGENTS_BASE="$(_d2578_stage_and_commit "$FX_AGENTS" "base with a stale agents/ mirror" \
  scripts/lib/agents-plugin-mirror.sh .claude/agents/roleA.md agents/roleA.md)"

LOCAL_NEW_ROLE="$TEST_SCRATCH/d2578-roleA-new.md"
printf 'You ONLY interact with autonomous-agent-7/fulcrumaxe.\nAlso: gh issue view 5 --repo autonomous-agent-7/fulcrumaxe\n' > "$LOCAL_NEW_ROLE"
OUT_AGENTS="$(cd "$FX_AGENTS" && code_plane_pr build --target-ref "$COMMIT_AGENTS_BASE" --base-ref "$COMMIT_AGENTS_BASE" \
  --branch test --message "edit roleA" ".claude/agents/roleA.md=$LOCAL_NEW_ROLE" 2>"$TEST_SCRATCH/d2578-agents.err")"
RC_AGENTS=$?

if [[ "$RC_AGENTS" -eq 0 ]]; then
  _pass "item5: build exits 0 for a written .claude/agents/*.md path"
else
  _fail "item5: expected exit 0, got $RC_AGENTS: $(cat "$TEST_SCRATCH/d2578-agents.err")"
fi
if grep -q "regenerated agents/roleA.md" "$TEST_SCRATCH/d2578-agents.err"; then
  _pass "item5: stderr names agents/roleA.md as regenerated"
else
  _fail "item5: stderr does not name agents/roleA.md as regenerated: $(cat "$TEST_SCRATCH/d2578-agents.err")"
fi
if [[ "$RC_AGENTS" -eq 0 ]]; then
  EXPECTED_AGENTS="$(bash "$REPO_ROOT/scripts/lib/agents-plugin-mirror.sh" "$LOCAL_NEW_ROLE")"
  ACTUAL_AGENTS="$(git -C "$FX_AGENTS" show "$OUT_AGENTS:agents/roleA.md" 2>/dev/null)"
  if [[ "$ACTUAL_AGENTS" == "$EXPECTED_AGENTS" ]]; then
    _pass "item5: built agents/roleA.md equals agents-plugin-mirror.sh run on the new source"
  else
    _fail "item5: built agents/roleA.md does not match the expected mirror output"
  fi
fi

# ── Case: commands/ mirror regeneration (item 6) ──────────────────────────────
echo ""
echo "=== D#2578 Case: build regenerates the commands/ mirror as a byte copy (item 6) ==="
FX_COMMANDS="$(_fixture_repo)"
mkdir -p "$FX_COMMANDS/.claude/commands" "$FX_COMMANDS/commands"
printf 'command body\n' > "$FX_COMMANDS/.claude/commands/cmdA.md"
cp "$FX_COMMANDS/.claude/commands/cmdA.md" "$FX_COMMANDS/commands/cmdA.md"
COMMIT_COMMANDS_BASE="$(_d2578_stage_and_commit "$FX_COMMANDS" "base with matching commands/ mirror" \
  .claude/commands/cmdA.md commands/cmdA.md)"

LOCAL_NEW_CMD="$TEST_SCRATCH/d2578-cmdA-new.md"
printf 'command body v2\n' > "$LOCAL_NEW_CMD"
OUT_COMMANDS="$(cd "$FX_COMMANDS" && code_plane_pr build --target-ref "$COMMIT_COMMANDS_BASE" --base-ref "$COMMIT_COMMANDS_BASE" \
  --branch test --message "edit cmdA" ".claude/commands/cmdA.md=$LOCAL_NEW_CMD" 2>"$TEST_SCRATCH/d2578-commands.err")"
RC_COMMANDS=$?

if [[ "$RC_COMMANDS" -eq 0 ]]; then
  _pass "item6: build exits 0 for a written .claude/commands/*.md path"
else
  _fail "item6: expected exit 0, got $RC_COMMANDS: $(cat "$TEST_SCRATCH/d2578-commands.err")"
fi
if [[ "$RC_COMMANDS" -eq 0 ]]; then
  ACTUAL_COMMANDS="$(git -C "$FX_COMMANDS" show "$OUT_COMMANDS:commands/cmdA.md" 2>/dev/null)"
  if [[ "$ACTUAL_COMMANDS" == "command body v2" ]]; then
    _pass "item6: built commands/cmdA.md is a byte copy of the new .claude/commands/cmdA.md"
  else
    _fail "item6: built commands/cmdA.md = '$ACTUAL_COMMANDS', expected 'command body v2'"
  fi
fi

# ── Case: allowlisted twin is left untouched (item 7) ─────────────────────────
echo ""
echo "=== D#2578 Case: an allowlisted twin pair is not regenerated even though its source changes (item 7) ==="
FX_ALLOWLIST="$(_fixture_repo)"
mkdir -p "$FX_ALLOWLIST/scripts/lib" "$FX_ALLOWLIST/scripts/ci" "$FX_ALLOWLIST/.claude/agents" "$FX_ALLOWLIST/agents"
cp "$REPO_ROOT/scripts/lib/agents-plugin-mirror.sh" "$FX_ALLOWLIST/scripts/lib/agents-plugin-mirror.sh"
printf '{"entries": [{"pair": "agents:stable.md", "date": "2026-09-19", "reason": "deliberate allowlisted variant for test"}]}' \
  > "$FX_ALLOWLIST/scripts/ci/twin-divergence-allowlist.json"
printf 'ORIGINAL AGENT CARD FOR STABLE\n' > "$FX_ALLOWLIST/.claude/agents/stable.md"
printf 'DELIBERATELY DIFFERENT MIRROR CONTENT\n' > "$FX_ALLOWLIST/agents/stable.md"
COMMIT_ALLOWLIST_BASE="$(_d2578_stage_and_commit "$FX_ALLOWLIST" "base with an allowlisted deliberate variant" \
  scripts/lib/agents-plugin-mirror.sh scripts/ci/twin-divergence-allowlist.json \
  .claude/agents/stable.md agents/stable.md)"

LOCAL_NEW_STABLE="$TEST_SCRATCH/d2578-stable-new.md"
printf 'CHANGED STABLE CONTENT — should NOT be regenerated\n' > "$LOCAL_NEW_STABLE"
OUT_ALLOWLIST="$(cd "$FX_ALLOWLIST" && code_plane_pr build --target-ref "$COMMIT_ALLOWLIST_BASE" --base-ref "$COMMIT_ALLOWLIST_BASE" \
  --branch test --message "edit stable" ".claude/agents/stable.md=$LOCAL_NEW_STABLE" 2>"$TEST_SCRATCH/d2578-allowlist.err")"
RC_ALLOWLIST=$?

if [[ "$RC_ALLOWLIST" -eq 0 ]]; then
  _pass "item7: build exits 0 when the only affected mirror is allowlisted"
else
  _fail "item7: expected exit 0, got $RC_ALLOWLIST: $(cat "$TEST_SCRATCH/d2578-allowlist.err")"
fi
if [[ "$RC_ALLOWLIST" -eq 0 ]]; then
  ORIG_STABLE_BLOB="$(git -C "$FX_ALLOWLIST" rev-parse "$COMMIT_ALLOWLIST_BASE:agents/stable.md" 2>/dev/null)"
  BUILT_STABLE_BLOB="$(git -C "$FX_ALLOWLIST" rev-parse "$OUT_ALLOWLIST:agents/stable.md" 2>/dev/null)"
  if [[ -n "$ORIG_STABLE_BLOB" && "$ORIG_STABLE_BLOB" == "$BUILT_STABLE_BLOB" ]]; then
    _pass "item7: agents/stable.md's blob is unchanged (allowlisted, not regenerated)"
  else
    _fail "item7: agents/stable.md blob changed despite being allowlisted"
  fi
fi
if grep -qi "allowlisted" "$TEST_SCRATCH/d2578-allowlist.err"; then
  _pass "item7: stderr names the allowlisted pair"
else
  _fail "item7: stderr does not mention the allowlisted pair: $(cat "$TEST_SCRATCH/d2578-allowlist.err")"
fi

# ── Case: ruff refusal on baseline over-allowance (item 8) ────────────────────
echo ""
echo "=== D#2578 Case: ruff-ratchet refusal when a baselined finding no longer reproduces (item 8) ==="
if ! $RUFF_ON_PATH; then
  echo "SKIP: item8 — ruff is absent from PATH, or its version does not match scripts/ruff-known-findings.txt's baseline; cannot exercise the real ruff refusal path on this host"
else
  FX_RUFF="$(_fixture_repo)"
  mkdir -p "$FX_RUFF/scripts/ci" "$FX_RUFF/backend" "$FX_RUFF/tests" "$FX_RUFF/scripts"
  cp "$REPO_ROOT/scripts/ci/ruff-ratchet.py" "$FX_RUFF/scripts/ci/ruff-ratchet.py"
  cp "$REPO_ROOT/ruff.toml" "$FX_RUFF/ruff.toml"
  : > "$FX_RUFF/backend/__init__.py"
  : > "$FX_RUFF/tests/__init__.py"
  printf '"""Fixture file - clean."""\n\n\ndef noop() -> None:\n    return None\n' > "$FX_RUFF/scripts/fixture_lint_target.py"
  BASELINE_LINE_8=$'scripts/fixture_lint_target.py\tF401\t1\t`os` imported but unused'
  {
    printf '# RUFF-RATCHET-V1\n#\n# ruff-version: %s\n' "$RUFF_HOST_VERSION"
    printf '# scope: backend/ tests/ scripts/ (must match Makefile'"'"'s `lint` target)\n#\n'
    printf '%s\n' "$BASELINE_LINE_8"
  } > "$FX_RUFF/scripts/ruff-known-findings.txt"
  COMMIT_RUFF_BASE="$(_d2578_stage_and_commit "$FX_RUFF" "base with a stale ruff baseline over-allowance" \
    scripts/ci/ruff-ratchet.py ruff.toml backend/__init__.py tests/__init__.py \
    scripts/fixture_lint_target.py scripts/ruff-known-findings.txt)"

  LOCAL_UNRELATED_8="$TEST_SCRATCH/d2578-ruff-unrelated.sh"
  printf 'echo unrelated\n' > "$LOCAL_UNRELATED_8"
  OUT_RUFF="$(cd "$FX_RUFF" && code_plane_pr build --target-ref "$COMMIT_RUFF_BASE" --base-ref "$COMMIT_RUFF_BASE" \
    --branch test --message "unrelated change" "scripts/unrelated.sh=$LOCAL_UNRELATED_8" 2>"$TEST_SCRATCH/d2578-ruff.err")"
  RC_RUFF=$?

  if [[ "$RC_RUFF" -eq 5 && -z "$OUT_RUFF" ]]; then
    _pass "item8: build exits 5 with no sha when a baselined ruff finding no longer reproduces"
  else
    _fail "item8: expected exit 5 with no sha, got rc=$RC_RUFF stdout='$OUT_RUFF'"
  fi
  if grep -qF "$BASELINE_LINE_8" "$TEST_SCRATCH/d2578-ruff.err" && grep -q "delete this line" "$TEST_SCRATCH/d2578-ruff.err"; then
    _pass "item8: stderr contains the verbatim baseline line followed by 'delete this line'"
  else
    _fail "item8: stderr missing the verbatim baseline line or 'delete this line': $(cat "$TEST_SCRATCH/d2578-ruff.err")"
  fi
  if ! grep -q "^scripts/ruff-known-findings.txt$" <(git -C "$FX_RUFF" diff --name-only "$COMMIT_RUFF_BASE" 2>/dev/null); then
    _pass "item8: scripts/ruff-known-findings.txt is not touched by the refusal"
  fi
fi

# ── Case: ruff unavailable is a WARN, not a refusal (item 9) ──────────────────
echo ""
echo "=== D#2578 Case: ruff absent from PATH is a WARN, not a refusal (item 9) ==="
FX_RUFF9="$(_fixture_repo)"
mkdir -p "$FX_RUFF9/scripts/ci" "$FX_RUFF9/backend" "$FX_RUFF9/tests" "$FX_RUFF9/scripts"
cp "$REPO_ROOT/scripts/ci/ruff-ratchet.py" "$FX_RUFF9/scripts/ci/ruff-ratchet.py"
cp "$REPO_ROOT/ruff.toml" "$FX_RUFF9/ruff.toml"
: > "$FX_RUFF9/backend/__init__.py"
: > "$FX_RUFF9/tests/__init__.py"
printf '"""Fixture file - clean."""\n\n\ndef noop() -> None:\n    return None\n' > "$FX_RUFF9/scripts/fixture_lint_target.py"
{
  printf '# RUFF-RATCHET-V1\n#\n# ruff-version: 999.999.999\n'
  printf '# scope: backend/ tests/ scripts/ (must match Makefile'"'"'s `lint` target)\n#\n'
} > "$FX_RUFF9/scripts/ruff-known-findings.txt"
COMMIT_RUFF9_BASE="$(_d2578_stage_and_commit "$FX_RUFF9" "base, ruff baseline present but irrelevant to this case" \
  scripts/ci/ruff-ratchet.py ruff.toml backend/__init__.py tests/__init__.py \
  scripts/fixture_lint_target.py scripts/ruff-known-findings.txt)"

NOPATH_9=""
IFS=':' read -ra _D2578_PDIRS <<<"$PATH"
for _d2578_dir in "${_D2578_PDIRS[@]}"; do
  [[ -x "$_d2578_dir/ruff" ]] && continue
  NOPATH_9="${NOPATH_9:+$NOPATH_9:}$_d2578_dir"
done
LOCAL_UNRELATED_9="$TEST_SCRATCH/d2578-ruff9-unrelated.sh"
printf 'echo unrelated\n' > "$LOCAL_UNRELATED_9"
OUT_RUFF9="$(cd "$FX_RUFF9" && PATH="$NOPATH_9" code_plane_pr build --target-ref "$COMMIT_RUFF9_BASE" --base-ref "$COMMIT_RUFF9_BASE" \
  --branch test --message "unrelated change" "scripts/unrelated9.sh=$LOCAL_UNRELATED_9" 2>"$TEST_SCRATCH/d2578-ruff9.err")"
RC_RUFF9=$?

if [[ "$RC_RUFF9" -eq 0 ]]; then
  _pass "item9: build exits 0 when ruff is absent from PATH"
else
  _fail "item9: expected exit 0 with ruff absent, got $RC_RUFF9: $(cat "$TEST_SCRATCH/d2578-ruff9.err")"
fi
if grep -q "WARN" "$TEST_SCRATCH/d2578-ruff9.err" && grep -q "ruff-ratchet" "$TEST_SCRATCH/d2578-ruff9.err"; then
  _pass "item9: stderr contains WARN and names ruff-ratchet"
else
  _fail "item9: stderr missing WARN/ruff-ratchet: $(cat "$TEST_SCRATCH/d2578-ruff9.err")"
fi

# ── Case: quiet path (item 10) ─────────────────────────────────────────────────
echo ""
echo "=== D#2578 Case: a write with no derived-file source stays quiet (item 10) ==="
FX_QUIET="$(_fixture_repo)"
mkdir -p "$FX_QUIET/tests"
printf 'echo old\n' > "$FX_QUIET/tests/existing_test.sh"
COMMIT_QUIET_BASE="$(_d2578_stage_and_commit "$FX_QUIET" "base for the quiet path" tests/existing_test.sh)"

LOCAL_NEW_TEST="$TEST_SCRATCH/d2578-newtest.sh"
printf 'echo new test\n' > "$LOCAL_NEW_TEST"
OUT_QUIET="$(cd "$FX_QUIET" && code_plane_pr build --target-ref "$COMMIT_QUIET_BASE" --base-ref "$COMMIT_QUIET_BASE" \
  --branch test --message "add a test" "tests/newtest.sh=$LOCAL_NEW_TEST" 2>"$TEST_SCRATCH/d2578-quiet.err")"
RC_QUIET=$?

if [[ "$RC_QUIET" -eq 0 ]]; then
  _pass "item10: build exits 0 for a tests/-only write"
else
  _fail "item10: expected exit 0, got $RC_QUIET: $(cat "$TEST_SCRATCH/d2578-quiet.err")"
fi
if ! grep -qE 'regenerated|replaced|WARN' "$TEST_SCRATCH/d2578-quiet.err"; then
  _pass "item10: stderr contains no regenerated/replaced/WARN line"
else
  _fail "item10: stderr is not quiet: $(cat "$TEST_SCRATCH/d2578-quiet.err")"
fi
if [[ "$RC_QUIET" -eq 0 ]]; then
  CHANGED_QUIET="$(git -C "$FX_QUIET" diff --name-only "$COMMIT_QUIET_BASE" "$OUT_QUIET")"
  if [[ "$CHANGED_QUIET" == "tests/newtest.sh" ]]; then
    _pass "item10: changed-path set equals the requested set exactly"
  else
    _fail "item10: changed-path set was '$CHANGED_QUIET'"
  fi
fi

# ── Case: push re-verifies before the network call (item 11) ─────────────────
echo ""
echo "=== D#2578 Case: push refuses a guard-failing commit before any network call (item 11) ==="
if ! $RUFF_ON_PATH; then
  echo "SKIP: item11 — needs the same real-ruff fixture as item8"
else
  FX_PUSH="$(_fixture_repo)"
  mkdir -p "$FX_PUSH/scripts/ci" "$FX_PUSH/backend" "$FX_PUSH/tests" "$FX_PUSH/scripts"
  cp "$REPO_ROOT/scripts/ci/ruff-ratchet.py" "$FX_PUSH/scripts/ci/ruff-ratchet.py"
  cp "$REPO_ROOT/ruff.toml" "$FX_PUSH/ruff.toml"
  : > "$FX_PUSH/backend/__init__.py"
  : > "$FX_PUSH/tests/__init__.py"
  printf '"""Fixture file - clean."""\n\n\ndef noop() -> None:\n    return None\n' > "$FX_PUSH/scripts/fixture_lint_target.py"
  {
    printf '# RUFF-RATCHET-V1\n#\n# ruff-version: %s\n' "$RUFF_HOST_VERSION"
    printf '# scope: backend/ tests/ scripts/ (must match Makefile'"'"'s `lint` target)\n#\n'
    printf 'scripts/fixture_lint_target.py\tF401\t1\t`os` imported but unused\n'
  } > "$FX_PUSH/scripts/ruff-known-findings.txt"
  COMMIT_PUSH="$(_d2578_stage_and_commit "$FX_PUSH" "commit whose tree fails the ruff guard" \
    scripts/ci/ruff-ratchet.py ruff.toml backend/__init__.py tests/__init__.py \
    scripts/fixture_lint_target.py scripts/ruff-known-findings.txt)"

  OUT_PUSH="$(cd "$FX_PUSH" && code_plane_pr push --remote /nonexistent/path/does-not-exist --branch x --commit "$COMMIT_PUSH" 2>"$TEST_SCRATCH/d2578-push.err")"
  RC_PUSH=$?
  if [[ "$RC_PUSH" -ne 0 ]] && grep -q "push: REFUSED" "$TEST_SCRATCH/d2578-push.err"; then
    _pass "item11: push refuses a guard-failing commit and says 'push: REFUSED'"
  else
    _fail "item11: expected a 'push: REFUSED' refusal, got rc=$RC_PUSH: $(cat "$TEST_SCRATCH/d2578-push.err")"
  fi
  if ! grep -qiE "could not read from remote|does not appear to be a git repository" "$TEST_SCRATCH/d2578-push.err"; then
    _pass "item11: no git transport error leaked through — refusal happened before the network call"
  else
    _fail "item11: a git transport error appeared, meaning the refusal did not happen first: $(cat "$TEST_SCRATCH/d2578-push.err")"
  fi

  OUT_PUSH_SKIP="$(cd "$FX_PUSH" && code_plane_pr push --remote /nonexistent/path/does-not-exist --branch x --commit "$COMMIT_PUSH" --skip-guards "testing bypass" 2>"$TEST_SCRATCH/d2578-push-skip.err")"
  if grep -q 'WARN --skip-guards: testing bypass' "$TEST_SCRATCH/d2578-push-skip.err"; then
    _pass "item11: --skip-guards prints a loud WARN quoting the reason"
  else
    _fail "item11: --skip-guards did not print the expected WARN: $(cat "$TEST_SCRATCH/d2578-push-skip.err")"
  fi
  if ! grep -q "push: REFUSED" "$TEST_SCRATCH/d2578-push-skip.err"; then
    _pass "item11: --skip-guards proceeds to the transport instead of refusing"
  else
    _fail "item11: --skip-guards still refused: $(cat "$TEST_SCRATCH/d2578-push-skip.err")"
  fi

  OUT_PUSH_EMPTY="$(cd "$FX_PUSH" && code_plane_pr push --remote /nonexistent/path/does-not-exist --branch x --commit "$COMMIT_PUSH" --skip-guards "" 2>"$TEST_SCRATCH/d2578-push-empty.err")"
  RC_PUSH_EMPTY=$?
  if [[ "$RC_PUSH_EMPTY" -eq 2 ]]; then
    _pass "item11: --skip-guards with an empty reason is a usage error (exit 2)"
  else
    _fail "item11: expected exit 2 for an empty --skip-guards reason, got $RC_PUSH_EMPTY"
  fi
fi

# ── Case: containment (item 12) ────────────────────────────────────────────────
echo ""
echo "=== D#2578 Case: containment — no source/dot-source, no --write-baseline, cwd unchanged (item 12) ==="
if grep -nE '^[[:space:]]*(source|\.)[[:space:]]' "$HELPER" >/dev/null; then
  _fail "item12: helper sources a sibling file (must stay self-contained)"
else
  _pass "item12: helper contains no 'source'/'.' line (self-contained, per grep -nE)"
fi
if [[ "$(grep -c -- '--write-baseline' "$HELPER")" -eq 0 ]]; then
  _pass "item12: helper never invokes --write-baseline"
else
  _fail "item12: helper's source mentions --write-baseline"
fi

FX_CONTAIN="$(_fixture_repo)"
mkdir -p "$FX_CONTAIN/scripts/lib"
printf 'echo v1\n' > "$FX_CONTAIN/scripts/lib/thing.sh"
COMMIT_CONTAIN_BASE="$(_d2578_stage_and_commit "$FX_CONTAIN" "base for containment case" scripts/lib/thing.sh)"

CALLER_CWD="$(mktemp -d)"
CWD_LISTING_BEFORE="$(ls -A "$CALLER_CWD")"
LOCAL_CONTAIN="$TEST_SCRATCH/d2578-contain.sh"
printf 'echo v2\n' > "$LOCAL_CONTAIN"
(cd "$CALLER_CWD" && cd "$FX_CONTAIN" && code_plane_pr build --target-ref "$COMMIT_CONTAIN_BASE" --base-ref "$COMMIT_CONTAIN_BASE" \
  --branch test --message "edit thing" "scripts/lib/thing.sh=$LOCAL_CONTAIN" >/dev/null 2>&1) || true
CWD_LISTING_AFTER="$(ls -A "$CALLER_CWD")"
if [[ "$CWD_LISTING_BEFORE" == "$CWD_LISTING_AFTER" ]]; then
  _pass "item12: a directory unrelated to the fixture is untouched by the run"
else
  _fail "item12: an unrelated directory's listing changed: before='$CWD_LISTING_BEFORE' after='$CWD_LISTING_AFTER'"
fi
rm -rf "$CALLER_CWD"
rm -rf "$FX_MANIFEST" "$FX_BOUNDED" "$FX_AGENTS" "$FX_COMMANDS" "$FX_ALLOWLIST" \
       "${FX_RUFF:-}" "$FX_RUFF9" "$FX_QUIET" "${FX_PUSH:-}" "$FX_CONTAIN"

# ── D#2622 Case: a guard-clean tree passes the full run-guards.sh suite
#                (item 1) ──────────────────────────────────────────────────
# Fix round 1: the fixture used to plant a synthetic .autonomous-team/
# config.json, which is exactly what the real code plane never ships — that
# planted file let the tree "self-resolve" in the test even though the real
# tree never can, so the suite never exercised the fallback path that fires
# in production. No config.json is planted anywhere below. Instead the test
# passes --code-repo explicitly, the way a real caller does (resolved once,
# in the caller's own checkout, before calling build), and a guard script
# checks the exact value the guard subprocess actually sees for
# AUTONOMOUS_TEAM_REPO — proving both that the guard ran and that it saw the
# right value, not a fallback.
echo ""
echo "=== D#2622 Case: build runs the full run-guards.sh suite and passes on a guard-clean tree (item 1) ==="
FX_RUNGUARDS_OK="$(_fixture_repo)"
mkdir -p "$FX_RUNGUARDS_OK/scripts/ci" "$FX_RUNGUARDS_OK/scripts/lib"
cp "$REPO_ROOT/scripts/ci/run-guards.sh" "$FX_RUNGUARDS_OK/scripts/ci/run-guards.sh"
cat > "$FX_RUNGUARDS_OK/scripts/ci/fixture-repo-check-guard.sh" <<'GUARDEOF'
#!/usr/bin/env bash
# Fails unless AUTONOMOUS_TEAM_REPO is exactly the slug this fixture's test
# case expects, so a passing run also proves the guard subprocess saw the
# caller-supplied --code-repo value and nothing else (not unset, not a
# value leaked from the caller's own shell).
if [[ "${AUTONOMOUS_TEAM_REPO:-}" != "fixture-org/fixture-repo" ]]; then
  echo "expected AUTONOMOUS_TEAM_REPO=fixture-org/fixture-repo, got '${AUTONOMOUS_TEAM_REPO:-<unset>}'" >&2
  exit 1
fi
exit 0
GUARDEOF
COMMIT_RUNGUARDS_OK_BASE="$(_d2578_stage_and_commit "$FX_RUNGUARDS_OK" "base for a guard-clean tree" \
  scripts/ci/run-guards.sh scripts/ci/fixture-repo-check-guard.sh)"

LOCAL_RUNGUARDS_OK="$TEST_SCRATCH/d2622-ok-trivial.txt"
printf 'trivial content\n' > "$LOCAL_RUNGUARDS_OK"
# env -u: the fixture carries no config.json (the real tree never does
# either), and this shell has no AUTONOMOUS_TEAM_REPO of its own — the only
# way the guard subprocess can see the right value is via --code-repo.
OUT_RUNGUARDS_OK="$(cd "$FX_RUNGUARDS_OK" && unset AUTONOMOUS_TEAM_REPO && code_plane_pr build \
  --target-ref "$COMMIT_RUNGUARDS_OK_BASE" --base-ref "$COMMIT_RUNGUARDS_OK_BASE" \
  --code-repo "fixture-org/fixture-repo" \
  --branch test --message "d2622 clean guard run" "trivial.txt=$LOCAL_RUNGUARDS_OK" 2>"$TEST_SCRATCH/d2622-ok.err")"
RC_RUNGUARDS_OK=$?

if [[ "$RC_RUNGUARDS_OK" -eq 0 && "$OUT_RUNGUARDS_OK" =~ ^[0-9a-f]{40}$ ]]; then
  _pass "item1 (D#2622): build exits 0 and prints a commit sha on a guard-clean tree, AUTONOMOUS_TEAM_REPO unset"
else
  _fail "item1 (D#2622): expected exit 0 with a commit sha, got rc=$RC_RUNGUARDS_OK stdout='$OUT_RUNGUARDS_OK': $(cat "$TEST_SCRATCH/d2622-ok.err")"
fi
if grep -q 'run-guards: OK' "$TEST_SCRATCH/d2622-ok.err"; then
  _pass "item1 (D#2622): build's stderr contains 'run-guards: OK'"
else
  _fail "item1 (D#2622): build's stderr does not contain 'run-guards: OK': $(cat "$TEST_SCRATCH/d2622-ok.err")"
fi
if grep -qE '0 failed' "$TEST_SCRATCH/d2622-ok.err"; then
  _pass "item1 (D#2622): build's stderr contains a summary line with '0 failed'"
else
  _fail "item1 (D#2622): build's stderr does not contain a '0 failed' summary: $(cat "$TEST_SCRATCH/d2622-ok.err")"
fi

# ── D#2622 Case: a leaked ambient AUTONOMOUS_TEAM_REPO must not reach the
#                guards (item 4 / fix round 1) ──────────────────────────────
# Reproduces the reviewer's second finding directly: export an arbitrary,
# wrong value into the CALLING shell before invoking build, still pass the
# correct --code-repo, and confirm the guard subprocess sees only the
# --code-repo value — never the caller's own exported one.
echo ""
echo "=== D#2622 Case: a bogus caller-exported AUTONOMOUS_TEAM_REPO does not reach the guard subprocess ==="
OUT_RUNGUARDS_LEAK="$(cd "$FX_RUNGUARDS_OK" && AUTONOMOUS_TEAM_REPO="LEAKED-org/LEAKED-repo" code_plane_pr build \
  --target-ref "$COMMIT_RUNGUARDS_OK_BASE" --base-ref "$COMMIT_RUNGUARDS_OK_BASE" \
  --code-repo "fixture-org/fixture-repo" \
  --branch test --message "d2622 leaked env must not reach guards" "trivial.txt=$LOCAL_RUNGUARDS_OK" 2>"$TEST_SCRATCH/d2622-leak.err")"
RC_RUNGUARDS_LEAK=$?

if [[ "$RC_RUNGUARDS_LEAK" -eq 0 && "$OUT_RUNGUARDS_LEAK" =~ ^[0-9a-f]{40}$ ]]; then
  _pass "leaked-env (D#2622): build still exits 0 using --code-repo despite a bogus AUTONOMOUS_TEAM_REPO in the caller's shell"
else
  _fail "leaked-env (D#2622): expected exit 0 with a commit sha, got rc=$RC_RUNGUARDS_LEAK stdout='$OUT_RUNGUARDS_LEAK': $(cat "$TEST_SCRATCH/d2622-leak.err")"
fi
if grep -q 'run-guards: OK' "$TEST_SCRATCH/d2622-leak.err" && ! grep -q 'LEAKED-org/LEAKED-repo' "$TEST_SCRATCH/d2622-leak.err"; then
  _pass "leaked-env (D#2622): guard subprocess saw the --code-repo value, not the leaked one"
else
  _fail "leaked-env (D#2622): the leaked value reached the guard, or the guard did not run cleanly: $(cat "$TEST_SCRATCH/d2622-leak.err")"
fi
rm -rf "$FX_RUNGUARDS_OK"

# ── D#2622 Case: omitting --code-repo refuses loudly, never a silent pass
#                (item 2 / item 4, fix round 1) ─────────────────────────────
# Reproduces the reviewer's primary finding: with the runner present in the
# tree and no --code-repo supplied, build must refuse (exit 2) before ever
# dispatching a single guard — not fall through to whatever the caller's
# shell happens to have exported, and not silently skip the check.
echo ""
echo "=== D#2622 Case: build refuses (exit 2) when --code-repo is omitted and the runner is present ==="
FX_RUNGUARDS_NOREPO="$(_fixture_repo)"
mkdir -p "$FX_RUNGUARDS_NOREPO/scripts/ci"
cp "$REPO_ROOT/scripts/ci/run-guards.sh" "$FX_RUNGUARDS_NOREPO/scripts/ci/run-guards.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$FX_RUNGUARDS_NOREPO/scripts/ci/fixture-clean-guard.sh"
COMMIT_RUNGUARDS_NOREPO_BASE="$(_d2578_stage_and_commit "$FX_RUNGUARDS_NOREPO" "base, no --code-repo case" \
  scripts/ci/run-guards.sh scripts/ci/fixture-clean-guard.sh)"

LOCAL_RUNGUARDS_NOREPO="$TEST_SCRATCH/d2622-norepo-trivial.txt"
printf 'trivial content\n' > "$LOCAL_RUNGUARDS_NOREPO"
# Tried once with a bogus value ALSO exported ambiently, so a regression
# that silently falls back to the environment is caught even though it
# would "work" by accident in the plain-unset case.
OUT_RUNGUARDS_NOREPO="$(cd "$FX_RUNGUARDS_NOREPO" && AUTONOMOUS_TEAM_REPO="LEAKED-org/LEAKED-repo" code_plane_pr build \
  --target-ref "$COMMIT_RUNGUARDS_NOREPO_BASE" --base-ref "$COMMIT_RUNGUARDS_NOREPO_BASE" \
  --branch test --message "d2622 no code-repo given" "trivial.txt=$LOCAL_RUNGUARDS_NOREPO" 2>"$TEST_SCRATCH/d2622-norepo.err")"
RC_RUNGUARDS_NOREPO=$?

if [[ "$RC_RUNGUARDS_NOREPO" -eq 2 ]]; then
  _pass "no-code-repo (D#2622): build refuses with exit 2 when --code-repo is omitted"
else
  _fail "no-code-repo (D#2622): expected exit 2, got $RC_RUNGUARDS_NOREPO (stdout='$OUT_RUNGUARDS_NOREPO'): $(cat "$TEST_SCRATCH/d2622-norepo.err")"
fi
if grep -q -- '--code-repo' "$TEST_SCRATCH/d2622-norepo.err"; then
  _pass "no-code-repo (D#2622): stderr names --code-repo as the missing input"
else
  _fail "no-code-repo (D#2622): stderr does not mention --code-repo: $(cat "$TEST_SCRATCH/d2622-norepo.err")"
fi
if grep -qE '^--- fixture-clean-guard\.sh$' "$TEST_SCRATCH/d2622-norepo.err"; then
  _fail "no-code-repo (D#2622): run-guards.sh was dispatched at all — refusal must happen before any guard runs: $(cat "$TEST_SCRATCH/d2622-norepo.err")"
else
  _pass "no-code-repo (D#2622): refusal happens before run-guards.sh dispatches any guard"
fi
rm -rf "$FX_RUNGUARDS_NOREPO"

# ── D#2622 Case: a behavioural regression is refused (item 2) ───────────────
# Plants a canonical-shaped spawn id in a NEW tracked file so
# no-planted-spawn-ids-guard.py fails — a `git ls-files`-based guard, not one
# of the three derived-file guards, so this exercises the materialized git
# index (_cpp_materialize_git_index), not just the archive-only path.
# Fix round 1: no planted config.json here either — --code-repo is passed
# explicitly and the ambient shell has no AUTONOMOUS_TEAM_REPO of its own.
echo ""
echo "=== D#2622 Case: build refuses a behavioural (non-derived-file) guard failure (item 2) ==="
FX_RUNGUARDS_FAIL="$(_fixture_repo)"
mkdir -p "$FX_RUNGUARDS_FAIL/scripts/ci" "$FX_RUNGUARDS_FAIL/docs"
cp "$REPO_ROOT/scripts/ci/run-guards.sh" "$FX_RUNGUARDS_FAIL/scripts/ci/run-guards.sh"
cp "$REPO_ROOT/scripts/ci/no-planted-spawn-ids-guard.py" "$FX_RUNGUARDS_FAIL/scripts/ci/no-planted-spawn-ids-guard.py"
printf 'benign doc content, no plant here\n' > "$FX_RUNGUARDS_FAIL/docs/notes.md"
COMMIT_RUNGUARDS_FAIL_BASE="$(_d2578_stage_and_commit "$FX_RUNGUARDS_FAIL" "base for a guard-clean tree, item 2" \
  scripts/ci/run-guards.sh scripts/ci/no-planted-spawn-ids-guard.py docs/notes.md)"

LOCAL_PLANT="$TEST_SCRATCH/d2622-planted.md"
# Assembled from two separate fragments, same discipline
# no-planted-spawn-ids-guard.py uses for its own source: this test file is
# ITSELF a tracked file the real run-guards.sh scans, so writing the tag
# immediately adjacent to a canonical-shaped id as one literal here would
# plant a hit against this very test file.
PLANT_TAG_D2622="hook_event_id="
PLANT_ID_D2622="executor-42-1735000000"
printf 'a doc with a planted spawn id: %s%s\n' "$PLANT_TAG_D2622" "$PLANT_ID_D2622" > "$LOCAL_PLANT"
OUT_RUNGUARDS_FAIL="$(cd "$FX_RUNGUARDS_FAIL" && unset AUTONOMOUS_TEAM_REPO && code_plane_pr build \
  --target-ref "$COMMIT_RUNGUARDS_FAIL_BASE" --base-ref "$COMMIT_RUNGUARDS_FAIL_BASE" \
  --code-repo "fixture-org/fixture-repo" \
  --branch test --message "d2622 planted id trips the guard" "docs/planted.md=$LOCAL_PLANT" 2>"$TEST_SCRATCH/d2622-fail.err")"
RC_RUNGUARDS_FAIL=$?

if [[ "$RC_RUNGUARDS_FAIL" -eq 5 ]]; then
  _pass "item2 (D#2622): build exits 5 (documented refusal code) when a behavioural guard fails"
else
  _fail "item2 (D#2622): expected exit 5, got $RC_RUNGUARDS_FAIL (stdout='$OUT_RUNGUARDS_FAIL'): $(cat "$TEST_SCRATCH/d2622-fail.err")"
fi
if [[ -z "$OUT_RUNGUARDS_FAIL" ]]; then
  _pass "item2 (D#2622): build prints no commit sha when the behavioural guard refuses"
else
  _fail "item2 (D#2622): expected empty stdout on guard refusal, got '$OUT_RUNGUARDS_FAIL'"
fi
if grep -q 'no-planted-spawn-ids-guard.py' "$TEST_SCRATCH/d2622-fail.err"; then
  _pass "item2 (D#2622): build's stderr names the failing guard by filename"
else
  _fail "item2 (D#2622): build's stderr does not name no-planted-spawn-ids-guard.py: $(cat "$TEST_SCRATCH/d2622-fail.err")"
fi
rm -rf "$FX_RUNGUARDS_FAIL"

# ── Summary ───────────────────────────────────────────────────────────────────
rm -rf "$FX4" "$FX6" "$FX8"

echo ""
echo "=== Results ==="
echo "PASS: $PASS  FAIL: $FAIL"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
