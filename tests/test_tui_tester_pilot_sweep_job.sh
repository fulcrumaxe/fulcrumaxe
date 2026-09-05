#!/usr/bin/env bash
# test_tui_tester_pilot_sweep_job.sh — unit tests for tui-tester-pilot-sweep.sh
#
# Tests:
#   1. Gate off: job exits 0 without doing anything, no gh calls
#   2. Gate on, sweep pass: no Discussion filed
#   3. Gate on, 1 new finding: Discussion created once, dismissed-pair updated, exit 0
#   4. Rate limit: second run within 1h skips Discussion (pair already in cache)
#
# Strategy: inject mock python3 and mock gh into PATH ahead of real binaries.
# The mock python3 intercepts control_plane.py and pilot-sweep.py calls;
# all other python3 calls fall through to the real interpreter.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
JOB_SCRIPT="$REPO_ROOT/scripts/schedule/jobs/tui-tester-pilot-sweep.sh"

PASS=0
FAIL=0
TOTAL=0

pass() { local name="$1"; echo "  PASS: $name"; PASS=$((PASS+1)); TOTAL=$((TOTAL+1)); }
fail() { local name="$1" msg="$2"; echo "  FAIL: $name — $msg"; FAIL=$((FAIL+1)); TOTAL=$((TOTAL+1)); }

echo "=== tui-tester-pilot-sweep job tests ==="
echo ""

# ── Shared setup ──────────────────────────────────────────────────────────────
# All scratch files (gate, findings, gh call log, job stdout) live under this
# one mktemp'd dir — was a set of fixed /tmp/tui_sweep_* names shared across
# any concurrently-running copy of this suite (D#2254).
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

MOCK_BIN="$TMPDIR_TEST/bin"
mkdir -p "$MOCK_BIN"

MOCK_STATE="$TMPDIR_TEST/state"
mkdir -p "$MOCK_STATE/tui-tester"
DISMISSED="$MOCK_STATE/tui-tester/dismissed-pairs.json"

SWEEP_GATE="$TMPDIR_TEST/tui_sweep_gate"
SWEEP_FINDINGS="$TMPDIR_TEST/tui_sweep_findings"
SWEEP_GH_LOG="$TMPDIR_TEST/tui_sweep_gh_calls.log"
SWEEP_OUT="$TMPDIR_TEST/tui_sweep_out.txt"

# ── Mock python3 — intercepts control_plane.py and pilot-sweep.py ─────────────
REAL_PYTHON3=$(command -v python3)
cat > "$MOCK_BIN/python3" <<MOCKPY
#!/usr/bin/env bash
# Intercept control_plane.py and pilot-sweep.py; pass everything else to real python3.
ARGS="\$*"
if [[ "\$ARGS" == *"control_plane.py"* ]]; then
  # Return the gate value from the test's gate file
  GATE=\$(cat $SWEEP_GATE 2>/dev/null || echo "false")
  echo "\$GATE"
  exit 0
elif [[ "\$ARGS" == *"pilot-sweep.py"* ]]; then
  # Return configured findings JSON (last line, as pilot-sweep.py does)
  FINDINGS=\$(cat $SWEEP_FINDINGS 2>/dev/null || echo '{"verdict":"pass","findings":[],"artifact_dir":"","elapsed_s":0}')
  echo "[pilot-sweep] mocked" >&2
  echo "\$FINDINGS"
  exit 0
else
  exec "$REAL_PYTHON3" "\$@"
fi
MOCKPY
chmod +x "$MOCK_BIN/python3"

# ── Mock gh — logs calls, returns fake GraphQL responses ─────────────────────
# Deliberately an unquoted heredoc (was quoted 'MOCKGH') so $SWEEP_GH_LOG
# resolves to this suite's own tmpdir when the stub is written; every other
# '$' the stub itself needs at runtime is escaped so it stays literal.
cat > "$MOCK_BIN/gh" <<MOCKGH
#!/usr/bin/env bash
# Log all args to the gh call log
echo "\$*" >> $SWEEP_GH_LOG
# Return appropriate responses based on the mutation/query type
ARGS="\$*"
if [[ "\$ARGS" == *"createDiscussion"* ]]; then
    echo '{"data":{"createDiscussion":{"discussion":{"number":999,"url":"https://github.com/autonomous-agent-7/autonomous-forever/discussions/999"}}}}'
