#!/usr/bin/env bash
# tests/test_spawn_hourly_stats.sh — impersonation_rate denominator (D#2501 PR-a)
# and the heartbeat liveness signal (D#2501 PR-b).
#
# scripts/spawn-hourly-stats.sh:135 used to read:
#   if e.get('role') == 'executor' and e.get('event_type') in ('agent_end', 'merge'):
#
# The 'merge' arm was dead: merge events are always appended by
# post-merge-hook.sh with role="merge" (never role="executor" — a merge is a
# Team Lead action on a PR, not an executor run), so that AND could never be
# satisfied by any row the feed actually produces. This test proves the arm
# is gone by constructing a row shape the dead arm WAS written to match
# (role="executor", event_type="merge") and asserting it is excluded from the
# impersonation_rate denominator, alongside real role="merge" rows (which
# were always excluded, dead arm or not).
#
# PR-b's tests cover the heartbeat: scripts/spawn-hourly-stats.sh writes an
# unconditional spawn_hourly_stats_heartbeat row before any of the three
# measurement guards, so "the job did not run" and "the job ran and found
# nothing" are no longer the same observable (no rows). See
# test_heartbeat_written_when_all_metrics_skip and test_heartbeat_three_states
# below. test_heartbeat_written_when_all_metrics_skip is the binding
# assertion: if the heartbeat write were made conditional on having measured
# something (the regression this item exists to prevent), that test fails
# because the fixture there measures nothing. Verified by hand both
# directions — real code passes, then wrapping the emit_metric call in
# `if [[ -n "$WASTED_RATIO" || -n "$IMPERSONATION_RATE" || -n "$VIOLATION_COUNT" ]]`
# makes it fail — and reported in the PR body rather than committed as a
# mutation harness, matching PR-a's precedent above.
#
# Isolation: same as tests/test_spawn_hourly_stats_sentinel.sh — the script
# always sets STATS_DB_PATH="" itself right before each write, so
# AUTONOMOUS_TEAM_STATE_DIR is the only isolation lever that works here.
#
# HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
# Tests use synthetic inputs and a scratch state dir — no real GitHub API
# calls, no writes to the real .autonomous-team/ state or
# ~/.autonomous-forever-state/.
#
# Usage:
#   bash tests/test_spawn_hourly_stats.sh
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

# ── Build a synthetic REPO_ROOT so the script's hardcoded FEED / RETROS paths
# (relative to its own location) never touch the real .autonomous-team/. ──────
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

# ── Test: merge-shaped rows (real role='merge' rows, AND the exact
# role='executor'+event_type='merge' shape the dead arm was written to catch)
# are excluded from the impersonation_rate denominator; only executor
# agent_end rows count. ─────────────────────────────────────────────────────
test_merge_rows_excluded_from_denominator() {
  local root state_dir
  root=$(mktemp -d)
  state_dir=$(mktemp -d)
  build_fixture "$root"

  local now
  now=$(python3 -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())")

  # 2 executor agent_end rows -> impl_runs should be 2.
  # 1 row shaped role='executor', event_type='merge' -> the exact shape the
  #   old dead arm matched on; must NOT be counted.
  # 2 real merge events (role='merge', event_type='merge', as post-merge-hook.sh
  #   actually writes them) -> must NOT be counted either.
  cat > "$root/.autonomous-team/agent-feed.jsonl" <<EOF
{"ts": "${now}", "role": "executor", "event_type": "agent_end", "verdict": "done"}
{"ts": "${now}", "role": "executor", "event_type": "agent_end", "verdict": "done"}
{"ts": "${now}", "role": "executor", "event_type": "merge", "verdict": "done"}
{"ts": "${now}", "role": "merge", "event_type": "merge", "message": "merged PR #1"}
{"ts": "${now}", "role": "merge", "event_type": "merge", "message": "merged PR #2"}
EOF

  # 1 skipped finding -> skipped=1. With impl_runs=2 (executor agent_end only)
  # the rate must be exactly 0.5. If the dead arm were still counting the
  # role='executor'/event_type='merge' row, impl_runs would be 3 and the rate
  # would be 0.3333 instead — the two are cleanly distinguishable.
  cat > "$root/.autonomous-team/agent-retros.jsonl" <<EOF
{"ts": "${now}", "classifier": "reviewer_skipped_by_impl_coord"}
EOF

  local out
  out=$(AUTONOMOUS_TEAM_STATE_DIR="$state_dir" STATS_DB_PATH="" bash "$root/scripts/spawn-hourly-stats.sh" 2>&1)
  local rc=$?

  if [[ $rc -ne 0 ]]; then
    fail "test_merge_rows_excluded_from_denominator" "script exited $rc: $out"
    rm -rf "$root" "$state_dir"
    return
  fi

  local ok=1
  local n
  n=$(metric_row_count "$state_dir" "impersonation_rate")
  if [[ "$n" != "1" ]]; then
    fail "test_merge_rows_excluded_from_denominator" "impersonation_rate has $n row(s), expected 1"
    ok=0
  else
    local v
    v=$(metric_value "$state_dir" "impersonation_rate")
    if [[ "$v" != "0.5" ]]; then
      fail "test_merge_rows_excluded_from_denominator" \
        "impersonation_rate=$v, expected 0.5 (skipped=1 / impl_runs=2 executor-agent_end-only). A value of 0.3333 means merge-shaped rows are still leaking into the denominator."
      ok=0
    fi
  fi

  [[ $ok -eq 1 ]] && pass "test_merge_rows_excluded_from_denominator"

  rm -rf "$root" "$state_dir"
}

