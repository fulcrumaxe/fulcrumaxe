#!/usr/bin/env bash
# tests/test_pre_spawn_check_block_events.sh
# Verifies that pre-spawn-check.sh emits spawn_blocked events to agent-feed.jsonl
# for each hard-block path (budget_exceeded, circuit_breaker_open, subscription_throttled,
# worktree_cap_reached) and that --dry-run never writes to the feed.
#
# HARD RULE: UNDER NO CIRCUMSTANCES may this test invoke `claude`, `claude -p`,
# `_start_loop_run`, or trigger /loop. Block conditions are simulated via
# mock scripts. See Discussion #439.
#
# Every AC below invokes the REAL scripts/pre-spawn-check.sh -- not a hand-rolled
# call to agent-feed-append.sh -- against a *sandboxed* copy of the script tree
# (see install_sandbox below). Copying pre-spawn-check.sh into a temp workspace
# relocates its own SCRIPT_DIR/REPO_ROOT there, so every path it touches
# (backend mocks, the agent feed, rotate-team-log) resolves inside the
# workspace instead of this repo's real state. Nothing in this file writes to
# $REPO_ROOT/.autonomous-team/agent-feed.jsonl (see the live-feed guard, AC8).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"

PASS=0
FAIL=0

ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

# ── Temp workspace ─────────────────────────────────────────────────────────────
TMPDIR_BASE=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_BASE"; }
trap cleanup EXIT

# make_workspace / install_sandbox / run_psc_sandboxed / feed_line_count /
# psc_exit / psc_stderr / psc_log now live in tests/lib/pre-spawn-check-fixture.sh
# (D#2267) -- test_pre_spawn_check_pm_dedup.sh needs the exact same sandboxed-
# pre-spawn-check.sh mechanism this suite already had, so it was extracted
# rather than duplicated a second time (the D#2119 mistake).
source "$SCRIPT_DIR/lib/pre-spawn-check-fixture.sh"

psc_exit()   { cat "$1/psc.exit"; }
psc_stderr() { cat "$1/psc.stderr" 2>/dev/null || true; }
psc_log()    { cat "$1/rotate-team-log.captured" 2>/dev/null || true; }

# Count spawn_blocked lines with a given reason in a feed file
count_blocked() {
  local feed="$1" reason="$2"
  python3 -c "
import json, sys
count = 0
try:
    with open('$feed') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get('event_type') == 'spawn_blocked' and d.get('reason') == '$reason':
                    count += 1
            except Exception:
                pass
except FileNotFoundError:
    pass
print(count)
" 2>/dev/null || echo "0"
}

# Get the last spawn_blocked event as JSON
last_blocked() {
  local feed="$1"
  python3 -c "
import json, sys
events = []
try:
    with open('$feed') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get('event_type') == 'spawn_blocked':
                    events.append(d)
            except Exception:
                pass
except FileNotFoundError:
    pass
if events:
    print(json.dumps(events[-1]))
else:
    print('{}')
" 2>/dev/null || echo "{}"
}

# ── Fixtures: mock backend scripts ────────────────────────────────────────────

make_budget_mock() {
  local ws="$1" allowed="$2" remaining="${3:-0}"
  # D#2063: the real `budget.py check` PRINTS its JSON verdict to stdout AND
  # exits 1 to signal exhaustion (see backend/budget.py:476-481) -- exit 1
  # there means "read succeeded, budget exhausted", not "the read failed".
  # This mock mirrors that exact print-then-exit-1 shape on allowed=False;
  # a mock that only printed and always exited 0 (the old shape of this
  # fixture) never exercised the bug D#2063 fixed at all.
  cat > "$ws/backend/budget.py" << PYEOF
#!/usr/bin/env python3
import json, sys
if len(sys.argv) > 1 and sys.argv[1] == 'check':
    print(json.dumps({"allowed": $allowed, "remaining": $remaining}))
    sys.exit(0 if $allowed else 1)
elif len(sys.argv) > 1 and sys.argv[1] == 'status':
    print(json.dumps({"session": {"ceiling": 5000000, "spent": 100, "remaining": $remaining}}))
else:
    pass
PYEOF
  chmod +x "$ws/backend/budget.py"
}

