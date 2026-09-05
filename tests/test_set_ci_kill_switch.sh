#!/usr/bin/env bash
# tests/test_set_ci_kill_switch.sh — scripts/set-ci-kill-switch.sh (D#1944)
#
# CI_DISABLED decides whether anything about a PR is machine-verified before it
# merges, and until now the only record it ever changed was the variable's own
# `updated_at` field — one timestamp, overwritten each time, no actor, no
# history. These tests are about the audit row, not about the write: the write
# is a one-line gh call, the row is the reason the switch is safe to have.
#
# Run: bash tests/test_set_ci_kill_switch.sh
# Makes zero GitHub API calls (CI_KILL_SWITCH_MODE=echo).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/set-ci-kill-switch.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

_run() {
  local audit="$1" current="$2"; shift 2
  env CI_KILL_SWITCH_MODE=echo \
      CI_KILL_SWITCH_CURRENT="$current" \
      CI_KILL_SWITCH_ACTOR="test-actor" \
      CI_STATUS_TEST_MODE=1 \
      CI_STATUS_TEST_AUDIT_FILE="$audit" \
      AUTONOMOUS_TEAM_REPO="autonomous-agent-7/fulcrumaxe" \
      bash "$SCRIPT" "$@" 2>&1
}

# ── KS-1: a real change writes exactly one fully-populated row ───────────────
echo "=== KS-1: false -> true writes one ci_kill_switch_changed row ==="
AUDIT="$(mktemp)"
: > "$AUDIT"
OUT=$(_run "$AUDIT" false true --reason "cutting Actions spend overnight"); RC=$?
if [ "$RC" -eq 0 ]; then pass "KS-1: exits 0"; else fail "KS-1: expected exit 0, got $RC — $OUT"; fi

ROWS=$(grep -c 'ci_kill_switch_changed' "$AUDIT" 2>/dev/null || true)
if [ "${ROWS:-0}" -eq 1 ]; then pass "KS-1: exactly one row"; else fail "KS-1: expected 1 row, got ${ROWS:-0}"; fi

if python3 -c '
import json, re, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
rows = [r for r in rows if r.get("kind") == "ci_kill_switch_changed"]
assert len(rows) == 1, rows
r = rows[0]
for field in ("old", "new", "actor", "ts"):
    assert r.get(field), f"{field} missing or empty: {r}"
assert r["old"] != r["new"], r
assert r["old"] == "false" and r["new"] == "true", r
assert re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", r["ts"]), r["ts"]
assert r["actor"] == "test-actor", r
' "$AUDIT"; then
  pass "KS-1: old/new/actor/ts all present, old != new, ts is ISO-8601 Z"
else
  fail "KS-1: row shape wrong — $(cat "$AUDIT")"
fi
rm -f "$AUDIT"

# ── KS-2: a no-op change writes nothing ─────────────────────────────────────
# A row that records no change is noise, and noise is what stops an audit
# trail being read.
echo ""
echo "=== KS-2: true -> true writes zero rows and exits 0 ==="
AUDIT2="$(mktemp)"
: > "$AUDIT2"
OUT2=$(_run "$AUDIT2" true true); RC2=$?
if [ "$RC2" -eq 0 ]; then pass "KS-2: exits 0"; else fail "KS-2: expected exit 0, got $RC2 — $OUT2"; fi
if [ ! -s "$AUDIT2" ]; then pass "KS-2: audit file is empty"; else fail "KS-2: rows written: $(cat "$AUDIT2")"; fi
if echo "$OUT2" | grep -qF "nothing to change"; then pass "KS-2: says why it did nothing"; else fail "KS-2: silent no-op"; fi
rm -f "$AUDIT2"

# ── KS-3: the reverse direction is audited too ──────────────────────────────
echo ""
echo "=== KS-3: true -> false is audited in the same shape ==="
AUDIT3="$(mktemp)"
: > "$AUDIT3"
OUT3=$(_run "$AUDIT3" true false); RC3=$?
if [ "$RC3" -eq 0 ]; then pass "KS-3: exits 0"; else fail "KS-3: expected exit 0, got $RC3 — $OUT3"; fi
if python3 -c '
import json, sys
r = [json.loads(l) for l in open(sys.argv[1]) if l.strip()][0]
assert r["old"] == "true" and r["new"] == "false", r
' "$AUDIT3"; then
  pass "KS-3: row records true -> false"
else
  fail "KS-3: wrong row — $(cat "$AUDIT3")"
fi
rm -f "$AUDIT3"

# ── KS-4: a bad or missing value is refused ─────────────────────────────────
echo ""
echo "=== KS-4: only 'true' or 'false' is accepted ==="
AUDIT4="$(mktemp)"
: > "$AUDIT4"
OUT4=$(_run "$AUDIT4" false yes); RC4=$?
if [ "$RC4" -ne 0 ]; then pass "KS-4: 'yes' refused"; else fail "KS-4: 'yes' accepted"; fi
OUT4B=$(_run "$AUDIT4" false); RC4B=$?
if [ "$RC4B" -ne 0 ]; then pass "KS-4: no value refused"; else fail "KS-4: missing value accepted"; fi
if [ ! -s "$AUDIT4" ]; then pass "KS-4: no rows written for a refused call"; else fail "KS-4: rows written: $(cat "$AUDIT4")"; fi
rm -f "$AUDIT4"

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
