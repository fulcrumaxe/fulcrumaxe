#!/usr/bin/env bash
# test_dial_state_summary_job.sh — unit tests for dial-state-summary.sh
#
# Tests:
#   1. Gate off: job exits 0 without doing anything, no rotate-team-log calls
#   2. Gate on, all-default registry: summary says "all classes at default"
#   3. Gate on, non-default class (level != ceiling): summary names the class
#   4. Gate on, class with active directives: summary names the class with directive count
#   5. Gate on, multiple non-default classes: summary names all of them
#
# Strategy: inject mock python3 and a mock rotate-team-log.sh into PATH.
# The mock python3 intercepts control_plane.py (returns gate value from a temp file)
# and dial_registry calls (the job uses "python3 -" with a heredoc).
# For the registry call, we override the list_directives import via a sitecustomize trick.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
JOB_SCRIPT="$REPO_ROOT/scripts/schedule/jobs/dial-state-summary.sh"

PASS=0
FAIL=0
TOTAL=0

pass() { local name="$1"; echo "  PASS: $name"; PASS=$((PASS+1)); TOTAL=$((TOTAL+1)); }
fail() { local name="$1" msg="$2"; echo "  FAIL: $name — $msg"; FAIL=$((FAIL+1)); TOTAL=$((TOTAL+1)); }

echo "=== dial-state-summary job tests ==="
echo ""

# ── Shared setup ──────────────────────────────────────────────────────────────
# All scratch files (gate, data, log, job stdout) live under this one
# mktemp'd dir — was a set of fixed /tmp/dial_summary_* names shared across
# any concurrently-running copy of this suite (D#2254).
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

MOCK_BIN="$TMPDIR_TEST/bin"
MOCK_SCRIPTS="$TMPDIR_TEST/scripts"
mkdir -p "$MOCK_BIN" "$MOCK_SCRIPTS"

DIAL_GATE="$TMPDIR_TEST/dial_summary_gate"
DIAL_DATA_FILE="$TMPDIR_TEST/dial_summary_data"
DIAL_LOG="$TMPDIR_TEST/dial_summary_log.txt"
DIAL_OUT="$TMPDIR_TEST/dial_summary_out.txt"

REAL_PYTHON3=$(command -v python3)

# ── Mock rotate-team-log.sh — logs what it receives ──────────────────────────
# The job calls bash "$REPO_ROOT/scripts/rotate-team-log.sh".
# We set REPO_ROOT to TMPDIR_TEST so the job finds our mock.
# The actual job script path stays the same (we don't copy it).
# Deliberately an unquoted heredoc (was quoted 'MOCKLOG') so $DIAL_LOG
# resolves to this suite's own tmpdir when the stub is written; the stub's
# own "$*" is escaped so it stays literal at runtime.
cat > "$MOCK_SCRIPTS/rotate-team-log.sh" <<MOCKLOG
#!/usr/bin/env bash
echo "\$*" >> $DIAL_LOG
exit 0
MOCKLOG
chmod +x "$MOCK_SCRIPTS/rotate-team-log.sh"
# Also put in mock bin for good measure
cp "$MOCK_SCRIPTS/rotate-team-log.sh" "$MOCK_BIN/rotate-team-log.sh"

# ── Mock python3 ──────────────────────────────────────────────────────────────
# Intercepts control_plane.py calls; passes everything else (including heredoc
# "-" scripts) to real python3, but prepends a sys.path override so that
# backend.dial_registry.list_directives() reads from our seeded file.
cat > "$MOCK_BIN/python3" <<MOCKPY
#!/usr/bin/env bash
# Detect control_plane.py calls by inspecting arguments
for arg in "\$@"; do
    case "\$arg" in
        *control_plane.py*)
            GATE=\$(cat $DIAL_GATE 2>/dev/null || echo "false")
            echo "\$GATE"
            exit 0
            ;;
    esac
done

