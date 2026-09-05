#!/usr/bin/env bash
# tests/test_no_unisolated_spawn_invocations.sh — D#1982 guard.
#
# Every test that executes a copy of the spawn script (the SPAWN_COPY
# copy-then-run convention every existing spawn-exercising test in this
# directory already uses — see test_spawn_agent_includes_template_body.sh
# for the canonical shape) must keep its start_run writes out of the
# production stats DB. Without isolation, agent_run_tracker.py resolves
# STATS_DB to ~/.autonomous-forever-state/stats.duckdb and the spawn
# concurrency cap counts the resulting rows as live agents (D#1982).
#
# Rule: any file under tests/ with a line invoking the SPAWN_COPY variable
# must also contain at least one of:
#   --no-register        (smoke invocation — no Agent() call follows)
#   STATS_DB_PATH=        (redirects agent_run_tracker.py to a scratch DB)
#   REPO_ROOT="$TEST_DIR"  (spawn script's own DB fallback path never resolves)
#
# Run: bash tests/test_no_unisolated_spawn_invocations.sh
# Exits 0 when every spawn-executing file is isolated; otherwise exits 1 and
# names each offending file plus this rule.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$(basename "${BASH_SOURCE[0]}")"
INVOKE_MARKER='SPAWN_COPY'
RULE='must contain --no-register, STATS_DB_PATH=, or REPO_ROOT="$TEST_DIR"'

FAIL=0

while IFS= read -r -d '' f; do
  rel="${f#"$SCRIPT_DIR"/}"
  [[ "$rel" == "$SELF" ]] && continue

  if grep -qF "\$${INVOKE_MARKER}" "$f" 2>/dev/null; then
    if ! grep -qE -- '--no-register|STATS_DB_PATH=|REPO_ROOT="\$TEST_DIR"' "$f" 2>/dev/null; then
      echo "FAIL: tests/$rel executes the spawn script but is not isolated from production stats — $RULE"
      FAIL=$((FAIL + 1))
    fi
  fi
done < <(find "$SCRIPT_DIR" -type f -print0)

if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  echo "Results: $FAIL offending file(s)"
  exit 1
fi

echo "OK: every spawn-executing test file under tests/ is isolated from production stats"
exit 0
