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
REAL_PYTHON3="$(command -v python3)"

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

  # D#2444 AC-5: the drift-guard test drives scripts/lib/pr-pickup-gate.sh's
  # pr_pickup_blocked/_ppg_gate_hint side by side with this sweeper, against
  # the identical mocked gh, so it needs its own copy here too. Plain copy
  # (not a symlink) is fine — unlike pr_intake_gate.py it resolves nothing
  # from its own path except the sibling pr_intake_gate.py placed alongside
  # it below.
  cp "$REPO_ROOT/scripts/lib/pr-pickup-gate.sh"  "$TEST_DIR/scripts/lib/"

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

  # D#2421 (porting D#2404's sweeper gate): the sweeper now runs the PR author
  # gate before enqueuing a respawn. SYMLINK rather than copy: the module
  # resolves its own repo root from Path(__file__).resolve(), which follows
  # the link back to the real checkout, so its imports (external_intake_gate,
  # pr_head_baseline, backend._repo, ...) resolve without copying half the
  # tree into the fixture. Only the shell script under test runs from $TEST_DIR.
  ln -sf "$REPO_ROOT/scripts/lib/pr_intake_gate.py" "$TEST_DIR/scripts/lib/"

  # Scratch state dir — the allowlist cache and PR head-baseline store this
  # module writes must not land in the operator's real one, and must be
  # empty at the start of every test.
  export AUTONOMOUS_TEAM_STATE_DIR="$TEST_DIR/state"
  mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"
  # The one login the fixture treats as trusted. Overriding it makes the
  # trust decision independent of whatever real config the checkout carries.
  export AUTONOMOUS_TEAM_BOT_ACCOUNT="fixture-bot"

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
# Minimal gh mock for sweep-stuck-prs tests.
#
# The REST endpoints below are also read by scripts/lib/pr_intake_gate.py
# (author, labels, label-application timeline) since D#2421 wires its gate
# into this sweeper. They answer from environment variables so a single mock
# covers every case; defaults are the trusted-author, no-comments shape every
# pre-gate test in this suite assumed.
args="\$*"

if echo "\$args" | grep -q "pr list"; then
  echo '$pr_list_json'
  exit 0
fi

if echo "\$args" | grep -q "pr view"; then
  echo '$pr_view_json'
  exit 0
fi

if echo "\$args" | grep -q "collaborators"; then
  echo '[]'
  exit 0
fi

if echo "\$args" | grep -qE "issues/[0-9]+/events"; then
  echo "\${MOCK_LABEL_EVENTS:-[]}"
  exit 0
fi

if echo "\$args" | grep -qE "pulls/[0-9]+\$"; then
  # D#2444 AC-5: MOCK_PR_HEAD_SHA is opt-in and omitted by default, so every
  # pre-existing test (none of which set it) sees the exact same JSON shape
  # as before. Only the drift-guard scenarios that need a real head_sha
  # (external_pr_head_changed_after_approval / _invalidation_ceiling) set it.
  if [ -n "\${MOCK_PR_HEAD_SHA:-}" ]; then
    printf '{"user":{"login":"%s","id":1},"labels":%s,"head":{"sha":"%s"}}\n' \
      "\${MOCK_PR_AUTHOR:-fixture-bot}" "\${MOCK_PR_LABELS:-[]}" "\${MOCK_PR_HEAD_SHA}"
  else
    printf '{"user":{"login":"%s","id":1},"labels":%s}\n' \
      "\${MOCK_PR_AUTHOR:-fixture-bot}" "\${MOCK_PR_LABELS:-[]}"
  fi
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

# ── Test 6 (D#2421, porting D#2404): a stuck PR from outside the trust set
#    gets no respawn ──────────────────────────────────────────────────────────
#
# The sweeper's respawn IS an agent spawn. Asserting the gate helper returns
# True would prove nothing about that; this drives the real sweeper and checks
# that spawn_queue.py was never invoked (D#2377).