# ── Test: the heartbeat is written even when all three metrics skip — the
# exact scenario a heartbeat conditional on "having measured something"
# would fail. Empty feed, no retros, no run-reports: every one of the three
# metric skip branches fires, and the heartbeat must still be the one row
# that lands. ─────────────────────────────────────────────────────────────
test_heartbeat_written_when_all_metrics_skip() {
  local root state_dir
  root=$(mktemp -d)
  state_dir=$(mktemp -d)
  build_fixture "$root"
  # No agent-feed.jsonl, no agent-retros.jsonl, empty run-reports/ — nothing
  # for any of the three metrics to measure.

  local out rc
  out=$(AUTONOMOUS_TEAM_STATE_DIR="$state_dir" STATS_DB_PATH="" bash "$root/scripts/spawn-hourly-stats.sh" 2>&1)
  rc=$?

  if [[ $rc -ne 0 ]]; then
    fail "test_heartbeat_written_when_all_metrics_skip" "script exited $rc: $out"
    rm -rf "$root" "$state_dir"
    return
  fi

  local ok=1
  local hb
  hb=$(metric_row_count "$state_dir" "spawn_hourly_stats_heartbeat")
  if [[ "$hb" != "1" ]]; then
    fail "test_heartbeat_written_when_all_metrics_skip" "spawn_hourly_stats_heartbeat has $hb row(s), expected 1 — a heartbeat that skips alongside the metrics reintroduces the exact ambiguity D#2501 exists to remove"
    ok=0
  fi
  local m
  for m in wasted_tokens_ratio impersonation_rate hard_rule_violation_count; do
    local n
    n=$(metric_row_count "$state_dir" "$m")
    if [[ "$n" != "0" ]]; then
      fail "test_heartbeat_written_when_all_metrics_skip" "$m has $n row(s), expected 0 on an empty window"
      ok=0
    fi
  done

  [[ $ok -eq 1 ]] && pass "test_heartbeat_written_when_all_metrics_skip"

  rm -rf "$root" "$state_dir"
}

# ── Test: the three-state test the Spec requires, not the two-state test it
# explicitly warns passes on a dead job. (a) ran-and-measured → heartbeat +
# metric rows. (b) ran-and-found-nothing → heartbeat, no metric rows. (c)
# did-not-run → no heartbeat. Each state gets its own isolated root/state_dir
# so they cannot leak into each other. ──────────────────────────────────────
test_heartbeat_three_states() {
  local ok=1

  # (a) ran-and-measured
  local root_a state_a
  root_a=$(mktemp -d)
  state_a=$(mktemp -d)
  build_fixture "$root_a"
  local now
  now=$(python3 -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())")
  cat > "$root_a/.autonomous-team/agent-feed.jsonl" <<EOF
{"ts": "${now}", "role": "executor", "event_type": "agent_end", "verdict": "done", "tokens": {"input": 10, "output": 5}}
EOF
  AUTONOMOUS_TEAM_STATE_DIR="$state_a" STATS_DB_PATH="" bash "$root_a/scripts/spawn-hourly-stats.sh" >/dev/null 2>&1
  local hb_a wt_a
  hb_a=$(metric_row_count "$state_a" "spawn_hourly_stats_heartbeat")
  wt_a=$(metric_row_count "$state_a" "wasted_tokens_ratio")
  if [[ "$hb_a" != "1" || "$wt_a" != "1" ]]; then
    fail "test_heartbeat_three_states" "(a) ran-and-measured: heartbeat=$hb_a (want 1), wasted_tokens_ratio=$wt_a (want 1)"
    ok=0
  fi
  rm -rf "$root_a" "$state_a"

  # (b) ran-and-found-nothing
  local root_b state_b
  root_b=$(mktemp -d)
  state_b=$(mktemp -d)
  build_fixture "$root_b"
  AUTONOMOUS_TEAM_STATE_DIR="$state_b" STATS_DB_PATH="" bash "$root_b/scripts/spawn-hourly-stats.sh" >/dev/null 2>&1
  local hb_b wt_b
  hb_b=$(metric_row_count "$state_b" "spawn_hourly_stats_heartbeat")
  wt_b=$(metric_row_count "$state_b" "wasted_tokens_ratio")
  if [[ "$hb_b" != "1" || "$wt_b" != "0" ]]; then
    fail "test_heartbeat_three_states" "(b) ran-and-found-nothing: heartbeat=$hb_b (want 1), wasted_tokens_ratio=$wt_b (want 0)"
    ok=0
  fi
  rm -rf "$root_b" "$state_b"

  # (c) did-not-run — the script is never invoked; the state dir stays empty.
  local state_c
  state_c=$(mktemp -d)
  local hb_c
  hb_c=$(metric_row_count "$state_c" "spawn_hourly_stats_heartbeat")
  if [[ "$hb_c" != "0" ]]; then
    fail "test_heartbeat_three_states" "(c) did-not-run: heartbeat=$hb_c (want 0)"
    ok=0
  fi
  rm -rf "$state_c"

  [[ $ok -eq 1 ]] && pass "test_heartbeat_three_states"
}

