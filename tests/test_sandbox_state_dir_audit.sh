#!/usr/bin/env bash
# tests/test_sandbox_state_dir_audit.sh
#
# D#2447: five distinct call sites append to <state_dir>/audit.jsonl —
# _write_gh_api_mutation_allow_event, _write_archive_protocol_warning_event,
# _write_head_flip_warning_event, and _write_unclassified_command_event in
# hooks/sandbox.py, plus record_payload_shape in hooks/payload_shape.py
# (called unconditionally from hooks/sandbox.py's main(), once per distinct
# payload key-set). All five used to fall back through Path.home(), which
# reroutes to the operator's real production state dir
# (~/.autonomous-forever-state) whenever AUTONOMOUS_TEAM_STATE_DIR is unset,
# including when HOME itself is unset (the passwd-database fallback).
#
# This suite proves the FIX from the scratch side only:
#   1. repo_root_fixture_run_hook (tests/lib/repo-root-fixture.sh) makes a
#      scratch AUTONOMOUS_TEAM_STATE_DIR the default when a caller forgets
#      to export one, and a row driven through it lands in that scratch
#      dir — not in ~/.autonomous-forever-state.
#   2. All five write sites still append their row on a genuine run
#      (positive proof — required because every one of them swallows every
#      exception, so a silently-broken write and a correctly-suppressed one
#      look identical from the outside; only a positive assertion tells
#      them apart).
#
# What this suite deliberately does NOT do: assert anything about
# ~/.autonomous-forever-state/audit.jsonl. That real-production proof (the
# HOME-unset / AUTONOMOUS_TEAM_STATE_DIR-unset "no leak" case, for all five
# sites) was verified manually against the real file as Gate 2 evidence for
# this PR, once — scripts/check-tests-live-state-paths.sh forbids any
# *committed* test from referencing or defaulting to that path (D#2447
# acceptance item 9), and a suite that re-ran that assertion on every CI run
# would be doing exactly what it is meant to prevent: routinely touching the
# real file.
#
# record_payload_shape writes at most once per distinct payload key-set per
# process tree (a marker file dedups it) — every payload below shares the
# same three keys (cwd, tool_input, tool_name), so its row appears on
# whichever call runs first and is NOT repeated on the later ones. That is
# existing, unrelated behaviour; this suite accounts for it rather than
# re-testing it.
#
# Usage: bash tests/test_sandbox_state_dir_audit.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source "$REPO_ROOT/tests/lib/repo-root-fixture.sh"
FIXTURE_ROOT="$(repo_root_fixture_make "$REPO_ROOT")" || {
  echo "FAIL: could not create isolated repo-root fixture" >&2
  exit 1
}
HOOK="$FIXTURE_ROOT/hooks/sandbox.py"
MAIN_REPO="$FIXTURE_ROOT"
WT_CLAUDE="$FIXTURE_ROOT/.claude/worktrees/testid123"

# Scratch dir for this suite's own stderr captures (D#2254 — a shared fixed
# /tmp filename could race with a concurrent run of this same suite).
RUN_TMP="$(mktemp -d /tmp/test_sandbox_state_dir_audit.XXXXXX)"

PASS=0
FAIL=0
CLEANUP_DIRS=("$FIXTURE_ROOT" "$RUN_TMP")
trap 'rm -rf "${CLEANUP_DIRS[@]}"' EXIT

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

build_payload() {
  # build_payload <command> <cwd>
  # Command text comes in on stdin, not argv — the oversize-span payload
  # (site 4) is well past ARG_MAX as a command-line argument.
  printf '%s' "$1" | python3 -c '
import json, sys
command = sys.stdin.read()
print(json.dumps({
    "tool_name": "Bash",
    "tool_input": {"command": command},
    "cwd": sys.argv[1],
}))
' "$2"
}

