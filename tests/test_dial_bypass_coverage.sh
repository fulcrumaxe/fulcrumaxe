#!/usr/bin/env bash
# tests/test_dial_bypass_coverage.sh
#
# Red-team tests: verify that all 3 dial bypass paths are closed.
#
# Test A: Agent() invocation from a worktree path → blocked.
# Test B: gh api -X POST from worktree → blocked.
# Test C: any mutation from /tmp/random/ (untrusted cwd) → blocked.
# Test D: same mutations from the main repo root (team_lead) → allowed.
#
# Each test also verifies the appropriate sandbox_block_* audit row appears
# in today's blocks-*.jsonl file.
#
# Usage: bash tests/test_dial_bypass_coverage.sh
# Exit 0 = all tests passed, non-zero = at least one failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# D#2267: hooks/sandbox.py's telemetry dir is anchored to wherever the
# invoked *file* lives, with no env override — invoking the real
# $REPO_ROOT/hooks/sandbox.py would append every block this suite generates
# to the LIVE .autonomous-team/hook-events/blocks-<date>.jsonl. Run against
# an isolated fixture copy instead. See tests/lib/repo-root-fixture.sh.
source "$REPO_ROOT/tests/lib/repo-root-fixture.sh"
FIXTURE_ROOT="$(repo_root_fixture_make "$REPO_ROOT")" || {
  echo "FAIL: could not create isolated repo-root fixture" >&2
  exit 1
}
trap 'rm -rf "$FIXTURE_ROOT"' EXIT

HOOK="$FIXTURE_ROOT/hooks/sandbox.py"
BLOCKS_FILE="$FIXTURE_ROOT/.autonomous-team/hook-events/blocks-$(date +%Y-%m-%d).jsonl"

# MAIN_REPO_ROOT is the fixture root, not the real checkout: the invoked
# hook copy (under $FIXTURE_ROOT/hooks/) derives its own "main repo root"
# from its own __file__ via hooks/repo_root.py, and that derivation is
# confident because repo_root_fixture_make() gave the fixture a real .git.
MAIN_REPO_ROOT="$FIXTURE_ROOT"

WT_CWD="$MAIN_REPO_ROOT/.claude/worktrees/test-redteam-99999"
TL_CWD="$MAIN_REPO_ROOT"
UNTRUSTED_CWD="/tmp/random-redteam-$$"

PASS=0
FAIL=0

green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }

run_hook() {
  # Usage: run_hook <json_payload>
  # Returns the exit code of the hook.
  local payload="$1"
  echo "$payload" | python3 "$HOOK"
  return $?
}

# _audit_kind_since <blocks_file> <line_offset> <expected_kind>
#
# Parses every line appended to <blocks_file> AFTER <line_offset> (i.e. rows
# the call under test could plausibly have written — D#2175 item 6) as JSON
# and checks whether any of them has a "kind" field equal to <expected_kind>.
#
# Parses with json.loads rather than grepping a literal, so it does not care
# whether the writer's separator is "kind": "X" or "kind":"X" (item 5).
#
# Prints "MATCH" and exits 0 on a match. On no match, prints a comma-separated
# list of the kinds actually found among the new rows (or "none" if there
# were no new rows / none parsed) and exits 1. A missing blocks file is
# treated as zero lines, not an error — hooks/sandbox.py never samples block
# writes ("Allow decisions are sampled at 10%; blocks are always written."),
# so absence of a matching row is always a genuine failure, never a sampling
# artifact (item 4).
_audit_kind_since() {
  local blocks_file="$1"
  local line_offset="$2"
  local expected_kind="$3"

  python3 - "$blocks_file" "$line_offset" "$expected_kind" <<'PYEOF'
import json
import sys

blocks_file, line_offset, expected_kind = sys.argv[1], int(sys.argv[2]), sys.argv[3]

try:
    with open(blocks_file) as f:
        lines = f.readlines()
except FileNotFoundError:
    lines = []

kinds_seen = []
for line in lines[line_offset:]:
    line = line.strip()
    if not line:
        continue
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        continue
    kind = row.get("kind")
    kinds_seen.append(kind)
    if kind == expected_kind:
        print("MATCH")
        sys.exit(0)

print(",".join(str(k) for k in kinds_seen) if kinds_seen else "none")
sys.exit(1)
PYEOF
}

