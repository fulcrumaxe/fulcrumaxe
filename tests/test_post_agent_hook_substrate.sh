#!/usr/bin/env bash
# tests/test_post_agent_hook_substrate.sh
#
# Integration test: verifies the pending→done lifecycle via post-agent-hook.sh.
#
# What this tests:
#   1. Pre-create a task record with status=pending (spawn-time write)
#   2. Create a pre-spawn-check done marker with the SAME event ID
#      (this was the collision that caused post-agent-hook to skip all work)
#   3. Call post-agent-hook.sh with --verdict pass
#   4. Assert task record has status=pass, created_at preserved, task_id preserved
#
# Usage:
#   bash tests/test_post_agent_hook_substrate.sh
#
# Exit code:
#   0 — all assertions passed
#   1 — at least one assertion failed

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# ── Isolated temp dirs ────────────────────────────────────────────────────────
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

export CLAUDE_TASKS_DIR="$WORK_DIR/tasks"
export CLAUDE_TEAMS_DIR="$WORK_DIR/teams"
export HOOK_EVENT_DIR="$WORK_DIR/hook-events"
mkdir -p "$CLAUDE_TASKS_DIR/autonomous-forever"
mkdir -p "$HOOK_EVENT_DIR/done"

TASK_ID="pah-lifecycle-test-$$"

# ── Step 1: Spawn-time write (status=pending) ─────────────────────────────────
python3 - <<PYEOF
import sys, json
from datetime import datetime, timezone
from pathlib import Path

tasks_dir = Path("$CLAUDE_TASKS_DIR/autonomous-forever")
task_path = tasks_dir / "${TASK_ID}.json"
record = {
    "task_id": "$TASK_ID",
    "status": "pending",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "owner": "executor",
    "discussion": "907",
}
task_path.write_text(json.dumps(record, indent=2))
PYEOF

# Capture created_at from the pending record
CREATED_AT=$(python3 -c "
import json
from pathlib import Path
p = Path('$CLAUDE_TASKS_DIR/autonomous-forever/${TASK_ID}.json')
print(json.loads(p.read_text())['created_at'])
")

# ── Step 2: Simulate pre-spawn-check done marker (the collision scenario) ─────
python3 - <<PYEOF
import json
from pathlib import Path

done_dir = Path("$HOOK_EVENT_DIR/done")
marker = {
    "event_id": "$TASK_ID",
    "hook": "pre-spawn-check",
    "started_at": "2026-05-15T21:00:00Z",
    "inputs": {"role": "executor", "discussion": 907},
    "steps_total": ["agent_feed", "budget_check", "circuit_breaker_check"],
    "steps_completed": ["agent_feed", "budget_check", "circuit_breaker_check"],
}
(done_dir / "${TASK_ID}.json").write_text(json.dumps(marker, indent=2))
PYEOF

# ── Step 3: Call post-agent-hook.sh ──────────────────────────────────────────
PAH_OUT=$(bash "$REPO_ROOT/scripts/post-agent-hook.sh" \
  --event-id  "$TASK_ID" \
  --role      executor \
  --discussion 907 \
  --verdict   pass \
  2>&1)
PAH_EXIT=$?

# ── Step 4: Assertions ────────────────────────────────────────────────────────

# 4a. Hook must exit 0
if [[ "$PAH_EXIT" -eq 0 ]]; then
  _pass "post-agent-hook.sh exited 0"
else
  _fail "post-agent-hook.sh exited $PAH_EXIT (output: $PAH_OUT)"
fi

# 4b. Hook must NOT have short-circuited (the old bug was: event already complete — no-op exit)
if echo "$PAH_OUT" | grep -q "already complete"; then
  _fail "hook short-circuited on pre-spawn done marker (old collision bug still present)"
else
  _pass "hook did not short-circuit on pre-spawn done marker"
fi

# 4c. task record must exist
TASK_FILE="$CLAUDE_TASKS_DIR/autonomous-forever/${TASK_ID}.json"
if [[ -f "$TASK_FILE" ]]; then
  _pass "task record file exists"
else
  _fail "task record file missing at $TASK_FILE"
fi

# 4d. status must be 'pass'
STATUS=$(python3 -c "import json; print(json.load(open('$TASK_FILE'))['status'])" 2>/dev/null || echo "ERROR")
if [[ "$STATUS" == "pass" ]]; then
  _pass "task status updated to 'pass'"
else
  _fail "task status is '$STATUS' (expected 'pass')"
fi

# 4e. created_at must be preserved
CREATED_AT_AFTER=$(python3 -c "import json; print(json.load(open('$TASK_FILE'))['created_at'])" 2>/dev/null || echo "MISSING")
if [[ "$CREATED_AT_AFTER" == "$CREATED_AT" ]]; then
  _pass "created_at preserved across update"
else
  _fail "created_at changed: before='$CREATED_AT' after='$CREATED_AT_AFTER'"
fi

# 4f. task_id must be preserved
TASK_ID_AFTER=$(python3 -c "import json; print(json.load(open('$TASK_FILE'))['task_id'])" 2>/dev/null || echo "MISSING")
if [[ "$TASK_ID_AFTER" == "$TASK_ID" ]]; then
  _pass "task_id preserved"
else
  _fail "task_id changed: before='$TASK_ID' after='$TASK_ID_AFTER'"
fi

# 4g. updated_at must be set
UPDATED_AT=$(python3 -c "import json; d=json.load(open('$TASK_FILE')); print(d.get('updated_at','MISSING'))" 2>/dev/null || echo "MISSING")
if [[ "$UPDATED_AT" != "MISSING" ]]; then
  _pass "updated_at set on completion"
else
  _fail "updated_at not set after completion update"
fi

# 4h. post-agent-hook done marker uses -pah suffix (not raw ID)
PAH_DONE="$HOOK_EVENT_DIR/done/${TASK_ID}-pah.json"
if [[ -f "$PAH_DONE" ]]; then
  _pass "post-agent-hook marker uses -pah suffix (no collision with pre-spawn marker)"
else
  _fail "post-agent-hook marker not found at $PAH_DONE (suffix fix may not be active)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
