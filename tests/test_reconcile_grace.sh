#!/usr/bin/env bash
# tests/test_reconcile_grace.sh — verify reconcile_grace_window() picks the
# right agent_run reconcile grace window based on SubagentStop hook presence.
#
# D#1655: interactive Claude Code sessions never get end_ts written (no
# SubagentStop hook wired), so spawn-agent.sh's cap-check reconcile must use
# a short grace window there instead of the 30-min loop default.
#
# Tests 1-3 (fixture settings.local.json files, no reliance on the machine's
# real .claude/settings.local.json):
#   1. Hook PRESENT  -> reconcile_grace_window prints 30 (loop default)
#   2. Hook ABSENT   -> reconcile_grace_window prints 5  (interactive default)
#   3. Missing path  -> reconcile_grace_window prints 5  (fail-safe)
#
# D#2107: those three exercise only the detection logic given a fixture
# path — they never exercise which path the real caller hands in, which is
# exactly how the wiring bug (settings.local.json instead of settings.json)
# survived them. Tests 4-8 below are integration-level: they call the
# function the way spawn-agent.sh actually does, including against this
# repo's real, on-disk .claude/ layout.
#
# D#2131: registration alone no longer grants the loop window (see
# reconcile-grace.sh). Tests 1, 4, 5, 7 point STATS_DB_PATH at a small
# "hook is live" fixture DB so they keep testing registration wiring,
# decoupled from real production history. Tests 9-12 are new: they exercise
# the liveness check itself.
#
# Exit 0 on pass, non-zero on any failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$REPO_ROOT/scripts/lib/reconcile-grace.sh"

PASS=0
FAIL=0

check() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    echo "PASS: $desc (expected=$expected actual=$actual)"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $desc (expected=$expected actual=$actual)"
    FAIL=$((FAIL + 1))
  fi
}

# _mk_agent_run_fixture <db_path> <hook_closed_count> <non_hook_count>
# Builds a minimal agent_run table (agent_id, verdict, end_ts) with
# <hook_closed_count> hook-plausible rows and <non_hook_count> rows carrying
# a known non-hook verdict ("reconciled-stale"). Callers point STATS_DB_PATH
# at the returned file; never touches the real stats.duckdb.
_mk_agent_run_fixture() {
  local db_path="$1" hook_closed="$2" non_hook="$3"
  python3 - "$db_path" "$hook_closed" "$non_hook" <<'PYEOF'
import sys
from datetime import datetime, timedelta, timezone

import duckdb

db_path, hook_closed, non_hook = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
conn = duckdb.connect(db_path)
conn.execute(
    "CREATE TABLE agent_run (agent_id VARCHAR PRIMARY KEY, verdict VARCHAR, end_ts TIMESTAMPTZ)"
)
now = datetime.now(timezone.utc)
i = 0
for _ in range(hook_closed):
    i += 1
    conn.execute(
        "INSERT INTO agent_run VALUES (?, ?, ?)",
        [f"hook-{i}", "done", now - timedelta(minutes=i)],
    )
for _ in range(non_hook):
    i += 1
    conn.execute(
        "INSERT INTO agent_run VALUES (?, ?, ?)",
        [f"nonhook-{i}", "reconciled-stale", now - timedelta(minutes=i)],
    )
conn.close()
PYEOF
}

TEST_DIR=$(mktemp -d /tmp/test-reconcile-grace-XXXXXX)
cleanup() { rm -rf "$TEST_DIR"; }
trap cleanup EXIT

# "Hook is live" fixture DB (all recent closed rows hook-closed), used by the
# pre-existing registration tests below — see D#2131 comment at file top.
LIVE_DB="$TEST_DIR/live-stats.duckdb"
_mk_agent_run_fixture "$LIVE_DB" 5 0

# ── Fixture 1: SubagentStop hook registered (loop path) ──────────────────────
HOOK_PRESENT="$TEST_DIR/settings-hook-present.json"
cat > "$HOOK_PRESENT" <<'JSON'
{
  "hooks": {
    "SubagentStop": [
      {
        "hooks": [
          { "type": "command", "command": "scripts/post-agent-hook.sh" }
        ]
      }
    ]
  }
}
JSON

# ── Fixture 2: SubagentStop hook absent (interactive path) ───────────────────
HOOK_ABSENT="$TEST_DIR/settings-hook-absent.json"
cat > "$HOOK_ABSENT" <<'JSON'
{
  "hooks": {}
}
JSON

# ── Fixture 3: missing path entirely ──────────────────────────────────────────
MISSING_PATH="$TEST_DIR/does-not-exist.json"

echo "--- Test 1: hook present -> 30 ---"
result_present=$(STATS_DB_PATH="$LIVE_DB" reconcile_grace_window "$HOOK_PRESENT")
check "hook present returns loop default" "30" "$result_present"