assert_blocked() {
  local test_name="$1"
  local payload="$2"
  local expected_kind="${3:-}"

  # Record the blocks file's line count BEFORE invoking the hook, so a kind
  # check below only considers rows this call actually appended — not a
  # stale row already sitting in the shared, append-only day log (item 6).
  local line_offset=0
  if [ -f "$BLOCKS_FILE" ]; then
    line_offset=$(wc -l < "$BLOCKS_FILE" | tr -d ' ')
  fi

  local exit_code=0
  local stderr_out
  stderr_out=$(echo "$payload" | python3 "$HOOK" 2>&1 >/dev/null) || exit_code=$?

  if [ "$exit_code" -eq 2 ]; then
    if [ -n "$expected_kind" ]; then
      local actual_kinds
      if actual_kinds=$(_audit_kind_since "$BLOCKS_FILE" "$line_offset" "$expected_kind"); then
        green "PASS $test_name (exit=2, audit_kind=$expected_kind found)"
        PASS=$((PASS + 1))
      else
        red "FAIL $test_name — expected audit kind '$expected_kind', got '$actual_kinds'"
        FAIL=$((FAIL + 1))
      fi
    else
      green "PASS $test_name (exit=2, blocked as expected)"
      PASS=$((PASS + 1))
    fi
  else
    red "FAIL $test_name — expected exit 2 (blocked), got exit $exit_code"
    info "stderr: $stderr_out"
    FAIL=$((FAIL + 1))
  fi
}

