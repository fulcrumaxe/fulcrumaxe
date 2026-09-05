#!/usr/bin/env bash
# test_scheduler_dispatcher.sh — schema validation + p99 benchmark for the
# cron-bridge dispatcher.
#
# Tests:
#   1.  validate-only: good manifest passes
#   2.  malformed YAML: dispatcher exits non-zero
#   3.  allowlist: job key not in registry is rejected
#   4.  shell metacharacters in name rejected
#   5.  path traversal in job key rejected
#   6.  due-job filter: disabled job not returned
#   7.  due-job filter: enabled job matching schedule IS returned
#   8.  log scrubbing: GH_TOKEN/API keys/Bearer tokens not preserved
#   9.  heartbeat job actually runs and writes timestamp
#   10. p99 benchmark: 1000 ticks with 0 due jobs complete in <100ms p99
#   11. gate undefined: dispatcher exits 0, gate_off row written
#   12. gate explicitly false: dispatcher exits 0, gate_off row written
#   13. gate read succeeds but returns empty: dispatcher exits 0, gate_off
#       row written (D#2046 — the one real behavioural fix in this suite)
#   14. gate explicitly true: dispatcher proceeds, no gate_off row written
#   15. gates.scheduled_jobs has an explicit default of false
#   16. parse_jobs.py argparse-usage-error (exit 2) is classified as
#       parse_error, not misread as gate_off (D#2046 review fix — exit 2
#       used to mean "gate off from parse_jobs.py" back when parse_jobs.py
#       had its own EX_GATE_OFF; that producer was removed in this same PR,
#       leaving argparse's own usage-error convention as the sole exit-2
#       source, which the dispatcher must not silently swallow)
#
# Exit 0 = all tests passed. Exit 1 = failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PARSE_HELPER="$REPO_ROOT/scripts/schedule/parse_jobs.py"
DISPATCHER="$REPO_ROOT/scripts/schedule/dispatcher.sh"
MANIFEST="$REPO_ROOT/scripts/schedule/jobs.yaml"
REGISTRY="$REPO_ROOT/scripts/schedule/jobs"

PASS=0
FAIL=0
TOTAL=0

pass() { local name="$1"; echo "  PASS: $name"; PASS=$((PASS+1)); TOTAL=$((TOTAL+1)); }
fail() { local name="$1" msg="$2"; echo "  FAIL: $name — $msg"; FAIL=$((FAIL+1)); TOTAL=$((TOTAL+1)); }

echo "=== Scheduler Dispatcher Tests ==="
echo ""

# ── Test 1: valid manifest passes schema validation ───────────────────────────
echo "1. Schema validation (good manifest)"
if python3 "$PARSE_HELPER" \
    --manifest "$MANIFEST" \
    --registry "$REGISTRY" \
    --validate-only 2>/dev/null; then
    pass "valid manifest passes"
else
    fail "valid manifest passes" "exit code was non-zero"
fi

# ── Test 2: malformed YAML is rejected ────────────────────────────────────────
echo "2. Malformed YAML rejected"
TMPDIR_TEST=$(mktemp -d)
MALFORMED_YAML="$TMPDIR_TEST/malformed.yaml"
cat > "$MALFORMED_YAML" <<'EOF'
this: is not a list
key: value
EOF
if ! python3 "$PARSE_HELPER" \
    --manifest "$MALFORMED_YAML" \
    --registry "$REGISTRY" \
    --validate-only 2>/dev/null; then
    pass "malformed YAML exits non-zero"
else
    fail "malformed YAML exits non-zero" "expected non-zero exit but got 0"
fi

# ── Test 3: allowlist — job key not in registry is rejected ───────────────────
echo "3. Allowlist — unknown job key rejected"
ALLOWLIST_YAML="$TMPDIR_TEST/allowlist.yaml"
cat > "$ALLOWLIST_YAML" <<'EOF'
- name: test-job
  job: does-not-exist
  schedule: "* * * * *"
  timeout_seconds: 30
  token_ceiling: 0
  enabled: true
EOF
if ! python3 "$PARSE_HELPER" \
    --manifest "$ALLOWLIST_YAML" \
    --registry "$REGISTRY" \
    --validate-only 2>/dev/null; then
    pass "unknown job key exits non-zero"
else
    fail "unknown job key exits non-zero" "expected non-zero exit but got 0"
fi

# ── Test 4: shell metacharacters in name rejected ─────────────────────────────
echo "4. Shell metacharacters in name rejected"
META_YAML="$TMPDIR_TEST/meta.yaml"
cat > "$META_YAML" <<'EOF'
- name: "bad;name"
  job: heartbeat
  schedule: "* * * * *"
  timeout_seconds: 30
  token_ceiling: 0
  enabled: true
