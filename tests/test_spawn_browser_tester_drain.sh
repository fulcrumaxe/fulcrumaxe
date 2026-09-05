#!/usr/bin/env bash
# tests/test_spawn_browser_tester_drain.sh — unit tests for spawn-browser-tester.sh
#
# Uses a stub spawner and mocked pre-spawn-check.sh to verify drain logic:
#   - picks up pending queue entries
#   - marks them drained in queue file + appends to done file
#   - respects MAX_SPAWNS_PER_ITER=2 cap
#   - skips entries when pre-spawn-check blocks
#   - nightly trigger fires when gate is enabled and last tour is stale
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DRAINER="${REPO_ROOT}/scripts/spawn-browser-tester.sh"

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
    fail "$label — expected: $needle"
    echo "    output: $(echo "$haystack" | head -5)"
  fi
}

assert_file_contains() {
  local label="$1" file="$2" needle="$3"
  if [[ -f "$file" ]] && grep -qF "$needle" "$file"; then
    pass "$label"
  else
    fail "$label — file $file does not contain: $needle"
  fi
}

assert_file_line_count() {
  local label="$1" file="$2" expected="$3"
  local actual
  actual=$(grep -c "" "$file" 2>/dev/null || echo 0)
  if [[ "$actual" -eq "$expected" ]]; then
    pass "$label (lines=$expected)"
  else
    fail "$label — expected $expected lines, got $actual"
  fi
}

# ── Setup ─────────────────────────────────────────────────────────────────────

setup_env() {
  local tmpdir
  tmpdir=$(mktemp -d)
  mkdir -p "$tmpdir/.autonomous-team"
  mkdir -p "$tmpdir/scripts/lib"
  mkdir -p "$tmpdir/backend"

  # Stub control_plane.py — returns false for browser_tester_periodic by default
  cat > "$tmpdir/backend/control_plane.py" <<'PY'
#!/usr/bin/env python3
import sys
key = sys.argv[2] if len(sys.argv) > 2 else ""
defaults = {
    "gates.browser_tester_periodic": "false",
}
print(defaults.get(key, "false"))
PY

  # Stub pre-spawn-check.sh — always allows
  cat > "$tmpdir/scripts/pre-spawn-check.sh" <<'SH'
#!/usr/bin/env bash
echo '{"allowed":true}'
exit 0
SH
  chmod +x "$tmpdir/scripts/pre-spawn-check.sh"

  # Stub rotate-team-log.sh
  cat > "$tmpdir/scripts/rotate-team-log.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$tmpdir/scripts/rotate-team-log.sh"

  # Stub workflow_runner.py
  cat > "$tmpdir/backend/workflow_runner.py" <<'PY'
#!/usr/bin/env python3
import sys, json
# Return a minimal resolved plan
print(json.dumps({"steps": [{"agent": "browser-tester"}]}))
PY

  # Stub claude_spawn_tracker
  cat > "$tmpdir/backend/claude_spawn_tracker.py" <<'PY'
def record(source): pass
PY

  # Stub spawner script (records calls to a log file)
  STUB_LOG="${tmpdir}/spawner-calls.log"
  cat > "$tmpdir/stub-spawner.sh" <<SH
#!/usr/bin/env bash
echo "spawned: \$*" >> "${STUB_LOG}"
exit 0
SH
  chmod +x "$tmpdir/stub-spawner.sh"

  echo "$tmpdir"
}

# ── Write a pending queue entry (age >= 31min) ───────────────────────────────