echo "--- Test 2: hook absent -> 5 ---"
result_absent=$(reconcile_grace_window "$HOOK_ABSENT")
check "hook absent returns interactive default" "5" "$result_absent"

echo "--- Test 3: missing settings path -> 5 (fail-safe) ---"
result_missing=$(reconcile_grace_window "$MISSING_PATH")
rc_missing=$?
check "missing path returns interactive default" "5" "$result_missing"
if [[ $rc_missing -ne 0 ]]; then
  echo "FAIL: missing path caused non-zero exit ($rc_missing)"
  FAIL=$((FAIL + 1))
else
  echo "PASS: missing path exits 0"
  PASS=$((PASS + 1))
fi

echo "--- Test 4 (Criterion 1): production wiring on THIS repo's real settings -> 30 ---"
# This is the discriminating test: it calls reconcile_grace_window with the
# exact argument spawn-agent.sh:247 uses, against the real, on-disk
# .claude/settings.json (tracked, registers SubagentStop) and
# .claude/settings.local.json (untracked, absent from this worktree) — i.e.
# this repo's actual shape. A version of this check that instead hands the
# function a hand-built fixture path (like Tests 1-3 above) does not count:
# that shape is already green on the broken tree.
result_prod=$(STATS_DB_PATH="$LIVE_DB" reconcile_grace_window "$REPO_ROOT/.claude/settings.local.json")
check "production wiring resolves loop window from real on-disk settings" "30" "$result_prod"

echo "--- Test 5 (Criterion 2): hook registered only in settings.local.json -> 30 ---"
FIX_EITHER_DIR="$TEST_DIR/fixture-either/.claude"
mkdir -p "$FIX_EITHER_DIR"
cat > "$FIX_EITHER_DIR/settings.local.json" <<'JSON'
{
  "hooks": {
    "SubagentStop": [
      { "hooks": [ { "type": "command", "command": "scripts/post-agent-hook.sh" } ] }
    ]
  }
}
JSON
cat > "$FIX_EITHER_DIR/settings.json" <<'JSON'
{ "hooks": {} }
JSON
result_either=$(STATS_DB_PATH="$LIVE_DB" reconcile_grace_window "$FIX_EITHER_DIR/settings.local.json")
check "hook registered only in settings.local.json still resolves loop window" "30" "$result_either"

echo "--- Test 6 (Criterion 3): neither file registers (both present) -> 5 ---"
FIX_NEITHER_DIR="$TEST_DIR/fixture-neither/.claude"
mkdir -p "$FIX_NEITHER_DIR"
cat > "$FIX_NEITHER_DIR/settings.local.json" <<'JSON'
{ "hooks": {} }
JSON
cat > "$FIX_NEITHER_DIR/settings.json" <<'JSON'
{ "hooks": {} }
JSON
result_neither=$(reconcile_grace_window "$FIX_NEITHER_DIR/settings.local.json")
check "neither settings file registering the hook resolves interactive window" "5" "$result_neither"

echo "--- Test 7 (Criterion 4): choice is logged on stderr ---"
stderr_present=$(STATS_DB_PATH="$LIVE_DB" reconcile_grace_window "$HOOK_PRESENT" 2>&1 1>/dev/null)
if echo "$stderr_present" | grep -q "window=30"; then
  echo "PASS: stderr names the chosen window (hook present)"
  PASS=$((PASS + 1))
else
  echo "FAIL: stderr did not name the chosen window (hook present): $stderr_present"
  FAIL=$((FAIL + 1))
fi
stderr_absent=$(reconcile_grace_window "$HOOK_ABSENT" 2>&1 1>/dev/null)
if echo "$stderr_absent" | grep -q "window=5"; then
  echo "PASS: stderr names the chosen window (hook absent)"
  PASS=$((PASS + 1))
else
  echo "FAIL: stderr did not name the chosen window (hook absent): $stderr_absent"
  FAIL=$((FAIL + 1))
fi

echo "--- Test 8 (Criterion 8): malformed JSON -> 5, exit 0 ---"
MALFORMED_PATH="$TEST_DIR/malformed.json"
echo '{not valid json' > "$MALFORMED_PATH"
result_malformed=$(reconcile_grace_window "$MALFORMED_PATH")
rc_malformed=$?
check "malformed settings file returns interactive default" "5" "$result_malformed"
if [[ $rc_malformed -ne 0 ]]; then
  echo "FAIL: malformed settings file caused non-zero exit ($rc_malformed)"
  FAIL=$((FAIL + 1))
else
  echo "PASS: malformed settings file exits 0"
  PASS=$((PASS + 1))
fi

echo "--- Test 9 (Spec item 2): registered but dead -> 5 ---"
# All recent closed rows carry a non-hook verdict — the hook is registered
# but nothing in recent history shows it closing rows.
DEAD_DB="$TEST_DIR/dead-stats.duckdb"
_mk_agent_run_fixture "$DEAD_DB" 0 5
result_dead=$(STATS_DB_PATH="$DEAD_DB" reconcile_grace_window "$HOOK_PRESENT")
check "registered-but-dead history resolves interactive window" "5" "$result_dead"