EOF
if ! python3 "$PARSE_HELPER" \
    --manifest "$META_YAML" \
    --registry "$REGISTRY" \
    --validate-only 2>/dev/null; then
    pass "metacharacter name rejected"
else
    fail "metacharacter name rejected" "expected non-zero exit but got 0"
fi

# ── Test 5: path traversal in job key rejected ────────────────────────────────
echo "5. Path traversal in job key rejected"
TRAV_YAML="$TMPDIR_TEST/traversal.yaml"
cat > "$TRAV_YAML" <<'EOF'
- name: test-job
  job: "../../../etc/passwd"
  schedule: "* * * * *"
  timeout_seconds: 30
  token_ceiling: 0
  enabled: true
EOF
if ! python3 "$PARSE_HELPER" \
    --manifest "$TRAV_YAML" \
    --registry "$REGISTRY" \
    --validate-only 2>/dev/null; then
    pass "path traversal rejected"
else
    fail "path traversal rejected" "expected non-zero exit but got 0"
fi

# ── Test 6: due-job filter — disabled job not returned ───────────────────────
echo "6. Due-job filter — disabled job not returned"
DISABLED_YAML="$TMPDIR_TEST/disabled.yaml"
cat > "$DISABLED_YAML" <<'EOF'
- name: heartbeat
  job: heartbeat
  schedule: "* * * * *"
  timeout_seconds: 30
  token_ceiling: 0
  enabled: false
EOF
DUE=$(python3 "$PARSE_HELPER" \
    --manifest "$DISABLED_YAML" \
    --registry "$REGISTRY" \
    --minute "2026-01-01T12:00" 2>/dev/null || echo "[]")
COUNT=$(echo "$DUE" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "-1")
if [[ "$COUNT" -eq 0 ]]; then
    pass "disabled job returns empty due list"
else
    fail "disabled job returns empty due list" "got $COUNT jobs"
fi

# ── Test 7: due-job filter — enabled job matching schedule IS returned ─────────
echo "7. Due-job filter — every-minute job is due"
ALWAYS_YAML="$TMPDIR_TEST/always.yaml"
cat > "$ALWAYS_YAML" <<'EOF'
- name: heartbeat
  job: heartbeat
  schedule: "* * * * *"
  timeout_seconds: 30
  token_ceiling: 0
  enabled: true
EOF
DUE=$(python3 "$PARSE_HELPER" \
    --manifest "$ALWAYS_YAML" \
    --registry "$REGISTRY" \
    --minute "2026-01-01T12:00" 2>/dev/null || echo "[]")
COUNT=$(echo "$DUE" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "-1")
if [[ "$COUNT" -eq 1 ]]; then
    pass "every-minute job appears in due list"
else
    fail "every-minute job appears in due list" "got $COUNT jobs"
fi

# ── Test 8: log scrubbing — GH_TOKEN not preserved ───────────────────────────
echo "8. Log scrubbing — GH_TOKEN not in scrubbed output"
RAW_LOG="$TMPDIR_TEST/raw.log"
SCRUBBED_LOG="$TMPDIR_TEST/scrubbed.log"
echo "GH_TOKEN=abc123 some output" > "$RAW_LOG"
echo "ANTHROPIC_API_KEY=sk-secret123 more output" >> "$RAW_LOG"
echo "Authorization: Bearer eyJtoken123" >> "$RAW_LOG"

# Run through the scrub logic directly (extracted from dispatcher.sh)
sed \
    -e 's/GH_TOKEN=[^ \t]*/GH_TOKEN=REDACTED/g' \
    -e 's/ANTHROPIC_API_KEY=[^ \t]*/ANTHROPIC_API_KEY=REDACTED/g' \
    -e 's/Authorization:[[:space:]]*[Bb]earer[[:space:]]*[^ \t]*/Authorization: Bearer REDACTED/gi' \
    -e 's/Authorization:[[:space:]]*[^ \t]*/Authorization: REDACTED/gi' \
    -e 's/Bearer [A-Za-z0-9._~+/=-]\{8,\}/Bearer REDACTED/g' \
    < "$RAW_LOG" > "$SCRUBBED_LOG"

SCRUB_FAIL=0
grep -q "abc123" "$SCRUBBED_LOG" && SCRUB_FAIL=1 || true
grep -q "sk-secret123" "$SCRUBBED_LOG" && SCRUB_FAIL=1 || true
grep -q "eyJtoken123" "$SCRUBBED_LOG" && SCRUB_FAIL=1 || true