# For all other python3 calls (including heredoc "-" scripts), inject a
# test-override module that monkey-patches list_directives to use test data.
DIAL_DATA=\$(cat $DIAL_DATA_FILE 2>/dev/null || echo "[]")
OVERRIDE_DIR=\$(mktemp -d)
cat > "\$OVERRIDE_DIR/backend_dial_override.py" <<PYMOD
import json
_TEST_DATA = json.loads(r'''\$DIAL_DATA''')
PYMOD
# Prepend the override via PYTHONSTARTUP or sitecustomize is complex;
# Instead, write a wrapper that substitutes the list_directives function
# by running the script with a modified backend module path.

# Write a temp backend/dial_registry.py that returns test data
BACKEND_MOCK="\$OVERRIDE_DIR/backend"
mkdir -p "\$BACKEND_MOCK"
cat > "\$BACKEND_MOCK/__init__.py" <<PY
PY
cat > "\$BACKEND_MOCK/dial_registry.py" <<PYMOD
import json
_TEST_DATA = json.loads(r'''\$DIAL_DATA''')
def list_directives():
    return _TEST_DATA
def revert_expired():
    pass
PYMOD

# Now run the real python3 with the override dir prepended to sys.path
PYTHONPATH="\$OVERRIDE_DIR:\${PYTHONPATH:-}" exec "$REAL_PYTHON3" "\$@"
MOCKPY
chmod +x "$MOCK_BIN/python3"

# Helper: run the job and capture output
# We set REPO_ROOT to TMPDIR_TEST so bash "$REPO_ROOT/scripts/rotate-team-log.sh"
# resolves to our mock.  The backend/ path is still real (REPO_ROOT is only used
# for rotate-team-log.sh in this job).
run_job() {
    rm -f "$DIAL_LOG" && touch "$DIAL_LOG"
    PATH="$MOCK_BIN:$PATH" \
    REPO_ROOT="$TMPDIR_TEST" \
    AUTONOMOUS_TEAM_STATE_DIR="$TMPDIR_TEST/state" \
        bash "$JOB_SCRIPT" > "$DIAL_OUT" 2>&1
    echo $?
}

# ── Test 1: Gate off — exits 0, no log calls ─────────────────────────────────
echo "1. Gate off — exits 0 without posting"
echo "false" > "$DIAL_GATE"
echo "[]" > "$DIAL_DATA_FILE"

RC=$(run_job)

LOG_LINES=$(wc -l < "$DIAL_LOG" | tr -d ' ')

if [[ $RC -eq 0 ]]; then
    pass "gate off: exits 0"
else
    fail "gate off: exits 0" "exit code was $RC (output: $(cat "$DIAL_OUT"))"
fi
if [[ "$LOG_LINES" -eq 0 ]]; then
    pass "gate off: no team-log calls"
else
    fail "gate off: no team-log calls" "got $LOG_LINES log calls"
fi

# ── Test 2: Gate on, all classes at default ───────────────────────────────────
echo "2. Gate on, all-default registry — summary says 'all classes at default'"
echo "true" > "$DIAL_GATE"
cat > "$DIAL_DATA_FILE" <<'JSEOF'
[
  {"class": "agent.spawn", "level": 5, "ceiling": 5, "directives": []},
  {"class": "docs.write",  "level": 5, "ceiling": 5, "directives": []},
  {"class": "tests.add",   "level": 5, "ceiling": 5, "directives": []}
]
JSEOF

RC=$(run_job)
OUT=$(cat "$DIAL_OUT")
LOG_CONTENT=$(cat "$DIAL_LOG" 2>/dev/null || echo "")

if [[ $RC -eq 0 ]]; then
    pass "all-default: exits 0"
else
    fail "all-default: exits 0" "exit code=$RC output=$OUT"
fi
if echo "$OUT" | grep -q "all classes at default"; then
    pass "all-default: output contains 'all classes at default'"
else
    fail "all-default: output contains 'all classes at default'" "got: $OUT"