make_circuit_mock() {
  local ws="$1" failures="$2"
  cat > "$ws/backend/circuit_breaker.py" << PYEOF
#!/usr/bin/env python3
import sys
if len(sys.argv) > 1 and sys.argv[1] == 'status':
    print("$failures")
else:
    pass
PYEOF
  chmod +x "$ws/backend/circuit_breaker.py"
}

make_control_plane_mock() {
  local ws="$1"
  local sub_throttle="${2:-false}"
  local sub_pct="${3:-0}"
  local target_pct="${4:-80}"
  cat > "$ws/backend/control_plane.py" << PYEOF
#!/usr/bin/env python3
import sys, json
key = sys.argv[2] if len(sys.argv) > 2 else ''
if key == 'gates.subscription_throttle':
    print('"$sub_throttle"')
elif key == 'policies.subscription.target_percent':
    print('"$target_pct"')
elif key == 'settings.team-lead.max_parallel_impl':
    print('"3"')
elif key == 'gates.human_approval_before_merge':
    print('"false"')
else:
    print('{}')
PYEOF
  chmod +x "$ws/backend/control_plane.py"
}

make_subscription_mock() {
  local ws="$1" pct="$2"
  cat > "$ws/backend/subscription_usage.py" << PYEOF
#!/usr/bin/env python3
import json, sys
print(json.dumps({"percent": $pct, "plan": "pro"}))
PYEOF
  chmod +x "$ws/backend/subscription_usage.py"
}

echo "=== test_pre_spawn_check_block_events ==="
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Live-state guard setup (AC8 below closes this out) -- capture the REAL
# repo feed's line count now, before any sandboxed run, so we can prove at
# the end that nothing in this suite wrote to it.
# ─────────────────────────────────────────────────────────────────────────────
REAL_FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
LIVE_FEED_BEFORE=$(feed_line_count "$REAL_FEED")

# ─────────────────────────────────────────────────────────────────────────────
# AC0: emit_spawn_block honours its arguments (Spec item 3)
# Source the function in isolation (extracted from the real file, not a
# hand-copy) and call it directly with a stubbed agent-feed-append.sh that
# records its argv. Asserting on the source text of pre-spawn-check.sh would
# only prove the file was edited, not that the value flows -- so this reads
# the *recorded argv of the stub*.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC0: emit_spawn_block argument flow ---"

extract_emit_spawn_block() {
  python3 -c "
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r'^emit_spawn_block\(\) \{\n(?:.*\n)*?^\}\n', src, re.M)
if not m:
    sys.exit(1)
sys.stdout.write(m.group(0))
" "$SCRIPTS_DIR/pre-spawn-check.sh"
}

ws0=$(make_workspace)
FUNC_SRC=$(extract_emit_spawn_block)
if [[ -z "$FUNC_SRC" ]]; then
  fail "AC0: could not extract emit_spawn_block from pre-spawn-check.sh"
else
  cat > "$ws0/agent-feed-append.sh" << 'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "$CAPTURE"
EOF
  chmod +x "$ws0/agent-feed-append.sh"
  : > "$ws0/captured.txt"

  AC0_SCRIPT="$FUNC_SRC"$'\n''emit_spawn_block "unit_test_reason" "unit test message body" '"'"'{"k":"v"}'"'"$'\n'
  SCRIPT_DIR="$ws0" ROLE="executor" DISCUSSION="4242" DRY_RUN="" CAPTURE="$ws0/captured.txt" \
    bash -c "$AC0_SCRIPT"

  CAPTURED=$(cat "$ws0/captured.txt")
  if echo "$CAPTURED" | grep -q "unit_test_reason" \
    && echo "$CAPTURED" | grep -q "unit test message body" \
    && echo "$CAPTURED" | grep -qF '{"k":"v"}'; then
    ok "AC0: emit_spawn_block's recorded argv carries reason, message and details"
  else
    fail "AC0: recorded argv missing expected values: $CAPTURED"
  fi
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC1: budget_exceeded → spawn_blocked emitted with reason=budget_exceeded
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC1: budget_exceeded ---"
ws1=$(make_workspace)
install_sandbox "$ws1"
# Note: make_budget_mock's $allowed value is spliced directly into a Python
# literal (see the heredoc above) -- it must be "False"/"True", not the shell
# convention "false"/"true", or the mock itself throws a NameError and every
# call silently falls through to pre-spawn-check.sh's own allowed=true default.
make_budget_mock "$ws1" False 0