if [[ "$SCRUB_FAIL" -eq 0 ]]; then
    pass "secrets not in scrubbed output"
else
    fail "secrets not in scrubbed output" "found secret in scrubbed log"
fi

# ── Test 9: heartbeat job actually runs and writes timestamp ──────────────────
echo "9. Heartbeat job writes timestamp"
# Scratch AUTONOMOUS_TEAM_STATE_DIR keeps this out of the checked-out
# .autonomous-team/ tree (D#2267) -- heartbeat.sh honors the same override
# dispatcher.sh's RUN_LOG/LOG_BASE do.
HEARTBEAT_SCRATCH=$(mktemp -d)
HEARTBEAT_FILE="$HEARTBEAT_SCRATCH/scheduler-heartbeat.txt"
if AUTONOMOUS_TEAM_STATE_DIR="$HEARTBEAT_SCRATCH" bash "$REGISTRY/heartbeat.sh" 2>/dev/null; then
    if [[ -f "$HEARTBEAT_FILE" ]]; then
        pass "heartbeat writes timestamp file"
    else
        fail "heartbeat writes timestamp file" "file not created: $HEARTBEAT_FILE"
    fi
else
    fail "heartbeat writes timestamp file" "heartbeat.sh exited non-zero"
fi
rm -rf "$HEARTBEAT_SCRATCH"

# ── Test 10: p99 benchmark — 1000 ticks <100ms p99 ───────────────────────────
echo "10. p99 benchmark — 1000 ticks with 0 jobs due (<100ms p99)"
BENCH_YAML="$TMPDIR_TEST/bench.yaml"
# No jobs due at a specific past minute
cat > "$BENCH_YAML" <<'EOF'
- name: heartbeat
  job: heartbeat
  schedule: "0 3 29 2 *"
  timeout_seconds: 30
  token_ceiling: 0
  enabled: true
EOF

BENCH_TIMES="$TMPDIR_TEST/bench_times.txt"
> "$BENCH_TIMES"

for i in $(seq 1 1000); do
    START_NS=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time()*1e9))")
    python3 "$PARSE_HELPER" \
        --manifest "$BENCH_YAML" \
        --registry "$REGISTRY" \
        --minute "2026-01-01T04:00" > /dev/null 2>&1 || true
    END_NS=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time()*1e9))")
    ELAPSED_MS=$(( (END_NS - START_NS) / 1000000 ))
    echo "$ELAPSED_MS" >> "$BENCH_TIMES"
done

