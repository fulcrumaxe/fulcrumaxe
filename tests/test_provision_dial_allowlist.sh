#!/usr/bin/env bash
# tests/test_provision_dial_allowlist.sh — hermetic tests for
# scripts/provision-dial-allowlist.sh (D#1883 Spec items 1-6, 10, 13).
#
# Every case gets its own `mktemp -d` state dir. Fixture setup/teardown that
# touches dial-directive-allowlist.json goes through python3, never a shell
# redirect — the file is protected by basename, path-independently, against
# every Bash-tool operand (hooks/sandbox_rules.py), even inside a mktemp
# scratch dir. See scripts/provision-dial-allowlist.sh's own header for the
# full rationale.
#
# Exit code: 0 = all passed, non-zero = failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PROVISION_SH="$REPO_ROOT/scripts/provision-dial-allowlist.sh"

PASS=0
FAIL=0

_pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# Captured stdout/stderr for items 2-5 live under one mktemp'd dir rather
# than fixed /tmp/.provision-item*.{out,stdout} names, so a concurrently
# running copy of this suite can't clobber this one's captures (D#2254).
RUN_TMP="$(mktemp -d /tmp/test_provision_dial_allowlist.XXXXXX)"
trap 'rm -rf "$RUN_TMP"' EXIT

echo "=== provision-dial-allowlist.sh tests ==="
echo ""

# ── Item 1: syntax check ─────────────────────────────────────────────────
echo "--- Item 1: bash -n exits 0 ---"
if bash -n "$PROVISION_SH"; then
  _pass "bash -n scripts/provision-dial-allowlist.sh"
else
  _fail "bash -n scripts/provision-dial-allowlist.sh" "syntax error"
fi
echo ""

# ── Item 2: clean dir seeds the dashboard entry ──────────────────────────
echo "--- Item 2: clean dir seeds dashboard entry ---"
DIR2="$(mktemp -d)"
if AUTONOMOUS_TEAM_STATE_DIR="$DIR2" PATH="$PATH" bash "$PROVISION_SH" >$RUN_TMP/provision-item2.out 2>&1; then
  _pass "provisioning exits 0 on a clean dir"
else
  _fail "provisioning exits 0 on a clean dir" "exit $? — $(cat $RUN_TMP/provision-item2.out)"