FEED1="$ws1/.autonomous-team/agent-feed.jsonl"

run_psc_sandboxed "$ws1" --role executor --discussion 999
EXIT1=$(psc_exit "$ws1")

if [[ "$EXIT1" -ne 0 ]]; then
  ok "AC1a: pre-spawn-check exits non-zero when budget exceeded"
else
  fail "AC1a: expected non-zero exit, got 0. stderr: $(psc_stderr "$ws1")"
fi

COUNT1=$(count_blocked "$FEED1" "budget_exceeded")
if [[ "$COUNT1" -ge 1 ]]; then
  ok "AC1b: spawn_blocked event with reason=budget_exceeded appended to feed"
else
  fail "AC1b: spawn_blocked event not found in feed"
fi

LAST1=$(last_blocked "$FEED1")
DETAIL1=$(python3 -c "import json,sys; d=json.loads('$LAST1'); print(d.get('details',{}).get('budget_remaining',''))" 2>/dev/null || echo "")
if [[ "$DETAIL1" == "0" ]]; then
  ok "AC1c: details.budget_remaining==0"
else
  fail "AC1c: details.budget_remaining expected 0, got '$DETAIL1'"
fi

# D#2063 Spec item 1: "Both halves are required -- asserting only on the exit
# code is not enough." Exit non-zero alone doesn't distinguish a real budget
# block from e.g. missing --event-id, which also exits non-zero before the
# budget check ever runs. Assert the actual stderr text too.
if echo "$(psc_stderr "$ws1")" | grep -q "budget exceeded"; then
  ok "AC1d: stderr contains 'budget exceeded'"
else
  fail "AC1d: stderr missing 'budget exceeded': $(psc_stderr "$ws1")"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC2: circuit_breaker_open → spawn_blocked with reason=circuit_breaker_open
# Invokes the real pre-spawn-check.sh (via run_psc_sandboxed) against a
# workspace-local feed -- this used to call agent-feed-append.sh by hand.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC2: circuit_breaker_open ---"
ws2=$(make_workspace)
install_sandbox "$ws2"
make_circuit_mock "$ws2" 5

FEED2="$ws2/.autonomous-team/agent-feed.jsonl"

run_psc_sandboxed "$ws2" --role executor --discussion 999
EXIT2=$(psc_exit "$ws2")

if [[ "$EXIT2" -ne 0 ]]; then
  ok "AC2a: pre-spawn-check exits non-zero when circuit-breaker is open"
else
  fail "AC2a: expected non-zero exit, got 0. stderr: $(psc_stderr "$ws2")"
fi

AFTER2=$(count_blocked "$FEED2" "circuit_breaker_open")
if [[ "$AFTER2" -ge 1 ]]; then
  ok "AC2: spawn_blocked with reason=circuit_breaker_open appended to feed"
  LAST2=$(last_blocked "$FEED2")
  CF=$(python3 -c "import json; d=json.loads('$LAST2'); print(d.get('details',{}).get('circuit_failures',''))" 2>/dev/null || echo "")
  if [[ "$CF" == "5" ]]; then
    ok "AC2-details: details.circuit_failures==5"
  else
    fail "AC2-details: details.circuit_failures expected 5, got '$CF'"
  fi
else
  fail "AC2: spawn_blocked with reason=circuit_breaker_open not appended"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC3: subscription_throttled → spawn_blocked with reason=subscription_throttled
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC3: subscription_throttled ---"
ws3=$(make_workspace)
install_sandbox "$ws3"
make_control_plane_mock "$ws3" true 95 80
make_subscription_mock "$ws3" 95

FEED3="$ws3/.autonomous-team/agent-feed.jsonl"

run_psc_sandboxed "$ws3" --role executor --discussion 999
EXIT3=$(psc_exit "$ws3")

if [[ "$EXIT3" -ne 0 ]]; then
  ok "AC3a: pre-spawn-check exits non-zero when subscription throttled"
else
  fail "AC3a: expected non-zero exit, got 0. stderr: $(psc_stderr "$ws3")"
fi

