#!/usr/bin/env bash
# scripts/test-muse-spawn-prompt.sh — verify the Muse spawn assembler + TL twin.
#
# Tests:
#   1. Agent(subagent_type=...) in input becomes subagent_spawn + subagent_wait
#      with args preserved (role, isolation) and the worktree mapping spelled out
#   2. Bare Agent() and Agent()/spawn-agent.sh shapes translate (no leftovers)
#   3. No "implement directly" / "direct implementation" remains in spawn output
#   4. Full assembly exits 0 with ## ROLE / ## TEMPLATE / ## BRIEF / ## REPORTING
#      sections and the hook_event_id trailer
#   5. TL twin helpers emit prompts; explicit event-id is honored on fix rounds
#   6. Missing role card fails loudly (exit 1)
#
# Exit 0 on pass, exit 1 on any failure. No network, no gh, no muse calls.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=true
FAILURES=()

pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1"; PASS=false; FAILURES+=("$1"); }

ASM="$REPO_ROOT/scripts/lib/muse-spawn-prompt.sh"
TWIN="$REPO_ROOT/scripts/lib/muse-team-lead-prompts.sh"

# Isolated state FIRST — every assembler call below (including the twin's)
# must land here, never in the real state dir or tracked feed. Moved above
# the render tests after a twin run polluted the real agent-feed.jsonl.
TMPSTATE="$(mktemp -d /tmp/test-muse-spawn-XXXXXX)"
export AUTONOMOUS_TEAM_STATE_DIR="$TMPSTATE"
export STATS_DB_PATH="$TMPSTATE/stats.duckdb"
export MUSE_SPAWN_FEED_FILE="$TMPSTATE/feed.jsonl"
export PATH="$REPO_ROOT/.venv/bin:$PATH"
if [[ -f /usr/lib/x86_64-linux-gnu/libstdc++.so.6 ]]; then
  export LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
_cleanup_tmpstate() { rm -rf "$TMPSTATE"; }
trap _cleanup_tmpstate EXIT

# ── 1. Agent(...) with args translates, args preserved ─────────────────────────
BRIEF='Tell Agent(subagent_type="executor", isolation="worktree", prompt="do it") to run.'
OUT=$(bash "$ASM" --role executor --brief "$BRIEF" --event-id executor-1-100 --discussion 1 --no-register 2>/dev/null)
for want in 'subagent_spawn' 'subagent_wait' 'subagent_send_message' 'subagent_type="executor"' 'isolation="worktree"' 'worktree-isolation'; do
  grep -qF "$want" <<<"$OUT" && pass "directive contains: $want" || fail "directive missing: $want"
done

# ── 2. Bare Agent() and Agent()/spawn-agent.sh shapes translate ────────────────
BRIEF2='First Agent() then Agent()/spawn-agent.sh for the rest.'
OUT2=$(bash "$ASM" --role executor --brief "$BRIEF2" --event-id executor-1-101 --discussion 1 --no-register 2>/dev/null)
grep -q 'Agent(' <<<"$OUT2" && fail "bare Agent( shape survived" || pass "no Agent( leftovers"
grep -qF 'muse-spawn-prompt.sh' <<<"$OUT2" && pass "assembler self-reference kept" || fail "assembler self-reference lost"

# ── 3. No implement-directly language remains ──────────────────────────────────
if grep -q 'implement directly' <<<"$OUT$OUT2" || grep -q 'direct implementation' <<<"$OUT$OUT2"; then
  fail "'implement directly' language still present"
else
  pass "no implement-directly language"
fi

# ── 4. Assembly shape ──────────────────────────────────────────────────────────
for want in '## ROLE: executor' '## TEMPLATE' '## BRIEF' '## REPORTING' 'hook_event_id=executor-1-100' 'AGENT_OUTPUT'; do
  grep -qF "$want" <<<"$OUT" && pass "assembly contains: $want" || fail "assembly missing: $want"
done

