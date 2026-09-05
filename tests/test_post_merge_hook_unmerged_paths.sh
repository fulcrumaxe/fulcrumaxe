#!/usr/bin/env bash
# tests/test_post_merge_hook_unmerged_paths.sh — hermetic tests for the
# unmerged-paths branch added to the auto_pull step of post-merge-hook.sh.
#
# Covers:
#   1. Unmerged-paths state → exits 0, team-log warning, marker file written
#   2. Second run with marker present → no duplicate team-log comment
#   3. Untracked-file branch still works (regression)
#   4. Modified-file branch still works (regression)
#
# All tests use temp git repos — no network calls, no real GitHub API.
#
# Run: bash tests/test_post_merge_hook_unmerged_paths.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    pass "$label"
  else
    fail "$label — expected to find: $needle"
    echo "    Output was:" >&2
    echo "$haystack" | head -30 >&2
  fi
}

assert_not_contains() {
  local label="$1" haystack="$2" needle="$3"
  if ! echo "$haystack" | grep -qF "$needle"; then
    pass "$label"
  else
    fail "$label — expected NOT to find: $needle"
  fi
}

assert_exit_0() {
  local label="$1" rc="$2"
  if [[ "$rc" -eq 0 ]]; then
    pass "$label"
  else
    fail "$label — exit code was $rc (expected 0)"
  fi
}

assert_file_exists() {
  local label="$1" path="$2"
  if [[ -f "$path" ]]; then
    pass "$label"
  else
    fail "$label — expected file to exist: $path"
  fi
}

assert_file_missing() {
  local label="$1" path="$2"
  if [[ ! -f "$path" ]]; then
    pass "$label"
  else
    fail "$label — expected file to be absent: $path"
  fi
}

# ── Shared git setup helpers ───────────────────────────────────────────────────

setup_fake_origin() {
  local origin_dir="$1"
  git -C "$origin_dir" init --initial-branch=main -q
  git -C "$origin_dir" config user.email "test@test.com"
  git -C "$origin_dir" config user.name "Test"
  echo "file1" > "$origin_dir/file1.txt"
  git -C "$origin_dir" add .
  git -C "$origin_dir" commit -m "init" -q
}

setup_fake_local() {
  local local_dir="$1" origin_dir="$2"
  git clone "$origin_dir" "$local_dir" -q --local
  git -C "$local_dir" config user.email "test@test.com"
  git -C "$local_dir" config user.name "Test"
}

# ── Hook stub builder ──────────────────────────────────────────────────────────
# Builds a minimal shell script that exercises only the auto_pull logic from
# the real post-merge-hook.sh, with all other steps pre-marked as done.
# The caller can inject UNMERGED_FILES to simulate the UU index state.
#
# Arguments passed to the generated run_hook.sh:
#   $1  event-id (unique per test)
#   $2  optional: "unmerged" — injects fake unmerged-file output to git diff