AFTER3=$(count_blocked "$FEED3" "subscription_throttled")
if [[ "$AFTER3" -ge 1 ]]; then
  ok "AC3: spawn_blocked with reason=subscription_throttled appended"
  LAST3=$(last_blocked "$FEED3")
  PCT=$(python3 -c "import json; d=json.loads('$LAST3'); print(d.get('details',{}).get('percent',''))" 2>/dev/null || echo "")
  if [[ "$PCT" == "95" ]]; then
    ok "AC3-details: details.percent==95"
  else
    fail "AC3-details: details.percent expected 95, got '$PCT'"
  fi
else
  fail "AC3: spawn_blocked with reason=subscription_throttled not appended"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC4: worktree_cap_reached → spawn_blocked with reason=worktree_cap_reached
# Real worktree dirs on disk, real cap comparison -- exercises the disk-based
# counter this Discussion added (scripts/lib/worktree-registry.sh count-disk),
# not a hand-written --details blob.
#
# Per the D#2059 Spec amendment: enforcement is deferred (no `exit 1`) until
# D#2001 (a working reaper) and D#2097 (the threshold itself) land, so this
# now expects exit 0 -- the guard still emits the row and logs the warning,
# it just doesn't block. See tests/test_worktree_cap_alarm.sh for the full
# over-cap/under-cap/mutation coverage of this behavior.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC4: worktree_cap_reached ---"
ws4=$(make_workspace)
install_sandbox "$ws4"
mkdir -p "$ws4/.claude/worktrees/agent-1" "$ws4/.claude/worktrees/agent-2" "$ws4/.claude/worktrees/agent-3"

FEED4="$ws4/.autonomous-team/agent-feed.jsonl"

WORKTREE_CAP=2 run_psc_sandboxed "$ws4" --role executor --discussion 999 --isolation worktree
EXIT4=$(psc_exit "$ws4")

if [[ "$EXIT4" -eq 0 ]]; then
  ok "AC4a: pre-spawn-check exits 0 when worktree cap reached (enforcement deferred, D#2059 amendment)"
else
  fail "AC4a: expected exit 0 (enforcement is deferred), got $EXIT4. stderr: $(psc_stderr "$ws4")"
fi

AFTER4=$(count_blocked "$FEED4" "worktree_cap_reached")
if [[ "$AFTER4" -ge 1 ]]; then
  ok "AC4: spawn_blocked with reason=worktree_cap_reached appended"
  LAST4=$(last_blocked "$FEED4")
  CAP=$(python3 -c "import json; d=json.loads('$LAST4'); print(d.get('details',{}).get('cap',''))" 2>/dev/null || echo "")
  ACTIVE=$(python3 -c "import json; d=json.loads('$LAST4'); print(d.get('details',{}).get('active_worktrees',''))" 2>/dev/null || echo "")
  if [[ "$CAP" == "2" ]]; then
    ok "AC4-details: details.cap==2"
  else
    fail "AC4-details: details.cap expected 2, got '$CAP'"
  fi
  if [[ "$ACTIVE" == "3" ]]; then
    ok "AC4-details: details.active_worktrees==3 (disk count, not the empty registry)"
  else
    fail "AC4-details: details.active_worktrees expected 3, got '$ACTIVE'"
  fi
else
  fail "AC4: spawn_blocked with reason=worktree_cap_reached not appended"
fi

LOG4=$(psc_log "$ws4")
if echo "$LOG4" | grep -q "worktree cap (2) reached" && echo "$LOG4" | grep -q "deferring spawn for"; then
  ok "AC4-log: rotate-team-log received the human-readable warning text"
else
  fail "AC4-log: rotate-team-log did not receive the expected warning text: $LOG4"
fi
if echo "$LOG4" | grep -q "emit_spawn_block"; then
  fail "AC4-log: rotate-team-log capture contains the literal string 'emit_spawn_block'"
else
  ok "AC4-log: rotate-team-log capture does not contain the literal string 'emit_spawn_block'"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC5: --dry-run never writes to feed
# Runs the real, uncopied script -- dry-run makes zero writes anywhere
# (verified by code inspection: every write site is gated on DRY_RUN != 1),
# so this is a legitimate direct check against the live feed without
# violating the live-state guard (AC8).
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC5: dry-run no feed write ---"

BEFORE5=$(feed_line_count "$REAL_FEED")

set +e
bash "$SCRIPTS_DIR/pre-spawn-check.sh" --role executor --discussion 999 --dry-run \
  > "$TMPDIR_BASE/psc-dryrun-out.txt" 2>&1
