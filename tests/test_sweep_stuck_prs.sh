#!/usr/bin/env bash
# tests/test_sweep_stuck_prs.sh — unit tests for sweep-stuck-prs.sh
#
# Tests use a mocked `gh` command to avoid any real GitHub API calls.
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
#
# Usage:
#   bash tests/test_sweep_stuck_prs.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
ERRORS=()

# ── Helpers ────────────────────────────────────────────────────────────────

pass() { echo "  PASS: $1"; ((PASS++)); }
fail() { echo "  FAIL: $1"; ((FAIL++)); ERRORS+=("$1"); }

# Set up a temp workspace for each test
setup() {
  TEST_DIR=$(mktemp -d)
  mkdir -p "$TEST_DIR/.autonomous-team"
  mkdir -p "$TEST_DIR/scripts/lib"
  mkdir -p "$TEST_DIR/backend"

  # Copy real helper scripts into test area
  cp "$REPO_ROOT/scripts/lib/stuck-pr-detect.sh" "$TEST_DIR/scripts/lib/"
  cp "$REPO_ROOT/scripts/lib/gh-label.sh"        "$TEST_DIR/scripts/lib/"
  cp "$REPO_ROOT/scripts/sweep-stuck-prs.sh"     "$TEST_DIR/scripts/"
  cp "$REPO_ROOT/backend/spawn_queue.py"          "$TEST_DIR/backend/"

  # repo-resolve.sh, and a config.json for it to resolve.
  #
  # These were missing, and the fixture passed anyway: sweep-stuck-prs.sh
  # sourced a file that did not exist, `_resolve_repo` was therefore an
  # undefined command, REPO ended up empty, and `gh pr list --repo ""` went to
  # the mock — which answers every query identically, so the empty slug was
  # invisible. Against the real gh an empty --repo is not an error either: it
  # exits 0 after silently resolving from the checkout's git remote. The
  # fixture was reproducing exactly the failure this suite should catch.
  cp "$REPO_ROOT/scripts/lib/repo-resolve.sh"    "$TEST_DIR/scripts/lib/"
  cat > "$TEST_DIR/.autonomous-team/config.json" <<'JSON'
{"repo": "test-owner/test-repo"}
JSON

  # stub rotate-team-log.sh — just echo the comment
  mkdir -p "$TEST_DIR/scripts"
  cat > "$TEST_DIR/scripts/rotate-team-log.sh" <<'SH'
#!/usr/bin/env bash
# stub: echo team-log comment to stdout for test capture
if [ "${1:-}" = "comment" ]; then
  echo "TEAM_LOG: ${2:-}"
fi
SH
  chmod +x "$TEST_DIR/scripts/rotate-team-log.sh"

  RESPAWNS_FILE="$TEST_DIR/.autonomous-team/stuck-pr-respawns.json"
  export STUCK_PR_THRESHOLD_MINUTES=30
}

teardown() {
  rm -rf "$TEST_DIR"
}

# Mock `gh` command factory — writes a shell script to $TEST_DIR/bin/gh
# that returns different responses based on what is asked.
install_gh_mock() {
  local pr_list_json="${1:-[]}"
  local pr_view_json="${2:-{}}"

  mkdir -p "$TEST_DIR/bin"
  cat > "$TEST_DIR/bin/gh" <<GHEOF
#!/usr/bin/env bash
# Minimal gh mock for sweep-stuck-prs tests
args="\$*"

if echo "\$args" | grep -q "pr list"; then
  echo '$pr_list_json'
  exit 0
fi

if echo "\$args" | grep -q "pr view"; then
  echo '$pr_view_json'
  exit 0
fi

if echo "\$args" | grep -q "api -X POST.*labels"; then
  echo '{"labels": []}'
  exit 0
fi

if echo "\$args" | grep -q "api -X DELETE.*labels"; then
  exit 0
fi

echo "[]"
exit 0
GHEOF
  chmod +x "$TEST_DIR/bin/gh"
  export PATH="$TEST_DIR/bin:$PATH"
}

# ── Test 1: No stuck PRs — exits 0, prints "0 stuck PRs found" ─────────────

