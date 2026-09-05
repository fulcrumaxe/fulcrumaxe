#!/usr/bin/env bash
# tests/test_security_trigger_diagnostic.sh
#
# The security gate's own explanation of why it fired must survive to the
# operator.
#
# detect_security_trigger() fails CLOSED on an unresolvable code plane: it
# returns 0, "triggered", because _check_security_trigger's contract is
# 0 = triggered and the merging phase passes that straight through, so any
# non-zero value reads as "no security review needed". That direction is right
# and is tested elsewhere.
#
# What this file covers is the other half. The block message an operator sees
# said "security trigger detected in diff" — the wrong diagnosis for a missing
# config key — and `detect_security_trigger "$pr" 2>/dev/null` threw away the
# one line that said otherwise. Fail-closed-but-silent is tolerable for a gate.
# Fail-closed-but-misattributed sends whoever hits it to read diffs.
#
# The function under test is EXTRACTED FROM THE REAL SCRIPT at run time rather
# than reproduced here, so deleting or renaming it fails this suite instead of
# leaving a green test of a copy.
#
# Run: bash tests/test_security_trigger_diagnostic.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="$REPO_ROOT/scripts/loop-phased-step5.sh"

PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL: $1"; echo "        $2"; FAIL=$((FAIL + 1)); }
check() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected [$3], got [$2]"; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ── Extract the real function ────────────────────────────────────────────────
sed -n '/^_check_security_trigger() {$/,/^}$/p' "$TARGET" > "$WORK/fn.sh"
if [ ! -s "$WORK/fn.sh" ]; then
  echo "FAIL: could not extract _check_security_trigger from $TARGET —"
  echo "      it was renamed or removed, and this suite is now testing nothing."
  exit 1
fi

run_case() {
  # $1 = stderr the callee emits, $2 = its exit status
  local stub_err="$1" stub_rc="$2"
  cat > "$WORK/driver.sh" <<DRIVER
set -uo pipefail
_SECURITY_TRIGGER_REASON=""
_LOGGED="$WORK/logged.txt"
: > "\$_LOGGED"
_log() { printf '%s\n' "\$*" >> "\$_LOGGED"; }
detect_security_trigger() {
  [ -n "$stub_err" ] && printf '%s\n' "$stub_err" >&2
  printf 'this is stdout and must not be captured as a reason\n'
  return $stub_rc
}
source "$WORK/fn.sh"
_check_security_trigger 123
printf 'rc=%s\n' "\$?"
printf 'reason=%s\n' "\$_SECURITY_TRIGGER_REASON"
DRIVER
  bash "$WORK/driver.sh" 2>/dev/null
}

echo "=== 1. an unresolvable plane: the reason survives and is logged ==="
OUT=$(run_case "[security-trigger] ERROR: could not resolve the code repo" 0)
REASON=$(printf '%s' "$OUT" | sed -n 's/^reason=//p')
RC=$(printf '%s' "$OUT" | sed -n 's/^rc=//p')

check "still fails closed (rc=0 means triggered)" "$RC" "0"
case "$REASON" in
  *"could not resolve the code repo"*)
    ok "the reason reaches the caller instead of /dev/null" ;;
  *)
    bad "the reason reaches the caller instead of /dev/null" "reason=[$REASON]" ;;
esac
if grep -q "could not resolve the code repo" "$WORK/logged.txt" 2>/dev/null; then
  ok "the reason is logged, not just held in a variable"
else
  bad "the reason is logged, not just held in a variable" "$(cat "$WORK/logged.txt" 2>/dev/null)"
fi
case "$REASON" in
  *"must not be captured"*)
    bad "stdout is not mistaken for the reason" "reason=[$REASON]" ;;
  *)
    ok "stdout is not mistaken for the reason" ;;
esac

echo ""
echo "=== 2. an ordinary quiet run: no reason, status passed through ==="
OUT2=$(run_case "" 1)
REASON2=$(printf '%s' "$OUT2" | sed -n 's/^reason=//p')
RC2=$(printf '%s' "$OUT2" | sed -n 's/^rc=//p')
check "not triggered is still not triggered" "$RC2" "1"
check "no reason invented when the callee said nothing" "$REASON2" ""

echo ""
echo "=== 3. the block message names the real cause ==="
# Driven through the real script, which is why _check_security_trigger honours
# SECURITY_TRIGGER_REASON in test mode: the suite runs it with SPAWN_AGENT=echo
# and never reaches the branch above.
if grep -q 'SECURITY_TRIGGER_REASON' "$TARGET"; then
  ok "the script reads a test-mode reason, so the branch is reachable"
else
  bad "the script reads a test-mode reason, so the branch is reachable" "absent"
fi
if grep -q '_SECURITY_TRIGGER_REASON:-' "$TARGET" && \
   grep -q 'merging blocked — security-review-passed label missing (\${_SECURITY_TRIGGER_REASON})' "$TARGET"; then
  ok "the block message interpolates the reason when there is one"
else
  bad "the block message interpolates the reason when there is one" \
      "$(grep -n 'merging blocked — security-review-passed' "$TARGET" | head -3)"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
echo "PRESUM: pass"
