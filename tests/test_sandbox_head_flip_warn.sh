#!/usr/bin/env bash
# tests/test_sandbox_head_flip_warn.sh
#
# D#2324 — HEAD-flip verbs are OBSERVED at team_lead tier, never blocked.
#
# This suite drives the real hooks/sandbox.py process with a real payload on
# stdin and reads the real <state_dir>/audit.jsonl it writes. Asserting on
# is_head_flipping_git_invocation() alone would prove nothing about the branch
# the predicate sits in — the whole defect behind D#2324 was that the
# team_lead arm short-circuits to allow before any git rule runs, and only an
# end-to-end run exercises that arm.
#
# Two things are being measured, and they are different:
#   1. the WARN cases still exit 0 (this change takes away no command), and
#      each writes exactly one head_flip_warning row;
#   2. routine Team Lead git traffic writes NO row (the Spec's stated failure
#      condition is audit rows on `git worktree` / `git branch` / `git status`).
#
# Usage: bash tests/test_sandbox_head_flip_warn.sh
# Exit 0 = all tests passed; non-zero = at least one failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Same isolation as tests/test_sandbox_hook.sh (D#2267): hooks/sandbox.py
# anchors its daily hook-events file to Path(__file__).parent.parent with no
# env override, so the suite must run against a materialised copy of hooks/
# rooted elsewhere or it appends to the LIVE telemetry file.
source "$REPO_ROOT/tests/lib/repo-root-fixture.sh"
FIXTURE_ROOT="$(repo_root_fixture_make "$REPO_ROOT")" || {
  echo "FAIL: could not create isolated repo-root fixture" >&2
  exit 1
}
HOOK="$FIXTURE_ROOT/hooks/sandbox.py"
MAIN_REPO="$FIXTURE_ROOT"
WORKTREE="$FIXTURE_ROOT/.claude/worktrees/testid123"
mkdir -p "$WORKTREE"

RUN_TMP="$(mktemp -d /tmp/test_sandbox_head_flip.XXXXXX)"
trap 'rm -rf "$RUN_TMP" "$FIXTURE_ROOT"' EXIT

# The audit row lands in <state_dir>/audit.jsonl. That file is append-only in
# production and has no cleanup path, so point the hook at a scratch state dir
# for the whole suite (CLAUDE.md, "AUTONOMOUS_TEAM_STATE_DIR in tests").
export AUTONOMOUS_TEAM_STATE_DIR="$RUN_TMP/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"
AUDIT="$AUTONOMOUS_TEAM_STATE_DIR/audit.jsonl"

PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

payload() {  # <cwd> <command> -> a real PreToolUse Bash payload on stdout
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[2]},"cwd":sys.argv[1]}))' "$1" "$2"
}

run_hook() {  # <cwd> <command>; sets HOOK_EXIT, HOOK_STDERR, ROWS
  local json
  json="$(payload "$1" "$2")"
  : > "$AUDIT"
  HOOK_EXIT=0
  printf '%s\n' "$json" | python3 "$HOOK" >/dev/null 2>"$RUN_TMP/stderr" || HOOK_EXIT=$?
  HOOK_STDERR="$(cat "$RUN_TMP/stderr" 2>/dev/null || true)"
  ROWS="$(grep -c '"kind": "head_flip_warning"' "$AUDIT" 2>/dev/null || true)"
  ROWS="${ROWS:-0}"
}

ok()   { echo "PASS: $1"; PASS=$((PASS + 1)); }
bad()  { echo "FAIL: $1 — $2"; FAIL=$((FAIL + 1)); }

# Acceptance 5 + 6: warn, audit, and STILL RUN.
expect_warn() {  # <name> <command>
  run_hook "$MAIN_REPO" "$2"
  if [[ "$HOOK_EXIT" -ne 0 ]]; then
    bad "$1" "expected exit 0 (this observes, it must not block), got $HOOK_EXIT"
    return
  fi
  if [[ "$ROWS" -ne 1 ]]; then
    bad "$1" "expected exactly 1 head_flip_warning row in audit.jsonl, got $ROWS"
    return
  fi
  if [[ "$HOOK_STDERR" != *"moves HEAD or the main ref"* ]]; then
    bad "$1" "expected a stderr warning, got: $HOOK_STDERR"
    return
  fi
  ok "$1"
}

# Acceptance 7: routine traffic stays silent.
expect_silent() {  # <name> <command>
  run_hook "$MAIN_REPO" "$2"
  if [[ "$HOOK_EXIT" -ne 0 ]]; then
    bad "$1" "expected exit 0, got $HOOK_EXIT"
    return
  fi
  if [[ "$ROWS" -ne 0 ]]; then
    bad "$1" "expected NO head_flip_warning row, got $ROWS"
    return
  fi
  ok "$1"
}

# ---------------------------------------------------------------------------
# Acceptance 5 — the incident command itself
# ---------------------------------------------------------------------------

expect_warn "reset --hard FETCH_HEAD (the D#2324 command) warns and runs" \
  "git reset --hard FETCH_HEAD"

# ---------------------------------------------------------------------------
# Acceptance 6 — the rest of the narrow list
# ---------------------------------------------------------------------------