fi
CHECK2=$(AUTONOMOUS_TEAM_STATE_DIR="$DIR2" python3 -c "
import json, os, pathlib
p = pathlib.Path(os.environ['AUTONOMOUS_TEAM_STATE_DIR']) / 'dial-directive-allowlist.json'
data = json.loads(p.read_text())
print('OK' if {'kind': 'system', 'reason': 'dashboard_rpc'} in data else 'MISSING')
")
if [[ "$CHECK2" == "OK" ]]; then
  _pass "dashboard entry present in seeded allowlist"
else
  _fail "dashboard entry present in seeded allowlist" "$CHECK2"
fi
echo ""

# ── Item 3: idempotent — second run leaves content unchanged ────────────
echo "--- Item 3: idempotent on re-run ---"
SNAP3="$(mktemp -d)/before.json"
AUTONOMOUS_TEAM_STATE_DIR="$DIR2" SNAP_PATH="$SNAP3" python3 -c "
import os, pathlib
src = pathlib.Path(os.environ['AUTONOMOUS_TEAM_STATE_DIR']) / 'dial-directive-allowlist.json'
pathlib.Path(os.environ['SNAP_PATH']).write_text(src.read_text())
"
if AUTONOMOUS_TEAM_STATE_DIR="$DIR2" bash "$PROVISION_SH" >$RUN_TMP/provision-item3.out 2>&1; then
  _pass "second run exits 0"
else
  _fail "second run exits 0" "exit $? — $(cat $RUN_TMP/provision-item3.out)"
fi
UNCHANGED3=$(AUTONOMOUS_TEAM_STATE_DIR="$DIR2" SNAP_PATH="$SNAP3" python3 -c "
import json, os, pathlib
p = pathlib.Path(os.environ['AUTONOMOUS_TEAM_STATE_DIR']) / 'dial-directive-allowlist.json'
before = json.loads(pathlib.Path(os.environ['SNAP_PATH']).read_text())
after = json.loads(p.read_text())
print('OK' if before == after else 'CHANGED')
")
if [[ "$UNCHANGED3" == "OK" ]]; then
  _pass "content unchanged on re-run (no duplicate entry)"
else
  _fail "content unchanged on re-run (no duplicate entry)" "$UNCHANGED3"
fi
rm -rf "$(dirname "$SNAP3")" "$DIR2"
echo ""

# ── Item 4: pre-existing entry survives provisioning (adds, never replaces) ──
echo "--- Item 4: pre-existing entry preserved ---"
DIR4="$(mktemp -d)"
STATE_DIR_4="$DIR4" python3 -c "
import json, os, pathlib
p = pathlib.Path(os.environ['STATE_DIR_4'])
p.mkdir(parents=True, exist_ok=True)
(p / 'dial-directive-allowlist.json').write_text(json.dumps([{'kind': 'github_user', 'login': 'someone-else'}]) + '\n')
"
AUTONOMOUS_TEAM_STATE_DIR="$DIR4" bash "$PROVISION_SH" >$RUN_TMP/provision-item4.out 2>&1
PRESENT4=$(AUTONOMOUS_TEAM_STATE_DIR="$DIR4" python3 -c "
import json, os, pathlib
p = pathlib.Path(os.environ['AUTONOMOUS_TEAM_STATE_DIR']) / 'dial-directive-allowlist.json'
data = json.loads(p.read_text())
print('OK' if {'kind': 'github_user', 'login': 'someone-else'} in data else 'MISSING')
")
if [[ "$PRESENT4" == "OK" ]]; then
  _pass "pre-existing entry still present after provisioning"
else
  _fail "pre-existing entry still present after provisioning" "$PRESENT4"
fi
rm -rf "$DIR4"
echo ""

# ── Item 5: gh unavailable/unauthenticated → warn, don't halt, don't hang ──
echo "--- Item 5: gh absent → warn on stderr, exit 0, no hang ---"
DIR5="$(mktemp -d)"
STUB_BIN="$(mktemp -d)"
# Minimal PATH containing only what bash/python3 need — no gh.
for tool in bash python3 mkdir cat rm env timeout; do
  found="$(command -v "$tool" 2>/dev/null || true)"
  [[ -n "$found" ]] && ln -sf "$found" "$STUB_BIN/$tool"
done
OUT5=$(timeout 15 env -i PATH="$STUB_BIN" HOME="$HOME" AUTONOMOUS_TEAM_STATE_DIR="$DIR5" \
  bash "$PROVISION_SH" 2>&1 1>$RUN_TMP/provision-item5.stdout)
STATUS5=$?
if [[ "$STATUS5" -eq 0 ]]; then
  _pass "exit 0 with gh unavailable"
else
  _fail "exit 0 with gh unavailable" "exit $STATUS5"
fi
if echo "$OUT5" | grep -qi "skip"; then
  _pass "warns on stderr that the operator entry was skipped"
else
  _fail "warns on stderr that the operator entry was skipped" "stderr was: $OUT5"
fi
PRESENT5=$(AUTONOMOUS_TEAM_STATE_DIR="$DIR5" python3 -c "
import json, os, pathlib
p = pathlib.Path(os.environ['AUTONOMOUS_TEAM_STATE_DIR']) / 'dial-directive-allowlist.json'
data = json.loads(p.read_text()) if p.exists() else []
print('OK' if {'kind': 'system', 'reason': 'dashboard_rpc'} in data else 'MISSING')
")
if [[ "$PRESENT5" == "OK" ]]; then
  _pass "dashboard entry still seeded when gh is unavailable"
else
  _fail "dashboard entry still seeded when gh is unavailable" "$PRESENT5"
fi
rm -rf "$DIR5" "$STUB_BIN"
echo ""

# ── Item 6: coldstart-project.sh registers the call, minimally ──────────
echo "--- Item 6: coldstart-project.sh invokes the script ---"
if grep -n 'provision-dial-allowlist' "$REPO_ROOT/scripts/coldstart-project.sh" >/dev/null; then
  _pass "coldstart-project.sh references provision-dial-allowlist.sh"
else
  _fail "coldstart-project.sh references provision-dial-allowlist.sh" "no match"
fi
CALL_SITES=$(grep -c 'provision-dial-allowlist' "$REPO_ROOT/scripts/coldstart-project.sh")
if [[ "$CALL_SITES" -eq 1 ]]; then
  _pass "exactly one call site (no scattered dial logic)"
else
  _fail "exactly one call site (no scattered dial logic)" "found $CALL_SITES"
fi
if ! grep -q 'dial-directive-allowlist' "$REPO_ROOT/scripts/coldstart-project.sh"; then
  _pass "coldstart-project.sh contains no inline allowlist-file logic"
else
  _fail "coldstart-project.sh contains no inline allowlist-file logic" "found a direct reference"
fi
echo ""

# ── Item 10: empty allowlist still denies (deny-all semantics untouched) ──
echo "--- Item 10: empty allowlist still deny-all ---"
DIR10="$(mktemp -d)"
STATE_DIR_10="$DIR10" python3 -c "
import os, pathlib
p = pathlib.Path(os.environ['STATE_DIR_10'])
p.mkdir(parents=True, exist_ok=True)
(p / 'dial-directive-allowlist.json').write_text('[]')
"
DENIED10=$(AUTONOMOUS_TEAM_STATE_DIR="$DIR10" python3 -c "
from backend.rpc import dial_control
try:
    dial_control.handle_set({'name': 'docs.write', 'level': 2, 'ttl': None})
    print('PERMITTED')
except ValueError:
    print('DENIED')
")
if [[ "$DENIED10" == "DENIED" ]]; then
  _pass "empty allowlist still refuses mutations"
else
  _fail "empty allowlist still refuses mutations" "$DENIED10"
fi
rm -rf "$DIR10"
echo ""

# ── Item 13: refusal names a real, runnable command ──────────────────────
echo "--- Item 13: refusal names a real command ---"
DIR13="$(mktemp -d)"
STATE_DIR_13="$DIR13" python3 -c "
import os, pathlib
p = pathlib.Path(os.environ['STATE_DIR_13'])
p.mkdir(parents=True, exist_ok=True)
(p / 'dial-directive-allowlist.json').write_text('[]')
"
MSG13=$(AUTONOMOUS_TEAM_STATE_DIR="$DIR13" python3 -c "
from backend.rpc import dial_control
try:
    dial_control.handle_set({'name': 'docs.write', 'level': 2, 'ttl': None})
    print('NOT_RAISED')
except ValueError as e:
    print(str(e))
")
if echo "$MSG13" | grep -q 'scripts/provision-dial-allowlist.sh'; then
  _pass "refusal message names scripts/provision-dial-allowlist.sh"
else
  _fail "refusal message names scripts/provision-dial-allowlist.sh" "$MSG13"
fi
if [[ -f "$PROVISION_SH" ]]; then
  _pass "the named script exists on disk"
else
  _fail "the named script exists on disk" "not found at $PROVISION_SH"
fi
rm -rf "$DIR13"
echo ""

rm -f $RUN_TMP/provision-item2.out $RUN_TMP/provision-item3.out $RUN_TMP/provision-item4.out $RUN_TMP/provision-item5.stdout

# ── Summary ─────────────────────────────────────────────────────────────
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