assert_allowed() {
  local test_name="$1"
  local payload="$2"

  local exit_code=0
  echo "$payload" | python3 "$HOOK" >/dev/null 2>&1 || exit_code=$?

  if [ "$exit_code" -eq 0 ]; then
    green "PASS $test_name (exit=0, allowed as expected)"
    PASS=$((PASS + 1))
  else
    red "FAIL $test_name — expected exit 0 (allowed), got exit $exit_code"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Dial Bypass Coverage Red-Team Tests ==="
echo "Repo root: $REPO_ROOT"
echo "Worktree cwd: $WT_CWD"
echo "Team-lead cwd: $TL_CWD"
echo "Untrusted cwd: $UNTRUSTED_CWD"
echo ""

# ---------------------------------------------------------------------------
# Self-check: prove the kind-matching machinery (_audit_kind_since) can
# itself go red, unconditionally on every run — not behind a flag or an env
# var (D#2175 item 3). This is what stops the soft-pass regressing silently:
# if the machinery below ever loses the ability to detect a mismatch, THIS
# section fails the suite. When the machinery correctly detects a mismatch
# (the expected, working case) that is scored as a PASS here, same as any
# other assertion — it does not itself make the suite exit non-zero.
#
# assert_matcher drives _audit_kind_since directly against constructed
# fixture files rather than the shared blocks log, so these checks are
# deterministic and don't depend on what else has written to today's file.
# ---------------------------------------------------------------------------
echo "--- Self-check: kind-matching machinery can go red ---"

SELFCHECK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/dial-bypass-selfcheck.XXXXXX")
# Bash traps replace rather than stack — re-declare to also clean up
# $FIXTURE_ROOT (the D#2267 repo-root fixture registered near the top of
# this file) instead of silently dropping that cleanup.
trap 'rm -rf "$SELFCHECK_DIR" "$FIXTURE_ROOT"' EXIT

# assert_matcher <test_name> <expect: match|nomatch> <blocks_file> <line_offset> <expected_kind>
assert_matcher() {
  local test_name="$1" expect="$2" blocks_file="$3" line_offset="$4" expected_kind="$5"

  local actual_kinds result
  if actual_kinds=$(_audit_kind_since "$blocks_file" "$line_offset" "$expected_kind"); then
    result="match"
  else
    result="nomatch"
  fi

  if [ "$result" = "$expect" ]; then
    green "PASS $test_name ($result, as expected)"
    PASS=$((PASS + 1))
  else
    red "FAIL $test_name — expected result '$expect', got '$result' (kinds seen: $actual_kinds)"
    FAIL=$((FAIL + 1))
  fi
}

# S1 (item 3): a deliberately wrong expected_kind against a real row must
# fail to match — proves a mismatch can go red.
S1_FILE="$SELFCHECK_DIR/s1.jsonl"
printf '%s\n' '{"ts": "2026-08-23T00:00:00Z", "kind": "sandbox_block_agent_spawn"}' > "$S1_FILE"
assert_matcher "S1: deliberately wrong expected_kind fails to match" \
  "nomatch" "$S1_FILE" 0 "sandbox_block_definitely_not_this_kind"

# S2 (item 4): an empty/nonexistent blocks file — no row for the call at
# all — must fail to match, not pass.
S2_FILE="$SELFCHECK_DIR/does-not-exist.jsonl"
assert_matcher "S2: absent blocks file fails to match" \
  "nomatch" "$S2_FILE" 0 "sandbox_block_agent_spawn"

# S3 (item 5): both the with-space separator the writer actually emits and
# the no-space separator the old buggy grep looked for must match — the
# matching logic must not depend on separator spelling either way.
S3_FILE="$SELFCHECK_DIR/s3.jsonl"
printf '%s\n' '{"ts": "2026-08-23T00:00:00Z", "kind": "sandbox_block_gh_api_mutation"}' > "$S3_FILE"
assert_matcher "S3a: with-space separator (as the writer emits) matches" \
  "match" "$S3_FILE" 0 "sandbox_block_gh_api_mutation"
S3B_FILE="$SELFCHECK_DIR/s3b.jsonl"
printf '%s\n' '{"ts":"2026-08-23T00:00:00Z","kind":"sandbox_block_gh_api_mutation"}' > "$S3B_FILE"
assert_matcher "S3b: no-space separator also matches" \
  "match" "$S3B_FILE" 0 "sandbox_block_gh_api_mutation"

# S4 (item 6): a stale row bearing the expected kind, already present BEFORE
# the offset, must not by itself satisfy the assertion — only rows appended
# after the call's line-count snapshot count. Pre-seed with the expected
# kind, take the offset, then append a DIFFERENT kind (simulating the call
# under test firing a different guard) and require nomatch.
S4_FILE="$SELFCHECK_DIR/s4.jsonl"
printf '%s\n' '{"ts": "2026-08-23T00:00:00Z", "kind": "sandbox_block_gh_api_mutation"}' > "$S4_FILE"
S4_OFFSET=$(wc -l < "$S4_FILE" | tr -d ' ')
printf '%s\n' '{"ts": "2026-08-23T00:00:01Z", "kind": "sandbox_block_agent_spawn"}' >> "$S4_FILE"
assert_matcher "S4: stale pre-existing row of the expected kind does not satisfy the call under test" \
  "nomatch" "$S4_FILE" "$S4_OFFSET" "sandbox_block_gh_api_mutation"

# ---------------------------------------------------------------------------
# Test A: Agent() from worktree → BLOCKED
# ---------------------------------------------------------------------------
echo "--- Test A: Agent() spawn from worktree ---"

PAYLOAD_A=$(cat <<EOF
{
  "tool_name": "Agent",
  "tool_input": {"prompt": "do something dangerous"},
  "cwd": "$WT_CWD"
}
EOF
)
assert_blocked "A1: Agent() from worktree" "$PAYLOAD_A" "sandbox_block_agent_spawn"

# ---------------------------------------------------------------------------
# Test B: gh api -X POST from worktree → BLOCKED
# ---------------------------------------------------------------------------
echo ""
echo "--- Test B: gh api mutation from worktree ---"

PAYLOAD_B1=$(cat <<EOF
{
  "tool_name": "Bash",
  "tool_input": {"command": "gh api repos/autonomous-agent-7/autonomous-forever/issues -X POST -f title=pwned"},
  "cwd": "$WT_CWD"
}
EOF
)
assert_blocked "B1: gh api -X POST from worktree" "$PAYLOAD_B1" "sandbox_block_gh_api_mutation"

PAYLOAD_B2=$(cat <<EOF
{
  "tool_name": "Bash",
  "tool_input": {"command": "gh api repos/autonomous-agent-7/autonomous-forever/issues/1 -X PATCH -f state=closed"},
  "cwd": "$WT_CWD"
}
EOF
)
assert_blocked "B2: gh api -X PATCH from worktree" "$PAYLOAD_B2" "sandbox_block_gh_api_mutation"

PAYLOAD_B3=$(cat <<EOF
{
  "tool_name": "Bash",
  "tool_input": {"command": "gh issue close 42 --repo autonomous-agent-7/autonomous-forever"},
  "cwd": "$WT_CWD"
}
EOF
)
assert_blocked "B3: gh issue close from worktree" "$PAYLOAD_B3"

# B4a/B4b: graphql mutation from worktree, split across the allowlist boundary.
# _is_gh_api_mutation() matches `gh api graphql ... mutation` regardless of how
# the query is passed (-f, --field, or inline) — but a mutation whose name is
# in _GH_API_GRAPHQL_MUTATION_ALLOWLIST (hooks/sandbox_rules.py) is deliberately
# let through even from a worktree, because gh-label.sh, reviewer label flips,
# and PM Spec updates (updateDiscussion) all run addLabelsToLabelable /
# updateDiscussion / addDiscussionComment / removeLabelsFromLabelable from
# worktrees as part of normal, sanctioned agent work. D#1148 (PR #1149) added
# that allowlist the same day this test was first written (PR #1139) — this
# test predated the allowlist and asserted a pre-#1149 world for it. Splitting
# into B4a (allowlisted → allowed) and B4b (non-allowlisted → blocked) covers
# both sides of that boundary, so a future narrowing of the allowlist breaks
# a test here instead of passing silently. D#2167.

# B4a: allowlisted graphql mutation (addLabelsToLabelable) from worktree → ALLOWED.
PAYLOAD_B4A=$(cat <<'EOF'
{
  "tool_name": "Bash",
  "tool_input": {"command": "gh api graphql -f query='mutation { addLabelsToLabelable(input:{labelableId:\"x\",labelIds:[\"y\"]}) { clientMutationId } }'"},
  "cwd": "WORKTREE_CWD_PLACEHOLDER"
}
EOF
)
PAYLOAD_B4A="${PAYLOAD_B4A/WORKTREE_CWD_PLACEHOLDER/$WT_CWD}"
assert_allowed "B4a: allowlisted graphql mutation (addLabelsToLabelable) from worktree" "$PAYLOAD_B4A"

# B4b: non-allowlisted graphql mutation (closeIssue) from worktree → BLOCKED.
# Not mergePullRequest: that name is intercepted one step earlier by
# _is_gh_merge() (step 1b, hooks/sandbox_rules.py ~line 2911, matched via
# _GRAPHQL_MERGE_PATTERN at line 377) before the allowlist logic in
# _is_gh_api_mutation() (step 1c) ever runs — so it would still get blocked
# even if the allowlist check were deleted outright, which doesn't exercise
# the boundary this test exists to cover. closeIssue is neither allowlisted
# nor merge-pattern-matched, so it actually reaches and is blocked by the
# allowlist-aware _is_gh_api_mutation() path, producing the
# sandbox_block_gh_api_mutation audit kind this test asserts on.
PAYLOAD_B4B=$(cat <<'EOF'
{
  "tool_name": "Bash",
  "tool_input": {"command": "gh api graphql -f query='mutation { closeIssue(input:{issueId:\"x\"}) { clientMutationId } }'"},
  "cwd": "WORKTREE_CWD_PLACEHOLDER"
}
EOF
)
PAYLOAD_B4B="${PAYLOAD_B4B/WORKTREE_CWD_PLACEHOLDER/$WT_CWD}"
assert_blocked "B4b: non-allowlisted graphql mutation (closeIssue) from worktree" "$PAYLOAD_B4B" "sandbox_block_gh_api_mutation"

# ---------------------------------------------------------------------------
# Test C: mutations from /tmp/random (untrusted cwd) → BLOCKED
# ---------------------------------------------------------------------------
echo ""
echo "--- Test C: mutations from untrusted cwd ---"

PAYLOAD_C1=$(cat <<EOF
{
  "tool_name": "Agent",
  "tool_input": {"prompt": "spawn from /tmp"},
  "cwd": "$UNTRUSTED_CWD"
}
EOF
)
assert_blocked "C1: Agent() from untrusted cwd" "$PAYLOAD_C1" "sandbox_block_agent_spawn"

PAYLOAD_C2=$(cat <<EOF
{
  "tool_name": "Bash",
  "tool_input": {"command": "gh api repos/owner/repo/issues -X POST -f title=x"},
  "cwd": "$UNTRUSTED_CWD"
}
EOF
)
assert_blocked "C2: gh api -X POST from untrusted cwd" "$PAYLOAD_C2"

PAYLOAD_C3=$(cat <<EOF
{
  "tool_name": "Edit",
  "tool_input": {"file_path": "$MAIN_REPO_ROOT/CLAUDE.md", "old_string": "x", "new_string": "y"},
  "cwd": "$UNTRUSTED_CWD"
}
EOF
)
assert_blocked "C3: Edit outside worktree from untrusted cwd" "$PAYLOAD_C3"

# ---------------------------------------------------------------------------
# Test D: same mutations from team_lead CWD → ALLOWED
# ---------------------------------------------------------------------------
echo ""
echo "--- Test D: mutations from team_lead cwd → allowed ---"

PAYLOAD_D1=$(cat <<EOF
{
  "tool_name": "Agent",
  "tool_input": {"prompt": "spawn from team lead"},
  "cwd": "$TL_CWD"
}
EOF
)
assert_allowed "D1: Agent() from team_lead" "$PAYLOAD_D1"

PAYLOAD_D2=$(cat <<EOF
{
  "tool_name": "Bash",
  "tool_input": {"command": "gh api repos/owner/repo/issues -X POST -f title=x"},
  "cwd": "$TL_CWD"
}
EOF
)
assert_allowed "D2: gh api -X POST from team_lead" "$PAYLOAD_D2"

PAYLOAD_D3=$(cat <<EOF
{
  "tool_name": "Edit",
  "tool_input": {"file_path": "$TL_CWD/CLAUDE.md", "old_string": "x", "new_string": "y"},
  "cwd": "$TL_CWD"
}
EOF
)
assert_allowed "D3: Edit within team_lead from team_lead cwd" "$PAYLOAD_D3"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"

if [ "$FAIL" -gt 0 ]; then
  red "FAIL — $FAIL test(s) did not pass"
  exit 1
else
  green "All $PASS tests passed"
  exit 0
fi