echo "--- Test 10 (Spec item 3): registered and live -> 30 ---"
# Recent closed rows are hook-closed at/above the default 0.5 threshold
# (3 hook-closed of 5 total = 0.6).
LIVE_ABOVE_DB="$TEST_DIR/live-above-stats.duckdb"
_mk_agent_run_fixture "$LIVE_ABOVE_DB" 3 2
result_live=$(STATS_DB_PATH="$LIVE_ABOVE_DB" reconcile_grace_window "$HOOK_PRESENT")
check "registered-and-live history resolves loop window" "30" "$result_live"

echo "--- Test 11 (Spec item 4): unreadable history fails to the short window ---"
# Point STATS_DB_PATH at a file that does not exist — the liveness query
# must fail open to the short window, exit 0, with no traceback on stderr.
MISSING_DB="$TEST_DIR/does-not-exist.duckdb"
stderr_unreadable=$(STATS_DB_PATH="$MISSING_DB" reconcile_grace_window "$HOOK_PRESENT" 2>&1 1>/dev/null)
result_unreadable=$(STATS_DB_PATH="$MISSING_DB" reconcile_grace_window "$HOOK_PRESENT" 2>/dev/null)
rc_unreadable=$?
check "unreadable agent_run history resolves interactive window" "5" "$result_unreadable"
if [[ $rc_unreadable -ne 0 ]]; then
  echo "FAIL: unreadable history caused non-zero exit ($rc_unreadable)"
  FAIL=$((FAIL + 1))
else
  echo "PASS: unreadable history exits 0"
  PASS=$((PASS + 1))
fi
if echo "$stderr_unreadable" | grep -qi "traceback"; then
  echo "FAIL: unreadable history leaked a Python traceback to stderr: $stderr_unreadable"
  FAIL=$((FAIL + 1))
else
  echo "PASS: unreadable history emitted no traceback"
  PASS=$((PASS + 1))
fi

echo "--- Test 12 (Spec item 5): stderr line carries the measured ratio and threshold ---"
# Reuse the registered-but-dead fixture from Test 9 — its stderr line must
# name the ratio it measured and the threshold it compared against, not
# just the settings file it found.
stderr_dead=$(STATS_DB_PATH="$DEAD_DB" reconcile_grace_window "$HOOK_PRESENT" 2>&1 1>/dev/null)
if echo "$stderr_dead" | grep -Eq '[0-9]+/[0-9]+ recent closed rows hook-closed \(threshold [0-9.]+\)'; then
  echo "PASS: stderr names the measured ratio and threshold: $stderr_dead"
  PASS=$((PASS + 1))
else
  echo "FAIL: stderr did not name the measured ratio and threshold: $stderr_dead"
  FAIL=$((FAIL + 1))
fi

echo "--- Test 13 (D#2232): NULL-verdict closed rows are not counted as hook-closed ---"
# is_agent_reported(None) is False, so a closed row with no verdict at all
# (end_ts set, verdict NULL) must NOT count toward the hook-closed ratio —
# this is a deliberate behavior change from the old inline check, which
# treated NULL as "not a non-hook verdict" and therefore hook-closed.
NULL_VERDICT_DB="$TEST_DIR/null-verdict-stats.duckdb"
python3 - "$NULL_VERDICT_DB" <<'PYEOF'
import sys
from datetime import datetime, timedelta, timezone

import duckdb

db_path = sys.argv[1]
conn = duckdb.connect(db_path)
conn.execute(
    "CREATE TABLE agent_run (agent_id VARCHAR PRIMARY KEY, verdict VARCHAR, end_ts TIMESTAMPTZ)"
)
now = datetime.now(timezone.utc)
for i in range(5):
    conn.execute(
        "INSERT INTO agent_run VALUES (?, NULL, ?)",
        [f"null-verdict-{i}", now - timedelta(minutes=i)],
    )
conn.close()
PYEOF
result_null_verdict=$(STATS_DB_PATH="$NULL_VERDICT_DB" reconcile_grace_window "$HOOK_PRESENT")
check "all-NULL-verdict closed rows resolve interactive window (not hook-closed)" "5" "$result_null_verdict"

stderr_null_verdict=$(STATS_DB_PATH="$NULL_VERDICT_DB" reconcile_grace_window "$HOOK_PRESENT" 2>&1 1>/dev/null)
if echo "$stderr_null_verdict" | grep -q '0/5 recent closed rows hook-closed'; then
  echo "PASS: stderr shows 0/5 hook-closed for all-NULL-verdict rows"
  PASS=$((PASS + 1))
else
  echo "FAIL: stderr did not show 0/5 hook-closed for all-NULL-verdict rows: $stderr_null_verdict"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== test_reconcile_grace.sh: $PASS passed, $FAIL failed ==="

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
