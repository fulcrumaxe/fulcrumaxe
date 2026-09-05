#!/usr/bin/env bash
# tests/test_pre_spawn_check_sandbox_modify.sh
# Verifies the D#1805 round-3 warn-and-audit path for the sandbox.modify dial:
# a hooks/-touchpoint spawn is ALLOWED (not denied), and the firing is
# recorded on every channel that survives to a real caller — stderr warning,
# stdout JSON field, and a hash-chained audit.jsonl row. Also verifies the
# no-touchpoints path stays completely silent on all three channels.
#
# Runs against the real backend/dial_registry.py and dial_operation_class.py
# (not mocked) with an isolated AUTONOMOUS_TEAM_STATE_DIR so the audit log
# and dial-registry.json never touch real state.
#
# HARD RULE: UNDER NO CIRCUMSTANCES may this test invoke `claude`, `claude -p`,
# `_start_loop_run`, or trigger /loop.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"

PASS=0
FAIL=0

ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

TMPDIR_BASE=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_BASE"; }
trap cleanup EXIT

# Extract a field from the JSON object embedded in pre-spawn-check.sh's stdout,
# using the same first-'{'-to-matching-'}'-line extraction spawn-agent.sh:500
# uses, so this test proves the field survives the real parsing path.
extract_json_field() {
  local out_file="$1" field="$2"
  python3 -c "
import json, sys
path, field = sys.argv[1], sys.argv[2]
with open(path) as f:
    lines = f.readlines()
start = end = None
for i, line in enumerate(lines):
    s = line.strip()
    if s.startswith('{') and start is None:
        start = i
    if s == '}':
        end = i
if start is None or end is None:
    print('')
    sys.exit(0)
d = json.loads(''.join(lines[start:end + 1]))
val = d
for part in field.split('.'):
    if not isinstance(val, dict) or part not in val:
        print('')
        sys.exit(0)
    val = val[part]
print(val if isinstance(val, str) else json.dumps(val))
" "$out_file" "$field" 2>/dev/null || echo ""
}

count_audit_rows() {
  local audit_file="$1" kind="$2"
  python3 -c "
import json, sys
path, kind = sys.argv[1], sys.argv[2]
count = 0
try:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get('kind') == kind:
                    count += 1
            except Exception:
                pass
except FileNotFoundError:
    pass
print(count)
" "$audit_file" "$kind" 2>/dev/null || echo "0"
}

echo "=== test_pre_spawn_check_sandbox_modify ==="
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC1: hooks/ touchpoint -> ALLOWED (exit 0), not denied
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC1: hooks/ touchpoint fires sandbox.modify and is allowed ---"

STATE_DIR="$TMPDIR_BASE/state"
mkdir -p "$STATE_DIR"
OUT="$TMPDIR_BASE/fire.out"
ERR="$TMPDIR_BASE/fire.err"

set +e
AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR" \
  bash "$SCRIPTS_DIR/pre-spawn-check.sh" \
  --role executor --touchpoints "hooks/sandbox_rules.py" \
  --event-id "test-sbx-fire-1" --no-register \
  > "$OUT" 2> "$ERR"
EXIT_CODE=$?
set -e

if [[ "$EXIT_CODE" -eq 0 ]]; then
  ok "AC1: hooks/ touchpoint spawn exits 0 (allowed, not denied)"
else
  fail "AC1: expected exit 0, got $EXIT_CODE (stderr: $(cat "$ERR"))"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC2: stderr carries the loud warning
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC2: stderr warning ---"

if grep -q "WARNING: sandbox.modify dial fired" "$ERR"; then
  ok "AC2: stderr contains the sandbox.modify firing warning"