DRY_EXIT=$?
set -e

AFTER5=$(feed_line_count "$REAL_FEED")

if [[ "$DRY_EXIT" -eq 0 ]]; then
  ok "AC5a: --dry-run exits 0"
else
  echo "  [SKIP] AC5a: dry-run exited $DRY_EXIT (may be budget/circuit state)"
fi

if [[ "$AFTER5" -le "$BEFORE5" ]]; then
  ok "AC5b: --dry-run did not append to feed"
else
  fail "AC5b: --dry-run wrote $((AFTER5 - BEFORE5)) line(s) to the live feed"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC7: pm_dedup — project-manager dedup blocks within 120s window
# Seeds the *workspace* feed directly (never $REPO_ROOT's) with a recent and
# an expired spawn_attempt entry, then runs the sandboxed script so its own
# pm_dedup check reads that same workspace feed.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC7: pm_dedup ---"

# AC7a: spawn_attempt for project-manager D#999 written now → should block
ws7a=$(make_workspace)
install_sandbox "$ws7a"
FEED7A="$ws7a/.autonomous-team/agent-feed.jsonl"
NOW_TS=$(python3 -c "import datetime; print(datetime.datetime.utcnow().isoformat() + 'Z')")
python3 -c "
import json, pathlib
feed = pathlib.Path('$FEED7A')
feed.parent.mkdir(parents=True, exist_ok=True)
entry = {'event_type': 'spawn_attempt', 'role': 'project-manager', 'discussion': 999, 'message': 'spawn_attempt: project-manager D#999', 'ts': '$NOW_TS'}
with open(feed, 'a') as f:
    f.write(json.dumps(entry) + '\n')
"

run_psc_sandboxed "$ws7a" --role project-manager --discussion 999
AC7A_EXIT=$(psc_exit "$ws7a")

if [[ "$AC7A_EXIT" -ne 0 ]]; then
  ok "AC7a: pm_dedup blocks when recent spawn_attempt exists (exit non-zero)"
else
  fail "AC7a: expected exit 1 for pm_dedup, got 0"
fi

if echo "$(psc_stderr "$ws7a")" | grep -q "pm_dedup"; then
  ok "AC7a-msg: pm_dedup reason present in stderr"
else
  fail "AC7a-msg: pm_dedup reason missing from stderr"
fi

# AC7b: spawn_attempt written 5 minutes ago → should NOT block (cooldown expired)
ws7b=$(make_workspace)
install_sandbox "$ws7b"
FEED7B="$ws7b/.autonomous-team/agent-feed.jsonl"
OLD_TS=$(python3 -c "import datetime; print((datetime.datetime.utcnow() - datetime.timedelta(seconds=310)).isoformat() + 'Z')")
python3 -c "
import json, pathlib
feed = pathlib.Path('$FEED7B')
feed.parent.mkdir(parents=True, exist_ok=True)
entry = {'event_type': 'spawn_attempt', 'role': 'project-manager', 'discussion': 998, 'message': 'spawn_attempt: project-manager D#998', 'ts': '$OLD_TS'}
with open(feed, 'a') as f:
    f.write(json.dumps(entry) + '\n')
"

run_psc_sandboxed "$ws7b" --role project-manager --discussion 998
AC7B_EXIT=$(psc_exit "$ws7b")

if [[ "$AC7B_EXIT" -eq 0 ]]; then
  ok "AC7b: expired spawn_attempt (5min ago) does not block (exit 0)"
else
  fail "AC7b: expected exit 0 for expired dedup window, got $AC7B_EXIT. stderr: $(psc_stderr "$ws7b")"
fi

if echo "$(psc_stderr "$ws7b")" | grep -q "pm_dedup"; then
  fail "AC7b: pm_dedup triggered for entry older than 120s window"
else
  ok "AC7b: expired spawn_attempt (5min ago) does not trigger pm_dedup"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC6: spawn_blocked event schema validation
# Reads the last blocked event from AC2's workspace feed (a real invocation
# result), not the live repo feed.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC6: event schema ---"

LAST6=$(last_blocked "$FEED2")

REQUIRED_FIELDS="event_type role message reason"
SCHEMA_OK=true
for field in $REQUIRED_FIELDS; do
  VAL=$(python3 -c "import json; d=json.loads('$LAST6'); print(d.get('$field', ''))" 2>/dev/null || echo "")
  if [[ -z "$VAL" ]]; then
    fail "AC6: missing field '$field' in spawn_blocked event"
    SCHEMA_OK=false
  fi