expect_warn "checkout -B tmp FETCH_HEAD"        "git checkout -B tmp FETCH_HEAD"
expect_warn "checkout -f tmp"                   "git checkout -f tmp"
expect_warn "merge origin/foo"                  "git merge origin/foo"
expect_warn "push --force origin main"          "git push --force origin main"

# Same four shapes reached the way they actually get typed — behind a `cd`,
# behind a `;`, and via `git -C`. The predicate reuses the module's shared
# tokenise+walk layer precisely so these are not separate cases in the code.
expect_warn "reset --hard behind a cd"          "cd /some/where && git reset --hard HEAD~1"
expect_warn "merge behind a semicolon"          "git fetch origin; git merge origin/main"
expect_warn "checkout -f via git -C"            "git -C /some/where checkout -f tmp"
expect_warn "push HEAD:main refspec"            "git push origin HEAD:main"
expect_warn "push refs/heads/main"              "git push origin refs/heads/main"

# ---------------------------------------------------------------------------
# Acceptance 7 — routine Team Lead traffic writes no row
# ---------------------------------------------------------------------------

expect_silent "worktree remove --force"         "git worktree remove x --force"
expect_silent "branch --list"                   "git branch --list"
expect_silent "status"                          "git status"
expect_silent "reset --soft HEAD~1"             "git reset --soft HEAD~1"
expect_silent "checkout -- path/to/file"        "git checkout -- path/to/file"
expect_silent "push to a feature branch"        "git push origin some-feature-branch"

# The near-misses that would show the predicate had re-broadened.
expect_silent "merge-base is not merge"         "git merge-base HEAD origin/main"
expect_silent "checkout -b creates, not flips"  "git checkout -b new-branch"
expect_silent "bare reset"                      "git reset"
expect_silent "push to main-feature"            "git push origin main-feature"
expect_silent "reset --help is documentation"   "git reset --hard --help"
expect_silent "a quoted mention is not a run"   "echo 'never run git reset --hard here'"
expect_silent "not a git command at all"        "gh pr merge 42 --squash"

# ---------------------------------------------------------------------------
# Acceptance 8 — worktree-tier blocking behaviour is unchanged
# ---------------------------------------------------------------------------

run_hook "$WORKTREE" "git reset --hard HEAD~1"
if [[ "$HOOK_EXIT" -eq 0 ]]; then
  bad "worktree tier still blocks reset --hard" "expected a non-zero exit, got 0"
elif [[ "$HOOK_STDERR" != *"blocked by sandbox: git write-verb outside worktree"* ]]; then
  bad "worktree tier still blocks reset --hard" \
      "reason string changed; got: $HOOK_STDERR"
elif [[ "$ROWS" -ne 0 ]]; then
  bad "worktree tier still blocks reset --hard" \
      "a blocked worktree command must not write a team_lead warn row, got $ROWS"
else
  ok "worktree tier still blocks reset --hard, same reason string, no warn row"
fi

# ---------------------------------------------------------------------------
# The archive-protocol warning it sits beside is untouched, and a command that
# is both writes both rows.
# ---------------------------------------------------------------------------

run_hook "$MAIN_REPO" "git rm tracked.py"
ARCHIVE_ROWS="$(grep -c '"kind": "archive_protocol_warning"' "$AUDIT" 2>/dev/null || true)"
if [[ "$HOOK_EXIT" -eq 0 && "${ARCHIVE_ROWS:-0}" -eq 1 && "$ROWS" -eq 0 ]]; then
  ok "git rm still writes its archive_protocol_warning and no head_flip row"
else
  bad "git rm still writes its archive_protocol_warning" \
      "exit=$HOOK_EXIT archive_rows=${ARCHIVE_ROWS:-0} head_flip_rows=$ROWS"
fi

run_hook "$MAIN_REPO" "git rm old.py && git merge origin/main"
ARCHIVE_ROWS="$(grep -c '"kind": "archive_protocol_warning"' "$AUDIT" 2>/dev/null || true)"
if [[ "$HOOK_EXIT" -eq 0 && "${ARCHIVE_ROWS:-0}" -eq 1 && "$ROWS" -eq 1 ]]; then
  ok "a command that is both writes both rows and still runs"
else
  bad "a command that is both writes both rows" \
      "exit=$HOOK_EXIT archive_rows=${ARCHIVE_ROWS:-0} head_flip_rows=$ROWS"
fi

# ---------------------------------------------------------------------------
# The row is also durable in the daily hook-events file, not only audit.jsonl.
# ---------------------------------------------------------------------------

BLOCKS_FILE="$FIXTURE_ROOT/.autonomous-team/hook-events/blocks-$(date +%F).jsonl"
run_hook "$MAIN_REPO" "git reset --hard FETCH_HEAD"
if [[ -f "$BLOCKS_FILE" ]] && grep -q '"kind": "head_flip_warning"' "$BLOCKS_FILE"; then
  ok "head_flip_warning is also written to the daily hook-events file"
else
  bad "head_flip_warning is also written to the daily hook-events file" \
      "no matching row in $BLOCKS_FILE"
fi

# ---------------------------------------------------------------------------

echo
echo "test_sandbox_head_flip_warn.sh: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