elif [[ "\$ARGS" == *"discussionCategories"* ]]; then
    echo '{"data":{"repository":{"discussionCategories":{"nodes":[{"id":"DIC_cat1","name":"General"}]}}}}'
elif [[ "\$ARGS" == *"repository(owner"* ]] || [[ "\$ARGS" == *'repository(owner:'* ]]; then
    echo '{"data":{"repository":{"id":"R_repo1"}}}'
else
    echo '{}'
fi
exit 0
MOCKGH
chmod +x "$MOCK_BIN/gh"

# Helper: count createDiscussion calls from log
count_create_calls() {
    grep -c "createDiscussion" "$SWEEP_GH_LOG" 2>/dev/null || true
    # grep -c exits 1 on no match but prints 0 — capture just the number
    true  # ensure exit 0
}

# ── Test 1: Gate off — exits 0, no gh calls ───────────────────────────────────
echo "1. Gate off — exits 0 without filing"
echo "false" > "$SWEEP_GATE"
echo '{"verdict":"pass","findings":[],"artifact_dir":"","elapsed_s":0}' > "$SWEEP_FINDINGS"
rm -f "$SWEEP_GH_LOG" && touch "$SWEEP_GH_LOG"
echo '{}' > "$DISMISSED"

PATH="$MOCK_BIN:$PATH" AUTONOMOUS_TEAM_STATE_DIR="$MOCK_STATE" \
    bash "$JOB_SCRIPT" > "$SWEEP_OUT" 2>&1
RC=$?

GH_LINES=$(wc -l < "$SWEEP_GH_LOG")
GH_LINES="${GH_LINES// /}"  # trim whitespace

if [[ $RC -eq 0 ]]; then
    pass "gate off: exits 0"
else
    fail "gate off: exits 0" "exit code was $RC"
fi
if [[ "$GH_LINES" -eq 0 ]]; then
    pass "gate off: no gh calls"
else
    fail "gate off: no gh calls" "got $GH_LINES gh calls"
fi

# ── Test 2: Gate on, sweep passes — no Discussion filed ───────────────────────
echo "2. Gate on, pass verdict — no Discussion filed"
echo "true" > "$SWEEP_GATE"
echo '{"verdict":"pass","findings":[],"artifact_dir":"","elapsed_s":0.5}' > "$SWEEP_FINDINGS"
rm -f "$SWEEP_GH_LOG" && touch "$SWEEP_GH_LOG"
echo '{}' > "$DISMISSED"

PATH="$MOCK_BIN:$PATH" AUTONOMOUS_TEAM_STATE_DIR="$MOCK_STATE" \
    bash "$JOB_SCRIPT" > "$SWEEP_OUT" 2>&1
RC=$?

