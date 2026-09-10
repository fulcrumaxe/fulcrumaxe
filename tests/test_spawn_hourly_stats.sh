#!/usr/bin/env bash
# tests/test_spawn_hourly_stats.sh — impersonation_rate denominator (D#2501 PR-a)
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

echo "== test_spawn_hourly_stats =="
test_merge_rows_excluded_from_denominator

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
exit 0