test_no_stuck_prs() {
  setup
  install_gh_mock "[]" "{}"

  output=$(DRY_RUN=1 bash "$TEST_DIR/scripts/sweep-stuck-prs.sh" 2>/dev/null)
  rc=$?

  if [ $rc -eq 0 ] && echo "$output" | grep -q "0 stuck PRs found"; then
    pass "no_stuck_prs: exits 0 and prints '0 stuck PRs found'"
  else
    fail "no_stuck_prs: expected exit 0 with '0 stuck PRs found', got rc=$rc output='$output'"
  fi
  teardown
}

# ── Test 2: One stuck PR — enqueues respawn, increments counter to 1 ────────

test_one_stuck_pr_first_encounter() {
  setup

  # PR #99 stuck >30min
  local old_time
  old_time=$(date -u -d "60 minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             date -u -v-60M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             echo "2026-05-10T05:00:00Z")

  local pr_list='[{"number":99,"updatedAt":"'"$old_time"'","labels":[{"name":"code-review-needs-fix"}]}]'
  local pr_view='{"body":"Implements Discussion #415.\n\nSome changes.","headRefName":"discussion-415-stuck-prs","comments":[]}'

  install_gh_mock "$pr_list" "$pr_view"

  # Stub spawn_queue.py to record what was enqueued. Result file lives
  # under this test's own $TEST_DIR, not a fixed /tmp name (D#2254).
  ENQUEUE_RESULT="$TEST_DIR/test-enqueue-result.json"
  cat > "$TEST_DIR/backend/spawn_queue.py" <<PY
#!/usr/bin/env python3
import sys, json
args = sys.argv[1:]
if args and args[0] == "enqueue":
    with open("$ENQUEUE_RESULT","w") as f:
        json.dump({"args": args}, f)
    print("enqueued")
    sys.exit(0)
sys.exit(0)
PY

  output=$(DRY_RUN="" bash "$TEST_DIR/scripts/sweep-stuck-prs.sh" 2>/dev/null)
  rc=$?

  # Check exit 0
  if [ $rc -ne 0 ]; then
    fail "one_stuck_pr_first: expected exit 0, got $rc"
    teardown; return
  fi

  # Check PR count reported
  if ! echo "$output" | grep -q "1 stuck PRs found"; then
    fail "one_stuck_pr_first: expected '1 stuck PRs found' in output"
    teardown; return
  fi

  # Check respawns counter incremented
  if [ -f "$RESPAWNS_FILE" ]; then
    count=$(python3 -c "import json; d=json.load(open('$RESPAWNS_FILE')); print(d.get('99',{}).get('count',0))" 2>/dev/null || echo "?")
    if [ "$count" = "1" ]; then
      pass "one_stuck_pr_first: respawn counter incremented to 1"
    else
      fail "one_stuck_pr_first: expected counter=1 for PR #99, got '$count'"
    fi
  else
    fail "one_stuck_pr_first: respawns file not created at $RESPAWNS_FILE"
  fi

  teardown
}

# ── Test 3: Second encounter — increments counter to 2 ──────────────────────

test_second_encounter_increments_counter() {
  setup

  local old_time
  old_time=$(date -u -d "90 minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             date -u -v-90M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             echo "2026-05-10T04:00:00Z")

  local pr_list='[{"number":99,"updatedAt":"'"$old_time"'","labels":[{"name":"code-review-needs-fix"}]}]'
  local pr_view='{"body":"Discussion #415","headRefName":"discussion-415-stuck","comments":[]}'
  install_gh_mock "$pr_list" "$pr_view"

  # Pre-seed counter at 1
  echo '{"99":{"count":1,"last_respawn":"2026-05-10T05:00:00+00:00"}}' > "$RESPAWNS_FILE"

  # Stub spawn_queue.py
  cat > "$TEST_DIR/backend/spawn_queue.py" <<'PY'
#!/usr/bin/env python3
import sys
if sys.argv[1:] and sys.argv[1] == "enqueue":
    print("enqueued")
    sys.exit(0)
sys.exit(0)
PY

  DRY_RUN="" bash "$TEST_DIR/scripts/sweep-stuck-prs.sh" 2>/dev/null

  count=$(python3 -c "import json; d=json.load(open('$RESPAWNS_FILE')); print(d.get('99',{}).get('count',0))" 2>/dev/null || echo "?")
  if [ "$count" = "2" ]; then
    pass "second_encounter: counter incremented to 2"
  else
    fail "second_encounter: expected counter=2, got '$count'"
  fi

  teardown
}

# ── Test 4: Third encounter (count >= 2) — escalate, no enqueue ─────────────

test_third_encounter_escalates() {
  setup

  local old_time
  old_time=$(date -u -d "120 minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             date -u -v-120M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             echo "2026-05-10T03:00:00Z")

  local pr_list='[{"number":99,"updatedAt":"'"$old_time"'","labels":[{"name":"code-review-needs-fix"}]}]'
  local pr_view='{"body":"Discussion #415","headRefName":"discussion-415-stuck","comments":[]}'
  install_gh_mock "$pr_list" "$pr_view"

  # Pre-seed counter at 2
  echo '{"99":{"count":2,"last_respawn":"2026-05-10T04:00:00+00:00"}}' > "$RESPAWNS_FILE"

  # Stub spawn_queue.py — should NOT be called. The marker file lives under
  # this test's own $TEST_DIR (mktemp'd per-test), not a fixed /tmp name, so
  # a concurrently-running copy of this suite can't clobber it (D#2254).
  ENQUEUE_CALLED=0
  ENQUEUE_MARKER="$TEST_DIR/unexpected-enqueue.txt"
  cat > "$TEST_DIR/backend/spawn_queue.py" <<PY
#!/usr/bin/env python3
import sys
if sys.argv[1:] and sys.argv[1] == "enqueue":
    # Record that enqueue was unexpectedly called
    with open("$ENQUEUE_MARKER", "w") as f:
        f.write("enqueue called!\n")
    print("enqueued")
    sys.exit(0)
sys.exit(0)
PY
  rm -f "$ENQUEUE_MARKER"

  output=$(DRY_RUN="" bash "$TEST_DIR/scripts/sweep-stuck-prs.sh" 2>/dev/null)

  # Should NOT have called enqueue
  if [ -f "$ENQUEUE_MARKER" ]; then
    fail "third_encounter: enqueue was called but should not be on 3rd stuck encounter"
    teardown; return
  fi

  # Should have posted to team-log with "stuck" message
  if echo "$output" | grep -iq "stuck"; then
    pass "third_encounter: team-log escalation message found in output"
  else
    fail "third_encounter: expected escalation message in output, got: '$output'"
  fi

  # Counter should NOT increment beyond 2
  count=$(python3 -c "import json; d=json.load(open('$RESPAWNS_FILE')); print(d.get('99',{}).get('count',0))" 2>/dev/null || echo "?")
  if [ "$count" = "2" ]; then
    pass "third_encounter: counter stays at 2 (no increment on escalation)"
  else
    fail "third_encounter: expected counter=2, got '$count'"
  fi

  teardown
}

# ── Test 5: list_stuck_prs helper — recent PR not included ──────────────────

test_recent_pr_not_stuck() {
  setup

  # PR updated just now
  local recent_time
  recent_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local pr_list='[{"number":50,"updatedAt":"'"$recent_time"'","labels":[{"name":"code-review-needs-fix"}]}]'
  install_gh_mock "$pr_list" "{}"

  source "$TEST_DIR/scripts/lib/stuck-pr-detect.sh"
  result=$(list_stuck_prs 30)
  count=$(echo "$result" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")

  if [ "$count" = "0" ]; then
    pass "recent_pr_not_stuck: recently-updated PR not included in stuck list"
  else
    fail "recent_pr_not_stuck: expected 0 stuck PRs, got $count"
  fi

  teardown
}

# ── Run all tests ─────────────────────────────────────────────────────────────

echo "=== test_sweep_stuck_prs.sh ==="

test_no_stuck_prs
test_one_stuck_pr_first_encounter
test_second_encounter_increments_counter
test_third_encounter_escalates
test_recent_pr_not_stuck

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [ "$FAIL" -gt 0 ]; then
  echo "FAILED tests:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi

echo "All tests passed."
exit 0