# ── 5. TL twin helpers ─────────────────────────────────────────────────────────
# shellcheck source=scripts/lib/muse-team-lead-prompts.sh
source "$TWIN"
TWIN_OUT=$(tl_muse_spawn_executor 543 "Do the thing" 2>/tmp/twin-err.txt)
[ $? -eq 0 ] && pass "twin helper exits 0" || fail "twin helper exits nonzero"
grep -qF '## ROLE: executor' <<<"$TWIN_OUT" && pass "twin emits executor prompt" || fail "twin prompt missing role"
grep -qF 'event-id: executor-543-' /tmp/twin-err.txt && pass "twin mints event-id" || fail "twin event-id missing"
grep -qF 'subagent_type=' /tmp/twin-err.txt && pass "twin names subagent_type" || fail "twin type missing"
TWIN_FIX=$(tl_muse_spawn_executor 543 "Do the thing" "executor-543-999" 2>/dev/null)
grep -qF 'hook_event_id=executor-543-999' <<<"$TWIN_FIX" && pass "explicit event-id honored" || fail "explicit event-id not honored"
rm -f /tmp/twin-err.txt

# ── 5b. TL twin MUSE_SPAWN_NO_REGISTER seam ──────────────────────────────────────
# Regression: the seam line lacked a continuation backslash, so with the var
# set the helper died with "--no-register: command not found" (exit 127).
MUSE_SPAWN_NO_REGISTER=1 TWIN_SEAM_OUT=$(tl_muse_spawn_executor 543 "Do the thing" "executor-543-seam" 2>/tmp/twin-seam-err.txt)
[ $? -eq 0 ] && pass "twin seam exits 0" || fail "twin seam exits nonzero"
grep -qF '## ROLE: executor' <<<"$TWIN_SEAM_OUT" && pass "twin seam emits prompt" || fail "twin seam prompt missing"
! grep -qF 'command not found' /tmp/twin-seam-err.txt && pass "twin seam no 127" || fail "twin seam hit command-not-found"
rm -f /tmp/twin-seam-err.txt

# ── 6. Missing role card fails loudly ──────────────────────────────────────────
if bash "$ASM" --role nope-not-a-role --brief "x" --event-id e-1 2>/dev/null; then
  fail "missing role card did not fail"
else
  pass "missing role card fails loudly"
fi

# ── 7-10. Recording + stop twin (isolated state set up top) ────────────────────
EV7="executor-77-1234"
bash "$ASM" --role executor --brief "record me" --event-id "$EV7" --discussion 77 >/dev/null 2>&1
grep -qF "$EV7" "$TMPSTATE/spawn-registry.jsonl" 2>/dev/null \
  && pass "registry row recorded" || fail "registry row missing"