# ── Test: staleness is readable from the heartbeat alone via the existing
# freshness watchdog (backend/stats_freshness_watchdog.py), which ages any
# metric_event row by MAX(ts) — no bespoke staleness code needed, just the
# registered_metrics() entry added in backend/stats_writer.py. A heartbeat
# written 3h ago crosses WARN_AGE_SECONDS (2h); one written now does not —
# the two are distinguishable through the same reader. ─────────────────────
test_heartbeat_staleness_distinguishable() {
  local state_stale state_fresh
  state_stale=$(mktemp -d)
  state_fresh=$(mktemp -d)

  AUTONOMOUS_TEAM_STATE_DIR="$state_stale" AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe" \
    python3 -c "
import sys, datetime
sys.path.insert(0, '$REAL_REPO_ROOT')
from backend import stats_writer
stale_ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
stats_writer.record('spawn_hourly_stats_heartbeat', 1.0, 'count', tags={}, source='spawn-hourly-stats', ts=stale_ts)
" >/dev/null 2>&1

  AUTONOMOUS_TEAM_STATE_DIR="$state_fresh" AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe" \
    python3 -c "
import sys
sys.path.insert(0, '$REAL_REPO_ROOT')
from backend import stats_writer
stats_writer.record('spawn_hourly_stats_heartbeat', 1.0, 'count', tags={}, source='spawn-hourly-stats')
" >/dev/null 2>&1

  local stale_age fresh_age
  stale_age=$(AUTONOMOUS_TEAM_STATE_DIR="$state_stale" AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe" python3 -c "
import sys
sys.path.insert(0, '$REAL_REPO_ROOT')
from backend import stats_freshness_watchdog as w
rows = [r for r in w.check() if r['metric_name'] == 'spawn_hourly_stats_heartbeat']
print(rows[0]['age_seconds'] if rows else -1)
" 2>/dev/null)
  fresh_age=$(AUTONOMOUS_TEAM_STATE_DIR="$state_fresh" AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe" python3 -c "
import sys
sys.path.insert(0, '$REAL_REPO_ROOT')
from backend import stats_freshness_watchdog as w
rows = [r for r in w.check() if r['metric_name'] == 'spawn_hourly_stats_heartbeat']
print(rows[0]['age_seconds'] if rows else -1)
" 2>/dev/null)

  local ok=1
  # WARN_AGE_SECONDS is 7200 (2h) — a 3h-old heartbeat must clear it, a
  # just-written one must not.
  if [[ -z "$stale_age" || "$stale_age" -lt 7200 ]]; then
    fail "test_heartbeat_staleness_distinguishable" "stale heartbeat age_seconds=$stale_age, expected >= 7200 (3h old)"
    ok=0
  fi
  if [[ -z "$fresh_age" || "$fresh_age" -ge 7200 ]]; then
    fail "test_heartbeat_staleness_distinguishable" "fresh heartbeat age_seconds=$fresh_age, expected < 7200 (just written)"
    ok=0
  fi

  [[ $ok -eq 1 ]] && pass "test_heartbeat_staleness_distinguishable"

  rm -rf "$state_stale" "$state_fresh"
}

echo "== test_spawn_hourly_stats =="
test_merge_rows_excluded_from_denominator
test_heartbeat_written_when_all_metrics_skip
test_heartbeat_three_states
test_heartbeat_staleness_distinguishable

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
exit 0
