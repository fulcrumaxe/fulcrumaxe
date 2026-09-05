#!/usr/bin/env bash
# tests/test_post_merge_hook_browser_queue.sh — hermetic tests for the
# browser_tour_queue step added to scripts/post-merge-hook.sh.
#
# Tests use a mocked `gh` command to avoid real GitHub API calls.
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
#
# Usage:
#   bash tests/test_post_merge_hook_browser_queue.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="${REPO_ROOT}/scripts/post-merge-hook.sh"

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
    fail "$label — expected to find: $(echo "$needle" | head -1)"
    echo "    Haystack: $(echo "$haystack" | head -5)"
  fi
}

assert_not_contains() {
  local label="$1" haystack="$2" needle="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    fail "$label — unexpectedly found: $needle"
  else
    pass "$label"
  fi
}

assert_file_contains() {
  local label="$1" file="$2" needle="$3"
  if [[ -f "$file" ]] && grep -qF "$needle" "$file"; then
    pass "$label"
  else
    fail "$label — file $file does not contain: $needle"
    [[ -f "$file" ]] && echo "    File content: $(cat "$file" | head -3)" || echo "    File not found"
  fi
}

# ── Build a temp environment ──────────────────────────────────────────────────

setup_env() {
  local tmpdir
  tmpdir=$(mktemp -d)

  # Minimal git repo so hook_event_init works with a real REPO_ROOT
  git -C "$tmpdir" init -q
  git -C "$tmpdir" config user.email "test@test.com"
  git -C "$tmpdir" config user.name "Test"

  # Mirror needed dirs
  mkdir -p "$tmpdir/.autonomous-team/hook-events"
  mkdir -p "$tmpdir/scripts/lib"

  # Copy needed scripts
  cp "${REPO_ROOT}/scripts/post-merge-hook.sh" "$tmpdir/scripts/"
  cp "${REPO_ROOT}/scripts/lib/hook-event.sh" "$tmpdir/scripts/lib/"
  # Stub rotate-team-log.sh
  cat > "$tmpdir/scripts/rotate-team-log.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$tmpdir/scripts/rotate-team-log.sh"

  # Stub post-merge-wiki.sh
  cat > "$tmpdir/scripts/post-merge-wiki.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$tmpdir/scripts/post-merge-wiki.sh"

  # Stub agent-feed-append.sh
  cat > "$tmpdir/scripts/agent-feed-append.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$tmpdir/scripts/agent-feed-append.sh"

  # lib/worktree-registry.sh stub — use return so it works when sourced
  cat > "$tmpdir/scripts/lib/worktree-registry.sh" <<'SH'
#!/usr/bin/env bash
# Define worktree_registry as a no-op function for hook compatibility
worktree_registry() { return 0; }
SH
  chmod +x "$tmpdir/scripts/lib/worktree-registry.sh"

  # Stub backend dir for lessons
  mkdir -p "$tmpdir/backend"
  cat > "$tmpdir/backend/__init__.py" <<'PY'
PY
  cat > "$tmpdir/backend/lessons.py" <<'PY'
class LessonsStore:
    def record(self, **kwargs): pass
PY
  cat > "$tmpdir/backend/blackboard.py" <<'PY'
def get_blackboard():
    class BB:
        def get(self, key): return None
    return BB()
PY

  echo "$tmpdir"
}

# ── Mock gh that simulates a PR touching dashboard/src/pages/IdeasPage.tsx ───

make_mock_gh_dashboard() {
  local bindir="$1"
  cat > "$bindir/gh" <<'GH'
#!/usr/bin/env bash
# Mock gh for browser_tour_queue tests
# The hook calls: gh pr view N --repo ... --json files --jq '[.files[].path | select(...)] | join("\n")'
# Since --jq is processed by the real gh, the mock must return already-filtered output
if [[ "$*" == *"pr view"* && "$*" == *"--json files"* ]]; then
  # Return newline-separated dashboard file paths (as --jq would produce)
  printf 'dashboard/src/pages/IdeasPage.tsx\ndashboard/src/components/Chart.tsx\n'
  exit 0
fi
if [[ "$*" == *"pr view"* && "$*" == *"--json body"* ]]; then
  echo ''
  exit 0
fi
# GraphQL calls (Discussion close, wiki, etc.) — return minimal valid JSON
if [[ "$*" == *"graphql"* ]]; then
  echo '{"data":{"repository":{"discussion":{"id":"D_test","body":"<!-- STATUS:SPEC_READY -->"}}}}'
  exit 0
fi
exit 0
GH
  chmod +x "$bindir/gh"
}

# ── Mock gh that simulates a PR NOT touching dashboard/ ──────────────────────