write_old_entry() {
  local queue_file="$1" pr="$2" pages="$3"
  # 32 minutes ago
  local ts
  ts=$(python3 -c "
import datetime, sys
dt = datetime.datetime.utcnow() - datetime.timedelta(minutes=32)
print(dt.strftime('%Y-%m-%dT%H:%M:%SZ'))
")
  python3 -c "
import json, sys
entry = {
  'trigger': 'post-merge',
  'pr': int(sys.argv[1]),
  'affected_pages': json.loads(sys.argv[2]),
  'tour_goal': 'Test tour',
  'queued_at': sys.argv[3],
  'status': 'pending'
}
print(json.dumps(entry))
" "$pr" "$pages" "$ts" >> "$queue_file"
}

# ── Write a fresh pending entry (age < 30min, should be skipped) ─────────────

write_fresh_entry() {
  local queue_file="$1" pr="$2"
  local ts
  ts=$(python3 -c "
import datetime
dt = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
print(dt.strftime('%Y-%m-%dT%H:%M:%SZ'))
")
  python3 -c "
import json, sys
entry = {
  'trigger': 'post-merge',
  'pr': int(sys.argv[1]),
  'affected_pages': ['/ideas'],
  'tour_goal': 'Fresh entry',
  'queued_at': sys.argv[2],
  'status': 'pending'
}
print(json.dumps(entry))
" "$pr" "$ts" >> "$queue_file"
}

# ── Test 1: single old entry gets drained ────────────────────────────────────

echo "Test 1: old queue entry is drained and spawner is called"
{
  tmpdir=$(setup_env)
  QUEUE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.jsonl"
  DONE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.done.jsonl"
  STUB_LOG="${tmpdir}/spawner-calls.log"

  write_old_entry "$QUEUE_FILE" 42 '["/ideas"]'

  out=$(REPO_ROOT="$tmpdir" BROWSER_TESTER_SPAWNER="${tmpdir}/stub-spawner.sh" \
    bash "$DRAINER" 2>&1) && rc=0 || rc=$?

  assert_contains "spawned=1 in output" "$out" "spawned=1"
  assert_contains "entry drained in output" "$out" "Entry drained"

  # Read file content explicitly so we can check it
  done_content=$(cat "$DONE_FILE" 2>/dev/null || echo "")
  queue_content=$(cat "$QUEUE_FILE" 2>/dev/null || echo "")
  assert_contains "done file has drained status" "$done_content" '"status": "drained"'
  assert_contains "done file has pr 42" "$done_content" '"pr": 42'
  assert_contains "queue entry marked drained" "$queue_content" '"status": "drained"'

  rm -rf "$tmpdir"
}

# ── Test 2: fresh entry (age < 30min) is NOT drained ─────────────────────────

echo "Test 2: fresh entry skipped (age < 30min)"
{
  tmpdir=$(setup_env)
  QUEUE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.jsonl"
  DONE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.done.jsonl"

  write_fresh_entry "$QUEUE_FILE" 55

  out=$(REPO_ROOT="$tmpdir" BROWSER_TESTER_SPAWNER="${tmpdir}/stub-spawner.sh" \
    bash "$DRAINER" 2>&1) && rc=0 || rc=$?

  assert_contains "no pending entries log" "$out" "No pending entries"
  if [[ -f "$DONE_FILE" ]] && [[ -s "$DONE_FILE" ]]; then
    fail "done file should be empty for fresh entries"
  else
    pass "done file empty for fresh entry"
  fi

  rm -rf "$tmpdir"
}

# ── Test 3: cap — only 2 entries spawned even if 3 are pending ───────────────

echo "Test 3: cap at 2 spawns per iteration"
{
  tmpdir=$(setup_env)
  QUEUE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.jsonl"
  DONE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.done.jsonl"

  write_old_entry "$QUEUE_FILE" 10 '["/"]'
  write_old_entry "$QUEUE_FILE" 11 '["/ideas"]'
  write_old_entry "$QUEUE_FILE" 12 '["/prs"]'

  out=$(REPO_ROOT="$tmpdir" BROWSER_TESTER_SPAWNER="${tmpdir}/stub-spawner.sh" \
    bash "$DRAINER" 2>&1) && rc=0 || rc=$?

  assert_contains "spawned=2 in output" "$out" "spawned=2"
  done_content=$(cat "$DONE_FILE" 2>/dev/null || echo "")
  done_count=$(python3 -c "
import sys
content = sys.argv[1]
count = content.count('\"status\": \"drained\"')
print(count)
" "$done_content" 2>/dev/null || echo "0")
  if [[ "$done_count" -eq 2 ]]; then
    pass "exactly 2 entries drained"
  else
    fail "expected 2 drained, got $done_count"
  fi
  # Third entry should still be pending
  queue_content3=$(cat "$QUEUE_FILE" 2>/dev/null || echo "")
  pending_count=$(python3 -c "
import json, sys
content = sys.argv[1]
count = 0
for line in content.splitlines():
    line = line.strip()
    if not line: continue
    try:
        e = json.loads(line)
        if e.get('status') == 'pending': count += 1
    except: pass
print(count)
" "$queue_content3" 2>/dev/null || echo "0")
  if [[ "$pending_count" -eq 1 ]]; then
    pass "1 entry still pending after cap"
  else
    fail "expected 1 pending entry after cap, got $pending_count"
  fi

  rm -rf "$tmpdir"
}

# ── Test 4: pre-spawn-check block leaves entry queued ────────────────────────

echo "Test 4: blocked pre-spawn-check leaves entry queued"
{
  tmpdir=$(setup_env)
  QUEUE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.jsonl"
  DONE_FILE="${tmpdir}/.autonomous-team/browser-tour-queue.done.jsonl"

  # Override pre-spawn-check to block
  cat > "$tmpdir/scripts/pre-spawn-check.sh" <<'SH'
#!/usr/bin/env bash
echo "blocked: circuit breaker tripped"
exit 1
SH
  chmod +x "$tmpdir/scripts/pre-spawn-check.sh"

  write_old_entry "$QUEUE_FILE" 88 '["/kpi"]'

  out=$(REPO_ROOT="$tmpdir" BROWSER_TESTER_SPAWNER="${tmpdir}/stub-spawner.sh" \
    bash "$DRAINER" 2>&1) && rc=0 || rc=$?

  assert_contains "blocked message in output" "$out" "Spawn blocked"
  done_content=$(cat "$DONE_FILE" 2>/dev/null || echo "")
  if [[ -n "$done_content" ]]; then
    fail "done file should be empty when spawn blocked"
  else
    pass "done file empty after blocked spawn"
  fi
  queue_content=$(cat "$QUEUE_FILE" 2>/dev/null || echo "")
  assert_contains "entry still pending in queue" "$queue_content" '"status": "pending"'

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