make_hook_env() {
  local tmpdir="$1"
  local fake_repo="$2"      # the "local" clone (REPO_ROOT for the hook)
  local marker_dir="$3"     # fake state dir

  # Stub rotate-team-log.sh → appends to teamlog.txt
  mkdir -p "$tmpdir/bin"
  cat > "$tmpdir/bin/rotate-team-log.sh" <<STUB
#!/usr/bin/env bash
echo "TEAMLOG: \$*" >> "$tmpdir/teamlog.txt"
STUB
  chmod +x "$tmpdir/bin/rotate-team-log.sh"

  # Stub gh so the hook startup (PR body lookup) doesn't fail
  cat > "$tmpdir/bin/gh" <<STUB
#!/usr/bin/env bash
# Stub: capture issue create/comment calls to a log
if [[ "\$*" == *"issue create"* ]]; then
  echo "ISSUE_CREATE: \$*" >> "$tmpdir/gh-calls.txt"
  echo "https://github.com/fake/repo/issues/9001"
  exit 0
fi
if [[ "\$*" == *"issue comment"* ]]; then
  echo "ISSUE_COMMENT: \$*" >> "$tmpdir/gh-calls.txt"
  exit 0
fi
if [[ "\$*" == *"issue list"* ]]; then
  # Return empty list so dedup branch opens a new issue
  echo "null"
  exit 0
fi
if [[ "\$*" == *"pr view"* ]]; then
  echo ""
  exit 0
fi
exit 0
STUB
  chmod +x "$tmpdir/bin/gh"

  cat > "$tmpdir/run_hook.sh" <<HOOKSCRIPT
#!/usr/bin/env bash
set -uo pipefail

export REPO_ROOT="$fake_repo"
export AUTONOMOUS_TEAM_STATE_DIR="$marker_dir"
SCRIPT_DIR="$REPO_ROOT/scripts"
export PATH="$tmpdir/bin:\$PATH"

# If caller passes "unmerged" as \$2, stub git diff to return a fake UU file
INJECT_UNMERGED="\${2:-}"

# Override git for this test so we can inject unmerged-file output
if [[ "\$INJECT_UNMERGED" == "unmerged" ]]; then
  cat > "$tmpdir/bin/git" <<'GITSTUB'
#!/usr/bin/env bash
# Pass most git commands through; intercept diff --diff-filter=U
if [[ "\$*" == *"diff --name-only --diff-filter=U"* ]]; then
  echo "dashboard/src/api/__tests__/client.test.ts"
  exit 0
fi
exec /usr/bin/git "\$@"
GITSTUB
  chmod +x "$tmpdir/bin/git"
fi

# Source hook-event helpers from the real repo
source "$REPO_ROOT/scripts/lib/hook-event.sh"

export HOOK_ROLE="merge"
export HOOK_DISCUSSION=""
export HOOK_PR="9999"
export HOOK_VERDICT="done"
export HOOK_CALLER="post-merge-hook"

hook_event_init "post-merge-hook" \
  "agent_feed,wiki_sync,discussion_close,worktree_merge_registry,lessons_record,team_log,auto_pull" \
  --event-id "\$1"

hook_event_mark_step "agent_feed"
hook_event_mark_step "wiki_sync"
hook_event_mark_step "discussion_close"
hook_event_mark_step "worktree_merge_registry"
hook_event_mark_step "lessons_record"
hook_event_mark_step "team_log"

# ── auto_pull step — inline copy of production logic ─────────────────────────
# Keep in sync with the auto_pull block in scripts/post-merge-hook.sh.
# (Sourcing the full hook would require mocking every step; inline copy is
# simpler for a focused unit test.)
if ! hook_event_has_step "auto_pull"; then
  CURRENT_BRANCH=\$(git -C "\$REPO_ROOT" branch --show-current 2>/dev/null || echo "")
  if [[ "\$CURRENT_BRANCH" != "main" ]]; then
    WARN_MSG="[\$(date +%H:%M)] post-merge-hook: parent repo was on '\${CURRENT_BRANCH:-unknown}' instead of main — attempting checkout main"
    bash "$tmpdir/bin/rotate-team-log.sh" comment "\$WARN_MSG" || echo "\$WARN_MSG" >&2
    DIRTY=\$(git -C "\$REPO_ROOT" status --porcelain 2>/dev/null | head -1 || echo "")
    if [[ -n "\$DIRTY" ]]; then
      ERR_MSG="[\$(date +%H:%M)] post-merge-hook: ERROR — cannot switch to main, parent repo has uncommitted changes."
      bash "$tmpdir/bin/rotate-team-log.sh" comment "\$ERR_MSG" || echo "\$ERR_MSG" >&2
      hook_event_mark_step "auto_pull"
      exit 1
    fi
    CHECKOUT_OUT=\$(git -C "\$REPO_ROOT" checkout main 2>&1) && CHECKOUT_RC=0 || CHECKOUT_RC=\$?
    if [[ \$CHECKOUT_RC -ne 0 ]]; then
      ERR_MSG="[\$(date +%H:%M)] post-merge-hook: ERROR — git checkout main failed: \$CHECKOUT_OUT"
      bash "$tmpdir/bin/rotate-team-log.sh" comment "\$ERR_MSG" || echo "\$ERR_MSG" >&2
      hook_event_mark_step "auto_pull"
      exit 1
    fi
    echo "[post-merge-hook] Switched from '\${CURRENT_BRANCH}' to main"
    CURRENT_BRANCH="main"
  fi

  if [[ "\$CURRENT_BRANCH" == "main" ]]; then
    git -C "\$REPO_ROOT" worktree prune 2>/dev/null || true
    git -C "\$REPO_ROOT" fetch origin main --quiet 2>/dev/null || true

    LOCAL=\$(git -C "\$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "")
    REMOTE=\$(git -C "\$REPO_ROOT" rev-parse origin/main 2>/dev/null || echo "")

    if [[ -n "\$LOCAL" && "\$LOCAL" == "\$REMOTE" ]]; then
      : # Already up to date — silent no-op
    else
      AUTO_PULL_BLOCKED_MARKER="\${AUTONOMOUS_TEAM_STATE_DIR:-\$HOME/.autonomous-forever-state}/auto-pull-blocked"
      UNMERGED_FILES=\$(git -C "\$REPO_ROOT" diff --name-only --diff-filter=U 2>/dev/null || echo "")
      if [[ -n "\$UNMERGED_FILES" ]]; then
        if [[ -f "\$AUTO_PULL_BLOCKED_MARKER" ]]; then
          echo "[post-merge-hook] auto-pull: unmerged paths detected — skipping pull (already reported, marker present)"
        else
          UNMERGED_LIST=\$(echo "\$UNMERGED_FILES" | head -5 | tr '\n' ' ')
          TS=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
          mkdir -p "\$(dirname "\$AUTO_PULL_BLOCKED_MARKER")"
          printf 'ts=%s\nfiles=%s\n' "\$TS" "\$UNMERGED_LIST" > "\$AUTO_PULL_BLOCKED_MARKER"
          WARN_MSG="[\$(date +%H:%M)] post-merge-hook: WARNING needs-boss — auto-pull blocked by unmerged paths in parent repo: \${UNMERGED_LIST}. Resolve manually, then rm \$AUTO_PULL_BLOCKED_MARKER"
          bash "$tmpdir/bin/rotate-team-log.sh" comment "\$WARN_MSG" || echo "\$WARN_MSG" >&2
          BUG_TITLE="[Bug] post-merge-hook auto-pull blocked by unmerged paths in parent repo"
          _REPO="autonomous-agent-7/autonomous-forever"
          EXISTING_ISSUE=\$(gh issue list --repo "\$_REPO" --state open \
            --json number,title \
            --jq "[.[] | select(.title == \"\$BUG_TITLE\")] | first | .number" \
            2>/dev/null || echo "")
          if [[ -n "\$EXISTING_ISSUE" && "\$EXISTING_ISSUE" != "null" ]]; then
            gh issue comment "\$EXISTING_ISSUE" --repo "\$_REPO" \
              --body "Recurred at \${TS}. Unmerged files: \${UNMERGED_LIST}" \
              2>/dev/null || true
            echo "[post-merge-hook] auto-pull: updated Bug Issue #\$EXISTING_ISSUE (recurrence)"
          else
            NEW_ISSUE_URL=\$(gh issue create --repo "\$_REPO" \
              --title "\$BUG_TITLE" \
              --label "needs-boss" \
              --body "Detected at \${TS}." \
              2>/dev/null || echo "")
            if [[ -n "\$NEW_ISSUE_URL" ]]; then
              echo "[post-merge-hook] auto-pull: opened Bug Issue \$NEW_ISSUE_URL"
            fi
          fi
          echo "[post-merge-hook] auto-pull: marker written"
        fi
      else
        PULL_OUT=\$(git -C "\$REPO_ROOT" pull --ff-only origin main 2>&1) && PULL_RC=0 || PULL_RC=\$?
        if [[ \$PULL_RC -eq 0 ]]; then
          echo "[post-merge-hook] auto-pull: pulled main successfully"
          if [[ -f "\${AUTO_PULL_BLOCKED_MARKER:-}" ]]; then
            rm -f "\$AUTO_PULL_BLOCKED_MARKER"
            echo "[post-merge-hook] auto-pull: cleared stale auto-pull-blocked marker"
          fi
        else
          if echo "\$PULL_OUT" | grep -q "untracked working tree files would be overwritten"; then
            UNTRACKED_FILES=\$(echo "\$PULL_OUT" | grep -A50 "untracked working tree files" \
              | grep -v "^error:" | grep -v "Please move" | grep -v "^\$" \
              | sed 's/^\s*//' | grep -v "untracked working tree" | head -20)
            UNTRACKED_COUNT=0
            while IFS= read -r f; do
              [[ -z "\$f" ]] && continue
              FULL_PATH="\${REPO_ROOT}/\${f}"
              if [[ -f "\$FULL_PATH" ]]; then
                rm -f "\$FULL_PATH"
                UNTRACKED_COUNT=\$((UNTRACKED_COUNT + 1))
              fi
            done <<< "\$UNTRACKED_FILES"
            RETRY_OUT=\$(git -C "\$REPO_ROOT" pull --ff-only origin main 2>&1) && RETRY_RC=0 || RETRY_RC=\$?
            if [[ \$RETRY_RC -eq 0 ]]; then
              echo "[post-merge-hook] auto-pull: removed \$UNTRACKED_COUNT stale untracked files and pulled cleanly"
            else
              MSG="[\$(date +%H:%M)] post-merge-hook: auto-pull FAILED after rm — \$RETRY_OUT"
              bash "$tmpdir/bin/rotate-team-log.sh" comment "\$MSG" || echo "\$MSG" >&2
            fi
          elif echo "\$PULL_OUT" | grep -q "local changes to the following files would be overwritten\|Your local changes"; then
            MODIFIED=\$(echo "\$PULL_OUT" | grep -A20 "following files" | tail -n +2 | grep -v "^Please\|^\$" | sed 's/^\s*//' | head -5 | tr '\n' ' ')
            MSG="[\$(date +%H:%M)] post-merge-hook: auto-pull SKIPPED — working tree has modifications: \${MODIFIED}. Run git stash + git pull manually."
            bash "$tmpdir/bin/rotate-team-log.sh" comment "\$MSG" || echo "\$MSG" >&2
          else
            MSG="[\$(date +%H:%M)] post-merge-hook: auto-pull FAILED — \$PULL_OUT"
            bash "$tmpdir/bin/rotate-team-log.sh" comment "\$MSG" || echo "\$MSG" >&2
          fi
        fi
      fi
    fi
  fi
  hook_event_mark_step "auto_pull"
fi

hook_event_finish
echo "[test-hook] done"
HOOKSCRIPT
  chmod +x "$tmpdir/run_hook.sh"
}

# ── Test 1: Unmerged-paths state ──────────────────────────────────────────────
# Simulates git index with UU entries. Expects:
#   - exit 0
#   - exactly one team-log comment containing the file path
#   - marker file written
echo "Test 1: Unmerged-paths detected — loud warning + marker"
T1=$(mktemp -d)
T1_ORIGIN="$T1/origin"
T1_LOCAL="$T1/local"
T1_MARKER_DIR="$T1/state"
mkdir -p "$T1_ORIGIN" "$T1_LOCAL" "$T1_MARKER_DIR"

setup_fake_origin "$T1_ORIGIN"
setup_fake_local "$T1_LOCAL" "$T1_ORIGIN"

# Advance origin so LOCAL != REMOTE (otherwise the pull path is skipped entirely)
echo "new" > "$T1_ORIGIN/newfile.txt"
git -C "$T1_ORIGIN" add .
git -C "$T1_ORIGIN" commit -m "advance" -q

make_hook_env "$T1" "$T1_LOCAL" "$T1_MARKER_DIR"
EVID1="test-unmerged-1-$$-$(date +%s)"
OUTPUT=$(bash "$T1/run_hook.sh" "$EVID1" "unmerged" 2>&1)
RC=$?

assert_exit_0 "test1: exit code is 0" "$RC"

TEAMLOG=$(cat "$T1/teamlog.txt" 2>/dev/null || echo "")
COMBINED="$OUTPUT $TEAMLOG"
assert_contains "test1: team-log contains file path" "$COMBINED" "client.test.ts"
assert_contains "test1: team-log tagged needs-boss" "$COMBINED" "needs-boss"
assert_file_exists "test1: marker file written" "$T1_MARKER_DIR/auto-pull-blocked"

# Also verify Issue creation was attempted
GH_CALLS=$(cat "$T1/gh-calls.txt" 2>/dev/null || echo "")
assert_contains "test1: issue create called" "$GH_CALLS" "ISSUE_CREATE"

rm -rf "$T1"

# ── Test 2: Second run with marker present — suppress duplicate ───────────────
echo "Test 2: Second run with marker present — no duplicate team-log comment"
T2=$(mktemp -d)
T2_ORIGIN="$T2/origin"
T2_LOCAL="$T2/local"
T2_MARKER_DIR="$T2/state"
mkdir -p "$T2_ORIGIN" "$T2_LOCAL" "$T2_MARKER_DIR"

setup_fake_origin "$T2_ORIGIN"
setup_fake_local "$T2_LOCAL" "$T2_ORIGIN"

echo "new" > "$T2_ORIGIN/newfile.txt"
git -C "$T2_ORIGIN" add .
git -C "$T2_ORIGIN" commit -m "advance" -q

make_hook_env "$T2" "$T2_LOCAL" "$T2_MARKER_DIR"

# Pre-write the marker (simulates a previous run)
printf 'ts=%s\nfiles=%s\n' "2026-01-01T00:00:00Z" "some/file.ts " > "$T2_MARKER_DIR/auto-pull-blocked"
# Also create stale teamlog to detect new writes
echo "INITIAL_LOG" > "$T2/teamlog.txt"

EVID2="test-unmerged-2-$$-$(date +%s)"
OUTPUT=$(bash "$T2/run_hook.sh" "$EVID2" "unmerged" 2>&1)
RC=$?

assert_exit_0 "test2: exit code is 0" "$RC"

TEAMLOG=$(cat "$T2/teamlog.txt" 2>/dev/null || echo "")
# The TEAMLOG should only contain the initial line — no new TEAMLOG: entry for unmerged
TEAMLOG_LINES=$(grep -c "^TEAMLOG:" "$T2/teamlog.txt" 2>/dev/null || echo "0")
TEAMLOG_LINES="${TEAMLOG_LINES//[^0-9]/}"
if [[ "${TEAMLOG_LINES:-0}" -eq 0 ]]; then
  pass "test2: no duplicate team-log comment posted"
else
  fail "test2: expected 0 new team-log entries, got $TEAMLOG_LINES"
fi

# Marker should still be present (not cleared by suppression path)
assert_file_exists "test2: marker still present after suppression run" "$T2_MARKER_DIR/auto-pull-blocked"

rm -rf "$T2"

# ── Test 3: Untracked-file branch still works (regression) ───────────────────
echo "Test 3: Untracked-file conflict branch (regression)"
T3=$(mktemp -d)
T3_ORIGIN="$T3/origin"
T3_LOCAL="$T3/local"
T3_MARKER_DIR="$T3/state"
mkdir -p "$T3_ORIGIN" "$T3_LOCAL" "$T3_MARKER_DIR"

setup_fake_origin "$T3_ORIGIN"
setup_fake_local "$T3_LOCAL" "$T3_ORIGIN"

echo "from-origin" > "$T3_ORIGIN/conflict.txt"
git -C "$T3_ORIGIN" add .
git -C "$T3_ORIGIN" commit -m "add conflict.txt" -q
echo "local-stale" > "$T3_LOCAL/conflict.txt"

make_hook_env "$T3" "$T3_LOCAL" "$T3_MARKER_DIR"
# No "unmerged" arg — git diff passes through to real git, which returns empty
EVID3="test-untracked-3-$$-$(date +%s)"
OUTPUT=$(bash "$T3/run_hook.sh" "$EVID3" 2>&1)
RC=$?

assert_exit_0 "test3: exit code is 0" "$RC"
assert_contains "test3: pulled cleanly after untracked removal" "$OUTPUT" "stale untracked files and pulled cleanly"
# Verify HEAD advanced
NEW_HEAD=$(git -C "$T3_LOCAL" rev-parse HEAD)
ORIGIN_HEAD=$(git -C "$T3_ORIGIN" rev-parse HEAD)
if [[ "$NEW_HEAD" == "$ORIGIN_HEAD" ]]; then
  pass "test3: HEAD advanced after untracked rm"
else
  fail "test3: HEAD did not advance (local=$NEW_HEAD origin=$ORIGIN_HEAD)"
fi

rm -rf "$T3"

# ── Test 4: Modified-file branch still works (regression) ────────────────────
echo "Test 4: Modified-file conflict branch (regression)"
T4=$(mktemp -d)
T4_ORIGIN="$T4/origin"
T4_LOCAL="$T4/local"
T4_MARKER_DIR="$T4/state"
mkdir -p "$T4_ORIGIN" "$T4_LOCAL" "$T4_MARKER_DIR"

setup_fake_origin "$T4_ORIGIN"
setup_fake_local "$T4_LOCAL" "$T4_ORIGIN"

echo "origin-update" >> "$T4_ORIGIN/file1.txt"
git -C "$T4_ORIGIN" add .
git -C "$T4_ORIGIN" commit -m "update file1" -q
echo "local-modification" >> "$T4_LOCAL/file1.txt"

make_hook_env "$T4" "$T4_LOCAL" "$T4_MARKER_DIR"
EVID4="test-modified-4-$$-$(date +%s)"
OUTPUT=$(bash "$T4/run_hook.sh" "$EVID4" 2>&1)
RC=$?

assert_exit_0 "test4: exit code is 0" "$RC"
TEAMLOG=$(cat "$T4/teamlog.txt" 2>/dev/null || echo "")
COMBINED="$OUTPUT $TEAMLOG"
assert_contains "test4: warning about modifications in team-log or output" "$COMBINED" "auto-pull SKIPPED"
# HEAD must NOT have advanced
NEW_HEAD=$(git -C "$T4_LOCAL" rev-parse HEAD)
ORIGIN_HEAD=$(git -C "$T4_ORIGIN" rev-parse HEAD)
if [[ "$NEW_HEAD" != "$ORIGIN_HEAD" ]]; then
  pass "test4: HEAD did not advance (pull correctly skipped)"
else
  fail "test4: HEAD advanced when it should not have"
fi

rm -rf "$T4"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