else
  fail "AC2: warning missing from stderr: $(cat "$ERR")"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC3: stdout JSON carries dial_fired (the channel that survives
# spawn-agent.sh:500's `2>/dev/null` discard of stderr)
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC3: stdout JSON dial_fired field ---"

DF_CLASS=$(extract_json_field "$OUT" "dial_fired.class")
DF_ROLE=$(extract_json_field "$OUT" "dial_fired.role")
DF_TOUCHPOINTS=$(extract_json_field "$OUT" "dial_fired.touchpoints")

if [[ "$DF_CLASS" == "sandbox.modify" ]]; then
  ok "AC3a: stdout JSON dial_fired.class == sandbox.modify"
else
  fail "AC3a: expected dial_fired.class=sandbox.modify, got '$DF_CLASS'"
fi

if [[ "$DF_ROLE" == "executor" ]]; then
  ok "AC3b: stdout JSON dial_fired.role == executor"
else
  fail "AC3b: expected dial_fired.role=executor, got '$DF_ROLE'"
fi

if [[ "$DF_TOUCHPOINTS" == "hooks/sandbox_rules.py" ]]; then
  ok "AC3c: stdout JSON dial_fired.touchpoints == hooks/sandbox_rules.py"
else
  fail "AC3c: expected dial_fired.touchpoints=hooks/sandbox_rules.py, got '$DF_TOUCHPOINTS'"
fi

# audit_written must be a real JSON boolean, not a string. `"false"` and
# `"dry_run"` are both truthy to a consumer writing the natural
# `if payload["dial_fired"]["audit_written"]:` — that read must actually be
# accurate, not merely present.
AW_TYPE_AND_VALUE=$(python3 -c "
import json
path = '$OUT'
with open(path) as f:
    lines = f.readlines()
start = end = None
for i, line in enumerate(lines):
    s = line.strip()
    if s.startswith('{') and start is None:
        start = i
    if s == '}':
        end = i
d = json.loads(''.join(lines[start:end + 1]))
v = d.get('dial_fired', {}).get('audit_written')
print(f'{type(v).__name__}:{v}')
" 2>/dev/null || echo "error")

if [[ "$AW_TYPE_AND_VALUE" == "bool:True" ]]; then
  ok "AC3d: dial_fired.audit_written is a real JSON boolean 'true' on a successful write"
else
  fail "AC3d: expected bool:True for dial_fired.audit_written, got '$AW_TYPE_AND_VALUE'"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC4: audit.jsonl carries a dial_sandbox_modify_fired row with the right shape
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC4: audit row ---"

AUDIT_FILE="$STATE_DIR/audit.jsonl"
AUDIT_COUNT=$(count_audit_rows "$AUDIT_FILE" "dial_sandbox_modify_fired")

if [[ "$AUDIT_COUNT" -ge 1 ]]; then
  ok "AC4a: audit.jsonl has >=1 dial_sandbox_modify_fired row"
else
  fail "AC4a: no dial_sandbox_modify_fired row in $AUDIT_FILE"
fi

AUDIT_ROLE=$(python3 -c "
import json
with open('$AUDIT_FILE') as f:
    rows = [json.loads(l) for l in f if l.strip()]
hits = [r for r in rows if r.get('kind') == 'dial_sandbox_modify_fired']
print(hits[-1].get('role','') if hits else '')
" 2>/dev/null || echo "")
AUDIT_TOUCHPOINTS=$(python3 -c "
import json
with open('$AUDIT_FILE') as f:
    rows = [json.loads(l) for l in f if l.strip()]
hits = [r for r in rows if r.get('kind') == 'dial_sandbox_modify_fired']
print(hits[-1].get('touchpoints','') if hits else '')
" 2>/dev/null || echo "")

if [[ "$AUDIT_ROLE" == "executor" ]]; then
  ok "AC4b: audit row role == executor"
else
  fail "AC4b: expected audit row role=executor, got '$AUDIT_ROLE'"
fi

if [[ "$AUDIT_TOUCHPOINTS" == "hooks/sandbox_rules.py" ]]; then
  ok "AC4c: audit row touchpoints == hooks/sandbox_rules.py"
else
  fail "AC4c: expected audit row touchpoints=hooks/sandbox_rules.py, got '$AUDIT_TOUCHPOINTS'"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC5: second firing chains prev_hash to the first row's real hash, not
# "genesis" again — proves the hash chain is live, not reset per-call.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC5: hash chain ---"

set +e
AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR" \
  bash "$SCRIPTS_DIR/pre-spawn-check.sh" \
  --role executor --touchpoints "hooks/other_rules.py" \
  --event-id "test-sbx-fire-2" --no-register \
  > "$TMPDIR_BASE/fire2.out" 2> "$TMPDIR_BASE/fire2.err"
set -e

CHAIN_OK=$(python3 -c "
import hashlib, json
with open('$AUDIT_FILE') as f:
    lines = [l for l in f if l.strip()]
hits_idx = [i for i, l in enumerate(lines) if json.loads(l).get('kind') == 'dial_sandbox_modify_fired']
if len(hits_idx) < 2:
    print('not_enough_rows')
else:
    first_line = lines[hits_idx[0]].rstrip('\n').encode()
    second_row = json.loads(lines[hits_idx[1]])
    expected = hashlib.sha256(first_line).hexdigest()
    print('true' if second_row.get('prev_hash') == expected else 'false')
" 2>/dev/null || echo "error")

if [[ "$CHAIN_OK" == "true" ]]; then
  ok "AC5: second audit row's prev_hash == sha256 of the first row (chain is live)"
else
  fail "AC5: hash chain check returned '$CHAIN_OK', expected true"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC6: no-touchpoints path is completely silent — no warning, no row, no
# dial_fired JSON key. A firing indicator on every spawn is noise that gets
# filtered, which is how this becomes invisible again.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC6: no-touchpoints path stays silent ---"

STATE_DIR2="$TMPDIR_BASE/state2"
mkdir -p "$STATE_DIR2"
OUT2="$TMPDIR_BASE/notouch.out"
ERR2="$TMPDIR_BASE/notouch.err"

set +e
AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR2" \
  bash "$SCRIPTS_DIR/pre-spawn-check.sh" \
  --role executor --event-id "test-sbx-notouch-1" --no-register \
  > "$OUT2" 2> "$ERR2"
EXIT2=$?
set -e

if [[ "$EXIT2" -eq 0 ]]; then
  ok "AC6a: no-touchpoints spawn still exits 0"
else
  fail "AC6a: expected exit 0, got $EXIT2"
fi

if ! grep -q "sandbox.modify" "$ERR2"; then
  ok "AC6b: no-touchpoints spawn prints no sandbox.modify warning"
else
  fail "AC6b: unexpected sandbox.modify mention in stderr: $(cat "$ERR2")"
fi

DF2=$(extract_json_field "$OUT2" "dial_fired")
if [[ -z "$DF2" ]]; then
  ok "AC6c: no-touchpoints spawn has no dial_fired key in stdout JSON"
else
  fail "AC6c: unexpected dial_fired in stdout JSON: '$DF2'"
fi

if [[ ! -f "$STATE_DIR2/audit.jsonl" ]] || [[ "$(count_audit_rows "$STATE_DIR2/audit.jsonl" "dial_sandbox_modify_fired")" -eq 0 ]]; then
  ok "AC6d: no dial_sandbox_modify_fired row written for no-touchpoints spawn"
else
  fail "AC6d: unexpected audit row written for no-touchpoints spawn"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# AC7: audit write failure surfaces instead of vanishing — the behaviour
# round 3 actually added. A regression restoring the old bare `except: pass`
# would still pass every check above (they only exercise the happy path and
# the stays-silent path), so this is the one that would catch it.
#
# Fixture: chmod 400 on audit.jsonl itself, with the state dir left
# writable. Deliberately NOT chmod 500 on the whole state dir — that has a
# confound where the spawn exits 1 from the unrelated external-intake gate
# (D#1588) failing closed on an unwritable state dir, which would mask
# exactly what this test is trying to isolate.
# ─────────────────────────────────────────────────────────────────────────────
echo "--- AC7: audit write failure surfaces (does not vanish) ---"

STATE_DIR3="$TMPDIR_BASE/state3"
mkdir -p "$STATE_DIR3"
: > "$STATE_DIR3/audit.jsonl"
chmod 400 "$STATE_DIR3/audit.jsonl"

OUT3="$TMPDIR_BASE/rofail.out"
ERR3="$TMPDIR_BASE/rofail.err"

set +e
AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR3" \
  bash "$SCRIPTS_DIR/pre-spawn-check.sh" \
  --role executor --touchpoints "hooks/sandbox_rules.py" \
  --event-id "test-sbx-rofail-1" --no-register \
  > "$OUT3" 2> "$ERR3"
EXIT3=$?
set -e

if [[ "$EXIT3" -eq 0 ]]; then
  ok "AC7a: spawn still proceeds (exit 0) when the audit write fails"
else
  fail "AC7a: expected exit 0 even on audit-write failure, got $EXIT3"
fi

if grep -q "ERROR: dial_sandbox_modify_fired audit write failed" "$ERR3"; then
  ok "AC7b: audit write failure produces an ERROR line on stderr"
else
  fail "AC7b: no audit-write-failure ERROR in stderr: $(cat "$ERR3")"
fi

AW3=$(python3 -c "
import json
path = '$OUT3'
with open(path) as f:
    lines = f.readlines()
start = end = None
for i, line in enumerate(lines):
    s = line.strip()
    if s.startswith('{') and start is None:
        start = i
    if s == '}':
        end = i
d = json.loads(''.join(lines[start:end + 1]))
v = d.get('dial_fired', {}).get('audit_written')
print(f'{type(v).__name__}:{v}')
" 2>/dev/null || echo "error")

if [[ "$AW3" == "bool:False" ]]; then
  ok "AC7c: dial_fired.audit_written is boolean false when the write actually failed"
else
  fail "AC7c: expected bool:False for dial_fired.audit_written, got '$AW3'"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