test_untrusted_author_pr_is_not_respawned() {
  setup

  local old_time
  old_time=$(date -u -d "60 minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             date -u -v-60M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             echo "2026-05-10T05:00:00Z")

  local pr_list='[{"number":77,"updatedAt":"'"$old_time"'","labels":[{"name":"code-review-needs-fix"}]}]'
  install_gh_mock "$pr_list" '{"body":"stuck PR","headRefName":"x","comments":[]}'

  export MOCK_PR_AUTHOR="drive-by-stranger"

  ENQUEUE_MARKER="$TEST_DIR/unexpected-enqueue.txt"
  cat > "$TEST_DIR/backend/spawn_queue.py" <<PY
#!/usr/bin/env python3
import sys
if sys.argv[1:] and sys.argv[1] == "enqueue":
    with open("$ENQUEUE_MARKER", "w") as f:
        f.write("enqueue called!\n")
    print("enqueued")
sys.exit(0)
PY

  output=$(DRY_RUN="" bash "$TEST_DIR/scripts/sweep-stuck-prs.sh" 2>/dev/null)

  if [ -f "$ENQUEUE_MARKER" ]; then
    fail "untrusted_author: executor respawn was enqueued for a PR from outside the trust set"
  else
    pass "untrusted_author: no executor respawn enqueued"
  fi

  if echo "$output" | grep -q "gated"; then
    pass "untrusted_author: gate decision is visible in the sweeper output"
  else
    fail "untrusted_author: expected a 'gated' line, got: '$output'"
  fi

  # No respawn counter bump either — a gated PR is waiting on a human, not
  # stuck, and counting it would eventually apply needs-boss to a stranger's PR.
  count=$(python3 -c "import json; d=json.load(open('$RESPAWNS_FILE')); print(d.get('77',{}).get('count',0))" 2>/dev/null || echo "?")
  if [ "$count" = "0" ]; then
    pass "untrusted_author: respawn counter untouched"
  else
    fail "untrusted_author: expected counter=0 for gated PR #77, got '$count'"
  fi

  # D#2444 AC-3 — a genuinely unapproved PR must keep the maintainer wording
  # and must NOT gain rebaseline-pr (fixing the unrecorded-head half must not
  # break this one).
  if echo "$output" | grep -q "awaiting intake-approved"; then
    pass "untrusted_author: gated line still tells a genuinely unapproved PR to await a maintainer (D#2444 AC-3)"
  else
    fail "untrusted_author: gated line lost the maintainer-approval wording: $output"
  fi
  if echo "$output" | grep -q "rebaseline-pr"; then
    fail "untrusted_author: gated line names rebaseline-pr for a PR that was never approved (D#2444 AC-3)"
  else
    pass "untrusted_author: gated line does not name rebaseline-pr for an unapproved PR (D#2444 AC-3)"
  fi

  # D#2444 AC-4 — the sweep must have provably inspected the PR: the count of
  # "PR #<N> age=... respawns=..." inspection lines must equal the number of
  # stuck PRs supplied (1 here), not merely be nonzero. "0 stuck PRs found"
  # and "could not read the queue" produce identical stdout otherwise, so a
  # negative-only assertion (stdout lacks certain text) would pass vacuously
  # on a sweep that examined nothing.
  inspect_count=$(echo "$output" | grep -cE '^  PR #[0-9]+  age=[0-9]+min  respawns=[0-9]+$')
  if [ "$inspect_count" -eq 1 ]; then
    pass "untrusted_author: exactly one PR-inspection line printed (D#2444 AC-4)"
  else
    fail "untrusted_author: expected 1 PR-inspection line, got $inspect_count in: $output"
  fi

  unset MOCK_PR_AUTHOR
  teardown
}

# ── Test 7 (D#2444 AC-2/AC-4) — an approved PR with no recorded head names
#    rebaseline-pr, not "awaiting intake-approved" ──────────────────────────
#
# external_pr_head_unrecorded means the PR *is* approved by a trusted
# account — it just lacks a recorded head baseline (the routine
# state-dir-loss shape after D#2421 PR 3). The old wording told the operator
# to chase a maintainer who had already approved it; this is the defect
# D#2444 exists to fix, on the sweeper's spawn path.

test_gated_unrecorded_head_names_rebaseline() {
  setup

  local old_time labeled_at
  old_time=$(date -u -d "60 minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             date -u -v-60M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             echo "2026-05-10T05:00:00Z")
  # Applied well outside the 900s first-observation grace, so the baseline
  # reads "unknown" -> external_pr_head_unrecorded instead of auto-baselining
  # to "match".
  labeled_at=$(date -u -d "2 hours ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
               date -u -v-2H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
               echo "2026-05-10T03:00:00Z")

  local pr_list='[{"number":4242,"updatedAt":"'"$old_time"'","labels":[{"name":"code-review-needs-fix"}]}]'
  install_gh_mock "$pr_list" '{"body":"stuck PR","headRefName":"x","comments":[]}'

  export MOCK_PR_AUTHOR="drive-by-stranger"
  export MOCK_PR_LABELS='[{"name":"intake-approved"}]'
  export MOCK_LABEL_EVENTS='[{"event":"labeled","id":1,"created_at":"'"$labeled_at"'","label":{"name":"intake-approved"},"actor":{"login":"fixture-bot"}}]'

  output=$(DRY_RUN=1 bash "$TEST_DIR/scripts/sweep-stuck-prs.sh" 2>/dev/null)

  if echo "$output" | grep -q "external_pr_head_unrecorded"; then
    pass "gated_unrecorded_head: reason is external_pr_head_unrecorded"
  else
    fail "gated_unrecorded_head: expected external_pr_head_unrecorded, got: $output"
  fi

  if echo "$output" | grep -q "rebaseline-pr 4242"; then
    pass "gated_unrecorded_head: gated line names rebaseline-pr for PR #4242 (D#2444 AC-2)"
  else
    fail "gated_unrecorded_head: no rebaseline-pr recovery in: $output"
  fi

  if echo "$output" | grep -q "awaiting intake-approved"; then
    fail "gated_unrecorded_head: gated line still says an already-approved PR is 'awaiting intake-approved' (D#2444 AC-2)"
  else
    pass "gated_unrecorded_head: gated line no longer claims the approved PR is awaiting approval (D#2444 AC-2)"
  fi

  # D#2444 AC-4 — same count-not-vacuous-negative guard as the untrusted-author
  # test above, for this reason.
  inspect_count=$(echo "$output" | grep -cE '^  PR #[0-9]+  age=[0-9]+min  respawns=[0-9]+$')
  if [ "$inspect_count" -eq 1 ]; then
    pass "gated_unrecorded_head: exactly one PR-inspection line printed (D#2444 AC-4)"
  else
    fail "gated_unrecorded_head: expected 1 PR-inspection line, got $inspect_count in: $output"
  fi

  unset MOCK_PR_AUTHOR MOCK_PR_LABELS MOCK_LABEL_EVENTS
  teardown
}

# ── Test 8 (D#2444 AC-5) — the sweeper and pr-pickup-gate.sh cannot drift ───
#
# Both paths now read `hint` straight off check-pr's JSON
# (scripts/lib/pr_intake_gate.py's `_gate_hint`, the single source) instead of
# keeping independent copies of the remedy text — that duplication is exactly
# what let D#2421 PR 3 fix pr-pickup-gate.sh's wording and leave the
# sweeper's copy wrong. This drives both readers against the identical mocked
# `gh`, for every reason the two paths can produce, and requires their
# remedy text to be byte-identical. Editing either side's wording alone (or
# reverting either reader to its own hardcoded string) fails this.

# _hint_via_pickup_gate <pr> — pr-pickup-gate.sh's remedy text for <pr>, via
# the real pr_pickup_blocked + _ppg_gate_hint against the mocked gh already
# installed in this test's $TEST_DIR.
_hint_via_pickup_gate() {
  (
    source "$TEST_DIR/scripts/lib/pr-pickup-gate.sh"
    pr_pickup_blocked "$1" >/dev/null 2>&1
    _ppg_gate_hint "$_PR_GATE_REASON" "$1"
  )
}

# _hint_via_sweeper <pr> <pr_list_json> — the real sweeper's remedy text for
# the fixture PR's gated line, driven end to end (gh pr list included).
_hint_via_sweeper() {
  local pr="$1" pr_list="$2"
  install_gh_mock "$pr_list" '{"body":"stuck PR","headRefName":"x","comments":[]}'
  local output
  output=$(DRY_RUN=1 bash "$TEST_DIR/scripts/sweep-stuck-prs.sh" 2>/dev/null)
  echo "$output" | grep -E "^    -> gated: " | sed 's/^.*no respawn, //'
}

_assert_hint_equal() {
  local reason="$1" hint_a="$2" hint_b="$3"
  if [ -n "$hint_a" ] && [ "$hint_a" = "$hint_b" ]; then
    pass "hint_drift_guard: $reason — pr-pickup-gate.sh and the sweeper emit byte-identical remedy text (D#2444 AC-5)"
  else
    fail "hint_drift_guard: $reason — remedy text diverged: pickup-gate='$hint_a' sweeper='$hint_b'"
  fi
}

# Pre-seed a baseline row already at the invalidation ceiling (CEILING=3 in
# scripts/lib/pr_head_baseline.py), so a single check-pr call reaches
# external_pr_head_invalidation_ceiling without four real gate calls to drift
# there one bump at a time.
#
# The seeded key must be computed the same way _hint_via_pickup_gate and
# _hint_via_sweeper now resolve it (D#2422: both pinned to the fixture's own
# $TEST_DIR/.autonomous-team/config.json via repo-resolve.sh), not by
# importing backend._repo.CODE_REPO directly here. That import resolves
# relative to $REPO_ROOT (the real checkout pr_intake_gate.py's __file__
# follows its symlink back to) rather than $TEST_DIR, so it used to land on
# the real checkout's own git-origin fallback — the same value check-pr's
# internal default happened to fall back to before either caller passed
# --repo. Once both callers pin to the fixture's slug instead, seeding under
# the checkout's slug writes to a different key than either caller now reads.
_seed_ceiling_baseline() {
  local pr="$1" repo_slug
  repo_slug="$(source "$TEST_DIR/scripts/lib/repo-resolve.sh" && _resolve_code_repo)"
  python3 - "$REPO_ROOT" "$pr" "$repo_slug" <<'PY'
import sys
sys.path.insert(0, f"{sys.argv[1]}/scripts/lib")
sys.path.insert(0, sys.argv[1])
import intake_baseline, pr_head_baseline

key = pr_head_baseline.pr_key(sys.argv[3], int(sys.argv[2]))
path = pr_head_baseline._default_store_path()
intake_baseline.record_baseline(
    key, content_sha256="sha-ceiling-base", last_edited_at=None,
    edit_count=0, editor=None, path=path, source="test-seed",
)
for _ in range(3):
    intake_baseline.bump_invalidation(key, path=path)
PY
}

test_hint_drift_guard() {
  setup
  install_gh_mock '[]' '{}'

  local fresh stale needs_fix_labels
  fresh=$(date -u -d "1 minute ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
          date -u -v-1M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
          echo "2026-05-10T05:00:00Z")
  stale=$(date -u -d "2 hours ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
          date -u -v-2H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
          echo "2026-05-10T03:00:00Z")
  local age_time
  age_time=$(date -u -d "60 minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             date -u -v-60M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             echo "2026-05-10T05:00:00Z")
  needs_fix_labels='[{"name":"code-review-needs-fix"}]'

  # ---- external_awaiting_intake_approval ----
  export MOCK_PR_AUTHOR="drive-by-stranger"
  unset MOCK_PR_LABELS MOCK_LABEL_EVENTS MOCK_PR_HEAD_SHA
  HINT_A=$(_hint_via_pickup_gate 6001)
  HINT_B=$(_hint_via_sweeper 6001 '[{"number":6001,"updatedAt":"'"$age_time"'","labels":'"$needs_fix_labels"'}]')
  _assert_hint_equal "external_awaiting_intake_approval" "$HINT_A" "$HINT_B"

  # ---- external_pr_head_unrecorded ----
  export MOCK_PR_LABELS='[{"name":"intake-approved"}]'
  export MOCK_LABEL_EVENTS='[{"event":"labeled","id":1,"created_at":"'"$stale"'","label":{"name":"intake-approved"},"actor":{"login":"fixture-bot"}}]'
  HINT_A=$(_hint_via_pickup_gate 6002)
  HINT_B=$(_hint_via_sweeper 6002 '[{"number":6002,"updatedAt":"'"$age_time"'","labels":'"$needs_fix_labels"'}]')
  _assert_hint_equal "external_pr_head_unrecorded" "$HINT_A" "$HINT_B"

  # ---- external_intake_approval_untrusted_actor (label applied by someone
  #      outside the trust set — not a real approval, D#2404 AC3) ----
  export MOCK_LABEL_EVENTS='[{"event":"labeled","id":1,"created_at":"'"$fresh"'","label":{"name":"intake-approved"},"actor":{"login":"another-stranger"}}]'
  HINT_A=$(_hint_via_pickup_gate 6003)
  HINT_B=$(_hint_via_sweeper 6003 '[{"number":6003,"updatedAt":"'"$age_time"'","labels":'"$needs_fix_labels"'}]')
  _assert_hint_equal "external_intake_approval_untrusted_actor" "$HINT_A" "$HINT_B"

  # ---- intake_approval_actor_unreadable (the winning labeled event's
  #      created_at does not parse) ----
  export MOCK_LABEL_EVENTS='[{"event":"labeled","id":1,"created_at":"not-a-date","label":{"name":"intake-approved"},"actor":{"login":"fixture-bot"}}]'
  HINT_A=$(_hint_via_pickup_gate 6004)
  HINT_B=$(_hint_via_sweeper 6004 '[{"number":6004,"updatedAt":"'"$age_time"'","labels":'"$needs_fix_labels"'}]')
  _assert_hint_equal "intake_approval_actor_unreadable" "$HINT_A" "$HINT_B"

  # ---- external_pr_head_changed_after_approval ----
  export MOCK_LABEL_EVENTS='[{"event":"labeled","id":1,"created_at":"'"$fresh"'","label":{"name":"intake-approved"},"actor":{"login":"fixture-bot"}}]'
  export MOCK_PR_HEAD_SHA="sha-6005-a"
  # First call auto-baselines the fresh label to sha-6005-a ("match") and
  # records it; both callers share one on-disk store, so it doesn't matter
  # which one records it.
  _hint_via_pickup_gate 6005 >/dev/null
  export MOCK_PR_HEAD_SHA="sha-6005-b"
  HINT_A=$(_hint_via_pickup_gate 6005)
  HINT_B=$(_hint_via_sweeper 6005 '[{"number":6005,"updatedAt":"'"$age_time"'","labels":'"$needs_fix_labels"'}]')
  _assert_hint_equal "external_pr_head_changed_after_approval" "$HINT_A" "$HINT_B"

  # ---- external_pr_head_invalidation_ceiling ----
  _seed_ceiling_baseline 6006
  export MOCK_PR_HEAD_SHA="sha-6006-new"
  HINT_A=$(_hint_via_pickup_gate 6006)
  HINT_B=$(_hint_via_sweeper 6006 '[{"number":6006,"updatedAt":"'"$age_time"'","labels":'"$needs_fix_labels"'}]')
  _assert_hint_equal "external_pr_head_invalidation_ceiling" "$HINT_A" "$HINT_B"
  unset MOCK_PR_HEAD_SHA

  # ---- pr_meta_unreadable ----
  # The mock always emits syntactically valid JSON around MOCK_PR_AUTHOR, so
  # unparseable JSON is forced a different way: a login value containing an
  # unescaped quote breaks the printf'd JSON outright.
  unset MOCK_PR_LABELS MOCK_LABEL_EVENTS
  export MOCK_PR_AUTHOR='stranger"broken'
  HINT_A=$(_hint_via_pickup_gate 6007)
  HINT_B=$(_hint_via_sweeper 6007 '[{"number":6007,"updatedAt":"'"$age_time"'","labels":'"$needs_fix_labels"'}]')
  _assert_hint_equal "pr_meta_unreadable" "$HINT_A" "$HINT_B"

  # ---- gate_check_failed (no JSON produced at all — pr_intake_gate.py
  #      missing from the path both callers resolve it from) ----
  unset MOCK_PR_AUTHOR
  rm -f "$TEST_DIR/scripts/lib/pr_intake_gate.py"
  HINT_A=$(_hint_via_pickup_gate 6008)
  HINT_B=$(_hint_via_sweeper 6008 '[{"number":6008,"updatedAt":"'"$age_time"'","labels":'"$needs_fix_labels"'}]')
  _assert_hint_equal "gate_check_failed" "$HINT_A" "$HINT_B"

  unset MOCK_PR_AUTHOR MOCK_PR_LABELS MOCK_LABEL_EVENTS MOCK_PR_HEAD_SHA
  teardown
}

# ── Test 9 (D#2422 item 1) — the author-gate call pins --repo explicitly ────
#
# pr_gate_blocked used to call pr_intake_gate.py check-pr without --repo,
# leaning on that module's own internal default resolver instead of the
# $REPO this script already resolved via repo-resolve.sh. Both resolve to
# the same slug today, so nothing observably breaks yet in the gh-mock
# assertions above — this test doesn't depend on the gate's *answer*
# differing, only on the subprocess argv the sweeper actually constructs,
# which is the thing that would silently diverge the day the two resolvers
# stop agreeing.
test_gate_call_pins_repo_flag() {
  setup

  local old_time
  old_time=$(date -u -d "60 minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             date -u -v-60M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             echo "2026-05-10T05:00:00Z")
  local pr_list='[{"number":88,"updatedAt":"'"$old_time"'","labels":[{"name":"code-review-needs-fix"}]}]'
  install_gh_mock "$pr_list" '{"body":"stuck PR","headRefName":"x","comments":[]}'

  # Shim python3 to log every argv used to invoke pr_intake_gate.py, then
  # delegate to the real interpreter so the gate still runs for real.
  CALL_LOG="$TEST_DIR/python3-calls.log"
  : > "$CALL_LOG"
  cat > "$TEST_DIR/bin/python3" <<SHIMEOF
#!/usr/bin/env bash
if printf '%s' "\$*" | grep -q "pr_intake_gate.py"; then
  printf '%s\n' "\$*" >> "$CALL_LOG"
fi
exec "$REAL_PYTHON3" "\$@"
SHIMEOF
  chmod +x "$TEST_DIR/bin/python3"

  DRY_RUN="" bash "$TEST_DIR/scripts/sweep-stuck-prs.sh" >/dev/null 2>&1

  local check_pr_call
  check_pr_call=$(grep "check-pr" "$CALL_LOG" | head -1)
  if echo "$check_pr_call" | grep -qE -- "--repo[[:space:]]+test-owner/test-repo"; then
    pass "gate_call_pins_repo: pr_gate_blocked passes --repo test-owner/test-repo to check-pr"
  else
    fail "gate_call_pins_repo: expected --repo test-owner/test-repo in check-pr invocation, got: '$check_pr_call'"
  fi

  teardown
}

# ── Test 10 (D#2422 item 4) — the empty-reason case that broke @tsv ────────
#
# The first cut of the jq consolidation joined fields with @tsv. bash's IFS
# whitespace-collapsing treats a lone tab as ordinary whitespace even when
# IFS is set to just "\t", so two consecutive tabs (an empty `reason` next to
# `blocked=false`) silently merged into one delimiter and shifted every field
# after it — `hint` landed in `reason`'s slot and `reason` came back empty.
# This pins exactly that shape end to end through pr_pickup_blocked, not just
# through the jq expression in isolation, so a future rewrite that
# reintroduces @tsv fails here instead of shipping quietly.
test_empty_reason_with_blocked_false_survives_field_parse() {
  setup

  # setup() symlinks pr_intake_gate.py to the real checkout's copy (so its
  # own imports resolve); `rm -f` first so this stub lands in a plain file
  # at that path instead of writing through the symlink into the real
  # checkout — cat > on a symlink follows it to the target.
  rm -f "$TEST_DIR/scripts/lib/pr_intake_gate.py"
  cat > "$TEST_DIR/scripts/lib/pr_intake_gate.py" <<'PY'
#!/usr/bin/env python3
import json
import sys

if sys.argv[1:2] == ["check-pr"]:
    print(json.dumps({"blocked": False, "reason": "", "hint": "some hint"}))
    sys.exit(0)
sys.exit(1)
PY

  local hint
  hint=$(
    source "$TEST_DIR/scripts/lib/pr-pickup-gate.sh"
    pr_pickup_blocked 9999
    rc=$?
    printf 'rc=%s reason=[%s] hint=[%s]' "$rc" "$_PR_GATE_REASON" "$_PR_GATE_HINT"
  )

  if [ "$hint" = "rc=1 reason=[] hint=[some hint]" ]; then
    pass "empty_reason_field_parse: blocked=false with an empty reason parses all three fields correctly"
  else
    fail "empty_reason_field_parse: expected 'rc=1 reason=[] hint=[some hint]', got '$hint'"
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
test_untrusted_author_pr_is_not_respawned
test_gated_unrecorded_head_names_rebaseline
test_hint_drift_guard
test_gate_call_pins_repo_flag
test_empty_reason_with_blocked_false_survives_field_parse

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