ROLE7=$(python3 -c "
import duckdb
print(duckdb.connect('$TMPSTATE/stats.duckdb', read_only=True).execute(
  \"select role from agent_run where agent_id='$EV7'\").fetchone()[0])
" 2>/dev/null)
[[ "$ROLE7" == "executor" ]] && pass "start row recorded" || fail "start row missing (got: $ROLE7)"
grep -qF "spawn_notify" "$TMPSTATE/feed.jsonl" 2>/dev/null \
  && pass "feed line recorded" || fail "feed line missing"

# ── 8. --no-register writes nothing ───────────────────────────────────────────
EV8="executor-77-9999"
bash "$ASM" --role executor --brief "smoke" --event-id "$EV8" --discussion 77 --no-register >/dev/null 2>&1
grep -qF "$EV8" "$TMPSTATE/spawn-registry.jsonl" 2>/dev/null \
  && fail "--no-register still recorded" || pass "--no-register writes nothing"

# ── 9. Stop twin dry-run resolves identity ────────────────────────────────────
STOP_JSON="{\"spawn_accept\": {\"status\": \"accepted\", \"subagent_id\": \"sub-9\", \"agent_path\": \"main/$EV7/1\", \"task_ref\": \"task/abc#0\"}, \"wait_result\": {\"status\": \"ready\", \"subagent_id\": \"sub-9\", \"task_ref\": \"task/abc#5\", \"summary\": \"did it\", \"evidence_refs\": []}, \"wait_is_error\": false}"
export MUSE_STOP_DRY_RUN=1 MUSE_STOP_ARGS_FILE="$TMPSTATE/stop-args.json"
echo "$STOP_JSON" | bash "$REPO_ROOT/scripts/muse-subagent-stop.sh" >/dev/null 2>&1
[ $? -eq 0 ] && pass "stop twin exits 0" || fail "stop twin exits nonzero"
for want in '"role": "executor"' '"verdict": "done"' "\"event_id\": \"$EV7\"" '"discussion": "77"'; do
  grep -qF "$want" "$TMPSTATE/stop-args.json" 2>/dev/null \
    && pass "stop args contain $want" || fail "stop args missing $want"
done
unset MUSE_STOP_DRY_RUN MUSE_STOP_ARGS_FILE

# ── 9b. Stop twin backfills tokens from the child log (dry-run mode) ──────────
printf '%s\n' \
  '{"payload": {"event": {"kind": "assistant_message_committed", "text": "did it"}}}' \
  '{"payload": {"event": {"kind": "goal_usage_attribution", "record": {"quantity": {"input_tokens": 300, "output_tokens": 30}}}}}' \
  > "$TMPSTATE/session.jsonl"
STOP_BF="{\"spawn_accept\": {\"status\": \"accepted\", \"subagent_id\": \"sub-9\", \"agent_path\": \"main/$EV7/1\", \"task_ref\": \"task/abc#0\"}, \"wait_result\": {\"status\": \"ready\", \"subagent_id\": \"sub-9\", \"task_ref\": \"task/abc#5\", \"summary\": \"did it\", \"evidence_refs\": [\"$TMPSTATE/session.jsonl\"]}, \"wait_is_error\": false}"
export MUSE_STOP_DRY_RUN=1 MUSE_STOP_ARGS_FILE="$TMPSTATE/stop-args.json"
echo "$STOP_BF" | bash "$REPO_ROOT/scripts/muse-subagent-stop.sh" >/dev/null 2>&1
TOK9=$(python3 -c "
import duckdb
print(duckdb.connect('$TMPSTATE/stats.duckdb', read_only=True).execute(
  \"select input_tok, output_tok from agent_run where agent_id='$EV7'\").fetchone())
" 2>/dev/null)
[[ "$TOK9" == "(300, 30)" ]] && pass "stop backfills tokens" || fail "tokens wrong (got: $TOK9)"
unset MUSE_STOP_DRY_RUN MUSE_STOP_ARGS_FILE

# ── 10. Row lifecycle closes via complete (NEVER the real post-agent-hook) ────
# The real post-agent-hook.sh contamination-recovers a branch checkout with
# `reset --hard origin/main` — invoking it here once destroyed this worktree's
# branch state. The dry-run above proves arg resolution; this proves the row
# closes using the same complete CLI post-agent-hook itself calls.
python3 "$REPO_ROOT/backend/agent_run_tracker.py" complete \
  --agent-id "$EV7" --verdict done --input-tokens 0 --output-tokens 0 >/dev/null 2>&1
VERDICT10=$(python3 -c "
import duckdb
print(duckdb.connect('$TMPSTATE/stats.duckdb', read_only=True).execute(
  \"select verdict from agent_run where agent_id='$EV7'\").fetchone()[0])
" 2>/dev/null)
[[ "$VERDICT10" == "done" ]] && pass "stop closes row as done" || fail "row verdict wrong (got: $VERDICT10)"

# ── 11. Stop twin garbage-in exits 0 ──────────────────────────────────────────
echo '{not json' | bash "$REPO_ROOT/scripts/muse-subagent-stop.sh" >/dev/null 2>&1
[ $? -eq 0 ] && pass "garbage-in exits 0" || fail "garbage-in exits nonzero"
# (TMPSTATE removed by the EXIT trap)

# ── Verdict ────────────────────────────────────────────────────────────────────
if $PASS; then
  echo "PASS: all muse spawn-prompt checks passed"
  exit 0
else
  echo "FAIL: ${#FAILURES[@]} check(s) failed: ${FAILURES[*]}"
  exit 1
fi