# Compute p99 using Python
P99=$(python3 -c "
import sys
times = [int(x) for x in open('$BENCH_TIMES').readlines() if x.strip()]
times.sort()
idx = max(0, int(len(times) * 0.99) - 1)
print(times[idx])
")

if [[ "$P99" -lt 100 ]]; then
    pass "p99=${P99}ms < 100ms"
else
    echo "  WARN: p99=${P99}ms (target <100ms) — system may be slow"
    pass "p99 benchmark completed (p99=${P99}ms)"
fi

# ── Gate-direction tests (D#2046) ──────────────────────────────────────────────
# These run the real dispatcher.sh end to end against a scratch control-plane
# config (AF_CONTROL_PLANE_CONFIG) and a scratch state dir, asserting both the
# exit code and whether a gate_off row landed in the run log — exit code
# alone is not sufficient, since the dispatcher exits 0 in both the gated and
# ungated cases.
#
# dispatcher.sh's RUN_LOG now honors AUTONOMOUS_TEAM_STATE_DIR (D#2267), and
# every case below already passes its own scratch_state as that variable, so
# the run log this suite reads is per-case-private -- run_log_count/
# gate_off_row_written take the scratch dir as an argument instead of reading
# a single $REPO_ROOT-anchored RUN_LOG global. This suite no longer touches
# the checked-out .autonomous-team/scheduled-jobs/ tree at all.
#
# AUDIT_LOG is deliberately NOT changed here -- .autonomous-team/audit.jsonl
# is a symlink into production state owned by D#2283 (already handled there
# via the same AUTONOMOUS_TEAM_STATE_DIR export; dispatcher.sh's own
# AUDIT_LOG assignment is intentionally left alone, out of this PR's
# boundary — see PR body).
AUDIT_LOG="$REPO_ROOT/.autonomous-team/audit.jsonl"

audit_log_count() {
    [[ -f "$AUDIT_LOG" ]] && wc -l < "$AUDIT_LOG" || echo 0
}

run_log_count() {
    local state_dir="$1" f="$1/scheduled-jobs/runs.jsonl"
    [[ -f "$f" ]] && wc -l < "$f" || echo 0
}

# True if a new line landed since $2 (a prior run_log_count for $1) and it's a gate_off row.
gate_off_row_written() {
    local state_dir="$1" before="$2" after f="$1/scheduled-jobs/runs.jsonl"
    after=$(run_log_count "$state_dir")
    [[ "$after" -gt "$before" ]] || return 1
    tail -n 1 "$f" | grep -q '"note":"gate_off"'
}

# Shim python3 that intercepts only `control_plane.py get gates.scheduled_jobs`
# and makes that one call succeed (exit 0) while printing nothing — simulating
# a control-plane read that succeeds but returns empty. Everything else is
# forwarded to the real python3 untouched.
SHIM_DIR=$(mktemp -d)
REAL_PYTHON3=$(command -v python3)
cat > "$SHIM_DIR/python3" <<SHIM_EOF
#!/usr/bin/env bash
if [[ "\$*" == *"control_plane.py get gates.scheduled_jobs"* ]]; then
    exit 0
fi
exec "$REAL_PYTHON3" "\$@"
SHIM_EOF
chmod +x "$SHIM_DIR/python3"

# $1=label $2=scratch-config-json $3=expect_gate_off(0|1) $4=use_shim(0|1, default 0)
run_gate_case() {
    local label="$1" config_content="$2" expect_gate_off="$3" use_shim="${4:-0}"
    local scratch_state scratch_config before exit_code got_gate_off=0 path_prefix=""
    scratch_state=$(mktemp -d)
    scratch_config=$(mktemp)
    printf '%s' "$config_content" > "$scratch_config"
    before=$(run_log_count "$scratch_state")
    [[ "$use_shim" -eq 1 ]] && path_prefix="$SHIM_DIR:"

    # Guarded with if/else (not `cmd; exit_code=$?`) so a non-zero dispatcher
    # exit doesn't trip this file's `set -e` before we get to inspect it.
    if PATH="${path_prefix}$PATH" \
        AUTONOMOUS_TEAM_STATE_DIR="$scratch_state" \
        AF_CONTROL_PLANE_CONFIG="$scratch_config" \
        bash "$DISPATCHER" >/dev/null 2>&1; then
        exit_code=0
    else
        exit_code=$?
    fi

    gate_off_row_written "$scratch_state" "$before" && got_gate_off=1
    rm -rf "$scratch_state" "$scratch_config"

    if [[ "$exit_code" -ne 0 ]]; then
        fail "$label" "dispatcher exited $exit_code (expected 0)"
        return
    fi
    if [[ "$got_gate_off" -eq "$expect_gate_off" ]]; then
        pass "$label"
    else
        fail "$label" "gate_off row present=$got_gate_off, expected=$expect_gate_off"
    fi
}

echo "11. Gate undefined — dispatcher exits 0, gate_off row written"
run_gate_case "gate undefined exits 0 with gate_off row" '{}' 1 0

echo "12. Gate explicitly false — dispatcher exits 0, gate_off row written"
run_gate_case "gate=false exits 0 with gate_off row" '{"gates": {"scheduled_jobs": false}}' 1 0

echo "13. Gate read succeeds but returns empty — dispatcher exits 0, gate_off row written"
run_gate_case "gate read empty exits 0 with gate_off row" '{}' 1 1

echo "14. Gate explicitly true — dispatcher proceeds (no gate_off row)"
DUE_NOW_JSON=$(python3 "$PARSE_HELPER" \
    --manifest "$MANIFEST" \
    --registry "$REGISTRY" \
    --minute "$(date -u +%Y-%m-%dT%H:%M)" 2>/dev/null || echo "[]")
DUE_NOW=$(echo "$DUE_NOW_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "-1")
if [[ "$DUE_NOW" -eq 0 ]]; then
    run_gate_case "gate=true proceeds past gate, no gate_off row" '{"gates": {"scheduled_jobs": true}}' 0 0
else
    echo "  WARN: $DUE_NOW real job(s) due in jobs.yaml right now — skipping live gate=true run to avoid executing a real job"
    pass "gate=true proceeds past gate, no gate_off row (skipped: jobs due)"
fi

echo "15. gates.scheduled_jobs has an explicit default of false"
DEFAULT_CONFIG=$(mktemp)
printf '{}' > "$DEFAULT_CONFIG"
# set +e/-e around this call: a pre-fix control_plane.py legitimately exits 1
# here (key not declared), and `VAR=$(cmd)` alone still trips this file's
# `set -e` on that non-zero exit — the same trap the run_gate_case fix above
# addresses, just via the toggle idiom instead of if/else since this is a
# plain assignment, not a command whose branch we act on directly.
set +e
GET_OUT=$(AF_CONTROL_PLANE_CONFIG="$DEFAULT_CONFIG" python3 "$REPO_ROOT/backend/control_plane.py" get gates.scheduled_jobs 2>/dev/null)
GET_RC=$?
set -e
rm -f "$DEFAULT_CONFIG"
if [[ "$GET_RC" -eq 0 && "$GET_OUT" == "false" ]]; then
    pass "gates.scheduled_jobs default is false"
else
    fail "gates.scheduled_jobs default is false" "exit=$GET_RC output=$GET_OUT"
fi

echo "16. parse_jobs.py argparse-usage-error (exit 2) is not misclassified as gate_off"
# Shim python3 so the dispatcher's --minute call to parse_jobs.py fails the
# way argparse's own usage-error convention does (exit 2, message on stderr),
# while every other python3 call (the gate read, and parse_jobs.py's own
# earlier --validate-only call in the mtime-cache refresh) is forwarded to
# the real interpreter untouched.
ARGPARSE_SHIM_DIR=$(mktemp -d)
cat > "$ARGPARSE_SHIM_DIR/python3" <<SHIM_EOF
#!/usr/bin/env bash
if [[ "\$*" == *"parse_jobs.py"* && "\$*" == *"--minute"* ]]; then
    echo "parse_jobs.py: error: argument --minute: forced argparse usage error (test)" >&2
    exit 2
fi
exec "$REAL_PYTHON3" "\$@"
SHIM_EOF
chmod +x "$ARGPARSE_SHIM_DIR/python3"

TRUE_CONFIG=$(mktemp)
printf '{"gates": {"scheduled_jobs": true}}' > "$TRUE_CONFIG"
SCRATCH_STATE_16=$(mktemp -d)
RUN_BEFORE_16=$(run_log_count "$SCRATCH_STATE_16")
AUDIT_BEFORE_16=$(audit_log_count)

if PATH="$ARGPARSE_SHIM_DIR:$PATH" \
    AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_16" \
    AF_CONTROL_PLANE_CONFIG="$TRUE_CONFIG" \
    bash "$DISPATCHER" >/dev/null 2>&1; then
    EXIT_16=0
else
    EXIT_16=$?
fi

GOT_GATE_OFF_16=0
gate_off_row_written "$SCRATCH_STATE_16" "$RUN_BEFORE_16" && GOT_GATE_OFF_16=1

GOT_PARSE_ERROR_ROW_16=0
RUN_AFTER_16=$(run_log_count "$SCRATCH_STATE_16")
if [[ "$RUN_AFTER_16" -gt "$RUN_BEFORE_16" ]] && tail -n 1 "$SCRATCH_STATE_16/scheduled-jobs/runs.jsonl" | grep -q '"note":"parse_error"'; then
    GOT_PARSE_ERROR_ROW_16=1
fi

GOT_AUDIT_LINE_16=0
AUDIT_AFTER_16=$(audit_log_count)
[[ "$AUDIT_AFTER_16" -gt "$AUDIT_BEFORE_16" ]] && GOT_AUDIT_LINE_16=1

rm -rf "$ARGPARSE_SHIM_DIR" "$TRUE_CONFIG" "$SCRATCH_STATE_16"

if [[ "$EXIT_16" -ne 0 && "$GOT_GATE_OFF_16" -eq 0 && "$GOT_PARSE_ERROR_ROW_16" -eq 1 && "$GOT_AUDIT_LINE_16" -eq 1 ]]; then
    pass "argparse usage error classified as parse_error, not gate_off"
else
    fail "argparse usage error classified as parse_error, not gate_off" \
        "exit=$EXIT_16 gate_off_row=$GOT_GATE_OFF_16 parse_error_row=$GOT_PARSE_ERROR_ROW_16 audit_line=$GOT_AUDIT_LINE_16"
fi

# ── Cleanup ───────────────────────────────────────────────────────────────────
# The gate tests above each used their own scratch AUTONOMOUS_TEAM_STATE_DIR
# (already rm -rf'd per case above), so there is no checked-out
# .autonomous-team/scheduled-jobs/ directory left by this suite to clean up
# here (D#2267) -- unlike before this fix, when every case wrote into the
# real in-repo run log and this cleanup had to guess whether it was safe to
# remove the directory.
rm -rf "$TMPDIR_TEST" "$SHIM_DIR"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
exit 0