done

if [[ "$SCHEMA_OK" == "true" ]]; then
  ok "AC6: spawn_blocked event has all required fields (event_type, role, message, reason)"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC9: budget.py unreadable (crash / LockTimeout / bad interpreter) is a
# DIFFERENT case from "budget.py ran and said exhausted" (AC1 above). D#2063's
# Spec does not test this case; this codifies the deliberate choice made in
# the PR: failing closed here would block every spawn on the host the moment
# budget.py breaks, with no way to spawn the agent that would fix it (the
# same shape of mistake PR #2093 made with the worktree-cap counter, which
# had to walk enforcement back to a warning for the same reason). So this
# stays open -- but it must be LOUD, not a silent fallback like the bug
# fixed here. A mock that prints nothing and exits 1 stands in for a crash
# (no valid JSON on stdout in either case).
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC9: budget.py unreadable (crash) stays open, but loud ---"

make_broken_budget_mock() {
  local ws="$1"
  cat > "$ws/backend/budget.py" << 'PYEOF'
#!/usr/bin/env python3
import sys
sys.stderr.write("Traceback (most recent call last):\nLockTimeout: could not acquire lock within 5s\n")
sys.exit(1)
PYEOF
  chmod +x "$ws/backend/budget.py"
}

ws9=$(make_workspace)
install_sandbox "$ws9"
make_broken_budget_mock "$ws9"

run_psc_sandboxed "$ws9" --role executor --discussion 999
EXIT9=$(psc_exit "$ws9")

if [[ "$EXIT9" -eq 0 ]]; then
  ok "AC9a: spawn NOT blocked when budget.py itself is unreadable (no bootstrap deadlock)"
else
  fail "AC9a: expected exit 0 (stay open on unknown budget state), got $EXIT9. stderr: $(psc_stderr "$ws9")"
fi

if echo "$(psc_stderr "$ws9")" | grep -qi "budget.py check unreadable"; then
  ok "AC9b: stderr carries a loud, explicit warning (not silent)"
else
  fail "AC9b: expected an unreadable-budget warning on stderr, got: $(psc_stderr "$ws9")"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC8: the suite never writes to the live repo feed (Spec item 7)
#
# Race-proofed (D#2267 Spec item 8): comparing raw before/after line counts
# is flaky the moment a real, concurrently-running agent legitimately
# appends to $REAL_FEED during this suite's run -- that's not this suite
# writing live state, it's someone else's traffic landing in the same file.
# Every AC in this suite passes --discussion 999 or 998 (see install_sandbox
# invocations above); those are sentinel numbers far below any real
# Discussion in this repo (currently 2200+), so instead of requiring the
# count to be identical, scan only the lines *added* since LIVE_FEED_BEFORE
# and confirm none of them carry one of those sentinel discussion numbers --
# that is "zero rows attributable to this suite", and it still catches a
# real sandbox leak while tolerating unrelated concurrent growth.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC8: live-state guard ---"

LIVE_FEED_AFTER=$(feed_line_count "$REAL_FEED")

ATTRIBUTABLE_ROWS=0
if [[ -f "$REAL_FEED" && "$LIVE_FEED_AFTER" -gt "$LIVE_FEED_BEFORE" ]]; then
  ATTRIBUTABLE_ROWS=$(tail -n "+$((LIVE_FEED_BEFORE + 1))" "$REAL_FEED" | python3 -c "
import json, sys
count = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if str(d.get('discussion', '')) in ('998', '999'):
        count += 1
print(count)
" 2>/dev/null || echo "0")
fi

if [[ "$ATTRIBUTABLE_ROWS" -eq 0 ]]; then
  ok "AC8: zero rows attributable to this suite in live repo feed (host jp, $REAL_FEED: $LIVE_FEED_BEFORE -> $LIVE_FEED_AFTER lines, $((LIVE_FEED_AFTER - LIVE_FEED_BEFORE)) line(s) from unrelated concurrent activity)"
else
  fail "AC8: $ATTRIBUTABLE_ROWS row(s) matching this suite's sentinel discussions (998/999) landed in the live repo feed -- sandbox leaked a write"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