run_and_check() {
  # run_and_check <label> <payload_json> <expected_kinds_csv>
  # Runs the payload through repo_root_fixture_run_hook with
  # AUTONOMOUS_TEAM_STATE_DIR unset beforehand (so the helper defaults it to
  # a fresh scratch dir), then asserts every row that landed there has one
  # of the expected kinds and nothing landed anywhere else.
  local label="$1" payload="$2" expected_kinds="$3"

  unset AUTONOMOUS_TEAM_STATE_DIR || true
  repo_root_fixture_run_hook "$HOOK" "$payload" >/dev/null 2>"$RUN_TMP/stderr"
  local hook_exit="$REPO_ROOT_FIXTURE_HOOK_EXIT"

  if [[ "$hook_exit" -eq 0 ]]; then
    pass "$label: hook exits 0 through repo_root_fixture_run_hook"
  else
    fail "$label: expected exit 0, got $hook_exit"
  fi

  if [[ -z "${AUTONOMOUS_TEAM_STATE_DIR:-}" ]]; then
    fail "$label: repo_root_fixture_run_hook did not default AUTONOMOUS_TEAM_STATE_DIR"
    return
  fi
  CLEANUP_DIRS+=("$AUTONOMOUS_TEAM_STATE_DIR")

  if [[ ! -f "$AUTONOMOUS_TEAM_STATE_DIR/audit.jsonl" ]]; then
    fail "$label: no audit.jsonl written to scratch dir at all"
    return
  fi

  local kinds
  kinds="$(python3 -c '
import json, sys
with open(sys.argv[1]) as fh:
    for line in fh:
        line = line.strip()
        if line:
            print(json.loads(line)["kind"])
' "$AUTONOMOUS_TEAM_STATE_DIR/audit.jsonl" | sort | tr "\n" "," )"

  local expected_sorted
  expected_sorted="$(echo "$expected_kinds" | tr "," "\n" | sort | tr "\n" "," )"

  if [[ "$kinds" == "$expected_sorted" ]]; then
    pass "$label: scratch dir got exactly {$expected_kinds}"
  else
    fail "$label: scratch dir got {$kinds}, expected {$expected_sorted}"
  fi
}

# ---------------------------------------------------------------------------
# site1: allowlisted gh api graphql mutation, worktree cwd.
# First call in this fixture, so record_payload_shape (site 5) also fires.
# ---------------------------------------------------------------------------
MUTATION_CMD="gh api graphql -f query='mutation { addDiscussionComment(input: {discussionId: \"D_x\", body: \"hi\"}) { comment { id } } }'"
run_and_check "site1 (gh mutation)" "$(build_payload "$MUTATION_CMD" "$WT_CLAUDE")" \
  "sandbox_allow_graphql_mutation,payload_shape"

# ---------------------------------------------------------------------------
# site2: real git rm, main-checkout (team_lead) cwd.
# payload_shape already deduped from site1 (identical key shape).
# ---------------------------------------------------------------------------
GIT_RM_CMD="git rm CLAUDE.md"
run_and_check "site2 (git rm)" "$(build_payload "$GIT_RM_CMD" "$MAIN_REPO")" \
  "archive_protocol_warning"

# ---------------------------------------------------------------------------
# site3: head-flipping git invocation, main-checkout (team_lead) cwd.
# ---------------------------------------------------------------------------
RESET_CMD="git reset --hard"
run_and_check "site3 (head flip)" "$(build_payload "$RESET_CMD" "$MAIN_REPO")" \
  "head_flip_warning"

# ---------------------------------------------------------------------------
# site4: oversize quoted region, non-team-lead (worktree) cwd.
# ---------------------------------------------------------------------------
LONG_QUOTED="$(python3 -c 'print("a " * 70000)')"
OVERSIZE_CMD="echo '$LONG_QUOTED'"
run_and_check "site4 (oversize span)" "$(build_payload "$OVERSIZE_CMD" "$WT_CLAUDE")" \
  "unclassified_oversize_command"

# ---------------------------------------------------------------------------
# Test: a caller-provided AUTONOMOUS_TEAM_STATE_DIR is respected, not
# overridden — the env var stays the primary override (Spec item 11).
# ---------------------------------------------------------------------------
CALLER_SCRATCH="$(mktemp -d)"
CLEANUP_DIRS+=("$CALLER_SCRATCH")
export AUTONOMOUS_TEAM_STATE_DIR="$CALLER_SCRATCH"

repo_root_fixture_run_hook "$HOOK" "$(build_payload "$MUTATION_CMD" "$WT_CLAUDE")" >/dev/null 2>&1

if [[ "$AUTONOMOUS_TEAM_STATE_DIR" == "$CALLER_SCRATCH" ]]; then
  pass "caller-exported AUTONOMOUS_TEAM_STATE_DIR was left alone"
else
  fail "caller-exported AUTONOMOUS_TEAM_STATE_DIR was overwritten"
fi
if [[ -f "$CALLER_SCRATCH/audit.jsonl" ]]; then
  pass "row landed in the caller's own scratch dir"
else
  fail "no row landed in the caller-provided scratch dir"
fi

unset AUTONOMOUS_TEAM_STATE_DIR || true

echo ""
echo "======================================"
echo "Results: $PASS passed, $FAIL failed"
echo "======================================"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