make_mock_gh_no_dashboard() {
  local bindir="$1"
  cat > "$bindir/gh" <<'GH'
#!/usr/bin/env bash
if [[ "$*" == *"pr view"* && "$*" == *"--json files"* ]]; then
  # No dashboard files — return empty string (simulating jq filter returning nothing)
  echo ""
  exit 0
fi
if [[ "$*" == *"pr view"* && "$*" == *"--json body"* ]]; then
  echo ''
  exit 0
fi
if [[ "$*" == *"graphql"* ]]; then
  echo '{"data":{"repository":{"discussion":{"id":"D_test","body":"<!-- STATUS:SPEC_READY -->"}}}}'
  exit 0
fi
exit 0
GH
  chmod +x "$bindir/gh"
}

# ── Test 1: dashboard PR queues an entry with /ideas in affected_pages ────────

echo "Test 1: dashboard-touching PR queues browser-tour entry"
{
  tmpdir=$(setup_env)
  mockbin="${tmpdir}/mockbin"
  mkdir -p "$mockbin"
  make_mock_gh_dashboard "$mockbin"

  QUEUE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.jsonl"
  export PATH="$mockbin:$PATH"
  export SKIP_WIKI_SYNC=1  # not used, but document intent

  out=$(REPO_ROOT="$tmpdir" bash "${tmpdir}/scripts/post-merge-hook.sh" --pr 42 2>&1) && rc=0 || rc=$?

  if [[ -f "$QUEUE_FILE" ]] && [[ -s "$QUEUE_FILE" ]]; then
    queue_content=$(cat "$QUEUE_FILE")
    assert_contains "queue entry written" "$queue_content" '"trigger": "post-merge"'
    assert_contains "pr=42 in entry" "$queue_content" '"pr": 42'
    assert_contains "/ideas in affected_pages" "$queue_content" '"/ideas"'
    assert_contains "status=pending" "$queue_content" '"status": "pending"'
    assert_contains "hook log mentions queued" "$out" "Browser-tour queued"
  else
    fail "queue file not created at $QUEUE_FILE"
    fail "pr=42 in entry"
    fail "/ideas in affected_pages"
    fail "status=pending"
    fail "hook log mentions queued"
  fi

  rm -rf "$tmpdir"
}

# ── Test 2: non-dashboard PR does NOT write a queue entry ────────────────────

echo "Test 2: non-dashboard PR skips browser-tour queue"
{
  tmpdir=$(setup_env)
  mockbin="${tmpdir}/mockbin"
  mkdir -p "$mockbin"
  make_mock_gh_no_dashboard "$mockbin"

  QUEUE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.jsonl"
  export PATH="$mockbin:$PATH"

  out=$(REPO_ROOT="$tmpdir" bash "${tmpdir}/scripts/post-merge-hook.sh" --pr 99 2>&1) && rc=0 || rc=$?

  if [[ -f "$QUEUE_FILE" ]] && [[ -s "$QUEUE_FILE" ]]; then
    fail "queue file should be empty for non-dashboard PR"
  else
    pass "no queue entry for non-dashboard PR"
  fi
  assert_contains "hook log says no tour queued" "$out" "does not touch dashboard"

  rm -rf "$tmpdir"
}

# ── Test 3: component file (non-page) triggers root page tour ─────────────────

echo "Test 3: component file outside pages/ triggers / tour"
{
  tmpdir=$(setup_env)
  mockbin="${tmpdir}/mockbin"
  mkdir -p "$mockbin"

  # Mock: only a non-page dashboard file (simulate --jq returning dashboard paths)
  cat > "$mockbin/gh" <<'GH'
#!/usr/bin/env bash
if [[ "$*" == *"pr view"* && "$*" == *"--json files"* ]]; then
  printf 'dashboard/src/components/AgentFeed.tsx\n'
  exit 0
fi
if [[ "$*" == *"pr view"* && "$*" == *"--json body"* ]]; then
  echo ''
  exit 0
fi
if [[ "$*" == *"graphql"* ]]; then
  echo '{"data":{"repository":{"discussion":{"id":"D_test","body":"<!-- STATUS:SPEC_READY -->"}}}}'
  exit 0
fi
exit 0
GH
  chmod +x "$mockbin/gh"
  export PATH="$mockbin:$PATH"

  QUEUE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.jsonl"

  out=$(REPO_ROOT="$tmpdir" bash "${tmpdir}/scripts/post-merge-hook.sh" --pr 77 2>&1) && rc=0 || rc=$?

  if [[ -f "$QUEUE_FILE" ]] && [[ -s "$QUEUE_FILE" ]]; then
    queue_content3=$(cat "$QUEUE_FILE")
    assert_contains "root page in affected_pages" "$queue_content3" '"/"'
    pass "component file triggers root tour"
  else
    fail "queue entry missing for component file"
    fail "root page in affected_pages"
  fi

  rm -rf "$tmpdir"
}

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failed tests:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
exit 0