fi
if echo "$OUT" | grep -q "\[dial-state-summary\].*all classes at default"; then
    pass "all-default: output line prefixed with [dial-state-summary]"
else
    fail "all-default: output line prefixed with [dial-state-summary]" "got: $OUT"
fi

# ── Test 3: Gate on, one class level != ceiling ───────────────────────────────
echo "3. Gate on, agent.spawn level=3 ceiling=5 — summary names agent.spawn"
echo "true" > "$DIAL_GATE"
cat > "$DIAL_DATA_FILE" <<'JSEOF'
[
  {"class": "agent.spawn", "level": 3, "ceiling": 5, "directives": []},
  {"class": "docs.write",  "level": 5, "ceiling": 5, "directives": []}
]
JSEOF

RC=$(run_job)
OUT=$(cat "$DIAL_OUT")
LOG_CONTENT=$(cat "$DIAL_LOG" 2>/dev/null || echo "")

if [[ $RC -eq 0 ]]; then
    pass "level!=ceiling: exits 0"
else
    fail "level!=ceiling: exits 0" "exit code=$RC output=$OUT"
fi
if echo "$OUT" | grep -q "agent.spawn"; then
    pass "level!=ceiling: output names agent.spawn"
else
    fail "level!=ceiling: output names agent.spawn" "got: $OUT"
fi
if echo "$OUT" | grep -q "non-default"; then
    pass "level!=ceiling: output says 'non-default'"
else
    fail "level!=ceiling: output says 'non-default'" "got: $OUT"
fi
if echo "$OUT" | grep -q "done"; then
    pass "level!=ceiling: job reports done"
else
    fail "level!=ceiling: job reports done" "got: $OUT"
fi

# ── Test 4: Gate on, class with active directives ─────────────────────────────
echo "4. Gate on, methodology.change has 2 active directives — summary names it"
echo "true" > "$DIAL_GATE"
cat > "$DIAL_DATA_FILE" <<'JSEOF'
[
  {"class": "methodology.change", "level": 2, "ceiling": 2, "directives": [{"id": "d1"}, {"id": "d2"}]},
  {"class": "docs.write",         "level": 5, "ceiling": 5, "directives": []}
]
JSEOF

RC=$(run_job)
OUT=$(cat "$DIAL_OUT")

if [[ $RC -eq 0 ]]; then
    pass "active-directives: exits 0"
else
    fail "active-directives: exits 0" "exit code=$RC output=$OUT"
fi
if echo "$OUT" | grep -q "methodology.change"; then
    pass "active-directives: output names methodology.change"
else
    fail "active-directives: output names methodology.change" "got: $OUT"
fi
if echo "$OUT" | grep -q "2 directives"; then
    pass "active-directives: output shows directive count"
else
    fail "active-directives: output shows directive count" "got: $OUT"
fi

# ── Test 5: Gate on, multiple non-default classes ─────────────────────────────
echo "5. Gate on, 3 non-default classes — summary names all three"
echo "true" > "$DIAL_GATE"
cat > "$DIAL_DATA_FILE" <<'JSEOF'
[
  {"class": "agent.spawn",        "level": 2, "ceiling": 5, "directives": []},
  {"class": "cost.spend",         "level": 1, "ceiling": 5, "directives": []},
  {"class": "methodology.change", "level": 2, "ceiling": 2, "directives": [{"id": "d1"}]},
  {"class": "docs.write",         "level": 5, "ceiling": 5, "directives": []}
]
JSEOF

RC=$(run_job)
OUT=$(cat "$DIAL_OUT")

if [[ $RC -eq 0 ]]; then
    pass "multi-non-default: exits 0"
else
    fail "multi-non-default: exits 0" "exit code=$RC output=$OUT"
fi
if echo "$OUT" | grep -q "agent.spawn" && echo "$OUT" | grep -q "cost.spend" && echo "$OUT" | grep -q "methodology.change"; then
    pass "multi-non-default: all three classes named"
else
    fail "multi-non-default: all three classes named" "got: $OUT"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
exit 0