CREATE_N=$(python3 -c "
lines = open('$SWEEP_GH_LOG').readlines() if __import__('os').path.exists('$SWEEP_GH_LOG') else []
print(sum(1 for l in lines if 'createDiscussion' in l))
" 2>/dev/null || echo 0)

if [[ $RC -eq 0 ]]; then
    pass "pass verdict: exits 0"
else
    fail "pass verdict: exits 0" "exit code was $RC"
fi
if [[ "$CREATE_N" -eq 0 ]]; then
    pass "pass verdict: no Discussion created"
else
    fail "pass verdict: no Discussion created" "got $CREATE_N createDiscussion calls"
fi

# ── Test 3: Gate on, 1 new finding — Discussion filed, cache updated ──────────
echo "3. Gate on, 1 new finding — Discussion created once, cache updated"
echo "true" > "$SWEEP_GATE"
cat > "$SWEEP_FINDINGS" <<'FINDINGS'
{"verdict":"needs-fix","findings":[{"tab":"agents","widget_id":"table","check_name":"cursor_visible","status":"fail","evidence_path":null,"detail":"No cursor on agents screen"}],"artifact_dir":"","elapsed_s":1.2}
FINDINGS
rm -f "$SWEEP_GH_LOG" && touch "$SWEEP_GH_LOG"
echo '{}' > "$DISMISSED"

PATH="$MOCK_BIN:$PATH" AUTONOMOUS_TEAM_STATE_DIR="$MOCK_STATE" \
    bash "$JOB_SCRIPT" > "$SWEEP_OUT" 2>&1
RC=$?

CREATE_N=$(python3 -c "
lines = open('$SWEEP_GH_LOG').readlines() if __import__('os').path.exists('$SWEEP_GH_LOG') else []
print(sum(1 for l in lines if 'createDiscussion' in l))
" 2>/dev/null || echo 0)

CACHE_HAS_PAIR=$(python3 -c "
import json, sys
try:
    cache = json.loads(open('$DISMISSED').read())
    print('yes' if 'agents:cursor_visible' in cache else 'no')
except Exception as e:
    print('no')
" 2>/dev/null || echo "no")

if [[ $RC -eq 0 ]]; then
    pass "1 finding: exits 0"
else
    fail "1 finding: exits 0" "exit code=$RC output=$(cat "$SWEEP_OUT" | head -3)"
fi
if [[ "$CREATE_N" -ge 1 ]]; then
    pass "1 finding: Discussion created"
else
    fail "1 finding: Discussion created" "got $CREATE_N createDiscussion calls; output: $(cat "$SWEEP_OUT")"
fi
if [[ "$CACHE_HAS_PAIR" == "yes" ]]; then
    pass "1 finding: dismissed-pair cache updated"
else
    fail "1 finding: dismissed-pair cache updated" "pair 'agents:cursor_visible' not in cache (cache: $(cat "$DISMISSED"))"
fi

# ── Test 4: Rate limit — same pair filed within 1h ────────────────────────────
echo "4. Rate limit — pair already filed within 1h, skip Discussion"
echo "true" > "$SWEEP_GATE"
# Same findings as Test 3
cat > "$SWEEP_FINDINGS" <<'FINDINGS'
{"verdict":"needs-fix","findings":[{"tab":"agents","widget_id":"table","check_name":"cursor_visible","status":"fail","evidence_path":null,"detail":"No cursor on agents screen"}],"artifact_dir":"","elapsed_s":1.2}
FINDINGS
rm -f "$SWEEP_GH_LOG" && touch "$SWEEP_GH_LOG"

# Pre-populate cache: last filed 30 min ago (within 1h window)
RECENT_TS=$(python3 -c "
from datetime import datetime, timezone, timedelta
print((datetime.now(timezone.utc) - timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%SZ'))
")
python3 -c "
import json
cache = {'agents:cursor_visible': '$RECENT_TS'}
open('$DISMISSED', 'w').write(json.dumps(cache))
"

PATH="$MOCK_BIN:$PATH" AUTONOMOUS_TEAM_STATE_DIR="$MOCK_STATE" \
    bash "$JOB_SCRIPT" > "$SWEEP_OUT" 2>&1
RC=$?

CREATE_N=$(python3 -c "
lines = open('$SWEEP_GH_LOG').readlines() if __import__('os').path.exists('$SWEEP_GH_LOG') else []
print(sum(1 for l in lines if 'createDiscussion' in l))
" 2>/dev/null || echo 0)

if [[ $RC -eq 0 ]]; then
    pass "rate limit: exits 0"
else
    fail "rate limit: exits 0" "exit code was $RC"
fi
if [[ "$CREATE_N" -eq 0 ]]; then
    pass "rate limit: Discussion skipped (within 1h)"
else
    fail "rate limit: Discussion skipped" "got $CREATE_N createDiscussion calls (expected 0)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
exit 0
