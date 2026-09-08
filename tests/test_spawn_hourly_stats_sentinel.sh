#!/usr/bin/env bash
# tests/test_spawn_hourly_stats_sentinel.sh — verify spawn-hourly-stats.sh does
# NOT publish wasted_tokens_ratio / impersonation_rate / hard_rule_violation_count
# when the 24h window has no data to compute them from.
#
# Before D#2477 these three metrics wrote a fail-open initialiser (0.0) whenever
# the feed was empty or the window had no matching rows, which is
# indistinguishable downstream from a real measured zero. Fixed: an unmeasured
# window must skip the write entirely (the same shape as the fix_rounds_per_pr
# fix), and a real, examined zero must still be written.
#
# Isolation: the script always sets STATS_DB_PATH="" itself right before each
# write, which clears any STATS_DB_PATH a caller exported — so the only
# isolation lever that actually works here is AUTONOMOUS_TEAM_STATE_DIR (see
# backend/state_paths.py's STATS_DB_PATH-then-AUTONOMOUS_TEAM_STATE_DIR
# precedence). Every invocation below exports it to a fresh scratch dir before
# running the script — never rely on STATS_DB_PATH for this script.
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and a scratch state dir — no real GitHub API
# calls, no writes to the real .autonomous-team/ state or
# ~/.autonomous-forever-state/.
#
# Usage:
#   bash tests/test_spawn_hourly_stats_sentinel.sh
#
# Exits 0 if all tests pass, non-zero otherwise.

set -uo pipefail

REAL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REPO_ROOT="$(cd "$REAL_SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

# ── Build a synthetic REPO_ROOT so the script's hardcoded FEED / RUN_REPORTS_DIR
# paths (relative to its own location) never touch the real .autonomous-team/. ──
build_fixture() {
  local root="$1"
  mkdir -p "$root/scripts" "$root/.autonomous-team/run-reports"
  cp "$REAL_REPO_ROOT/scripts/spawn-hourly-stats.sh" "$root/scripts/spawn-hourly-stats.sh"
  ln -s "$REAL_REPO_ROOT/backend" "$root/backend"
}

metric_row_count() {
  local state_dir="$1" metric="$2"
  AF_DB="$state_dir/stats.duckdb" AF_METRIC="$metric" python3 -c "
import os
import duckdb
db = os.environ['AF_DB']
if not os.path.exists(db):
    print(0)
else:
    conn = duckdb.connect(db, read_only=True)
    n = conn.execute('SELECT COUNT(*) FROM metric_event WHERE metric = ?', [os.environ['AF_METRIC']]).fetchone()[0]
    print(n)
    conn.close()
"
}

metric_value() {
  local state_dir="$1" metric="$2"
  AF_DB="$state_dir/stats.duckdb" AF_METRIC="$metric" python3 -c "
import os
import duckdb
conn = duckdb.connect(os.environ['AF_DB'], read_only=True)
row = conn.execute('SELECT value FROM metric_event WHERE metric = ? ORDER BY ts DESC LIMIT 1', [os.environ['AF_METRIC']]).fetchone()
print(row[0] if row else '')
conn.close()
"
}

# ── Test 1: empty feed + empty run-reports dir -> no rows for any of the three ─
test_empty_window_skips_all_three() {
  local root state_dir
  root=$(mktemp -d)
  state_dir=$(mktemp -d)
  build_fixture "$root"
  : > "$root/.autonomous-team/agent-feed.jsonl"

  local out
  out=$(AUTONOMOUS_TEAM_STATE_DIR="$state_dir" STATS_DB_PATH="" bash "$root/scripts/spawn-hourly-stats.sh" 2>&1)
  local rc=$?

  if [[ $rc -ne 0 ]]; then
    fail "test_empty_window_skips_all_three" "script exited $rc: $out"
    rm -rf "$root" "$state_dir"
    return
  fi

  local ok=1
  for m in wasted_tokens_ratio impersonation_rate hard_rule_violation_count; do
    local n
    n=$(metric_row_count "$state_dir" "$m")
    if [[ "$n" != "0" ]]; then
      fail "test_empty_window_skips_all_three" "$m has $n row(s), expected 0 (empty window must not publish)"
      ok=0
    fi
  done
  [[ $ok -eq 1 ]] && pass "test_empty_window_skips_all_three"

  rm -rf "$root" "$state_dir"
}

# ── Test 2: real in-window signal -> all three write a value ──────────────────
test_real_signal_writes_values() {
  local root state_dir
  root=$(mktemp -d)
  state_dir=$(mktemp -d)
  build_fixture "$root"

  local now
  now=$(python3 -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())")

  cat > "$root/.autonomous-team/agent-feed.jsonl" <<EOF
{"ts": "${now}", "role": "executor", "event_type": "agent_end", "verdict": "needs-fix", "tokens": {"input": 100, "output": 100}}
{"ts": "${now}", "role": "executor", "event_type": "agent_end", "verdict": "pass", "tokens": {"input": 100, "output": 100}}
EOF

  cat > "$root/.autonomous-team/run-reports/fixture.json" <<EOF
{"report_at": "${now}", "findings": [{"category": "git_rm_usage"}]}
EOF

  local out
  out=$(AUTONOMOUS_TEAM_STATE_DIR="$state_dir" STATS_DB_PATH="" bash "$root/scripts/spawn-hourly-stats.sh" 2>&1)
  local rc=$?

  if [[ $rc -ne 0 ]]; then
    fail "test_real_signal_writes_values" "script exited $rc: $out"
    rm -rf "$root" "$state_dir"
    return
  fi

  local ok=1
  local n
  n=$(metric_row_count "$state_dir" "wasted_tokens_ratio")
  if [[ "$n" != "1" ]]; then
    fail "test_real_signal_writes_values" "wasted_tokens_ratio has $n row(s), expected 1"
    ok=0
  else
    local v
    v=$(metric_value "$state_dir" "wasted_tokens_ratio")
    [[ "$v" == "0.5" ]] || { fail "test_real_signal_writes_values" "wasted_tokens_ratio=$v, expected 0.5"; ok=0; }
  fi

  n=$(metric_row_count "$state_dir" "hard_rule_violation_count")
  if [[ "$n" != "1" ]]; then
    fail "test_real_signal_writes_values" "hard_rule_violation_count has $n row(s), expected 1"
    ok=0
  else
    local v
    v=$(metric_value "$state_dir" "hard_rule_violation_count")
    [[ "$v" == "1.0" ]] || { fail "test_real_signal_writes_values" "hard_rule_violation_count=$v, expected 1.0"; ok=0; }
  fi

  [[ $ok -eq 1 ]] && pass "test_real_signal_writes_values"

  rm -rf "$root" "$state_dir"
}

echo "== test_spawn_hourly_stats_sentinel =="
test_empty_window_skips_all_three
test_real_signal_writes_values

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
exit 0
