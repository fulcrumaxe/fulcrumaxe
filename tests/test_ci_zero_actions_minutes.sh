#!/usr/bin/env bash
# tests/test_ci_zero_actions_minutes.sh — proves the CI-gate suites are offline.
#
# The whole point of the CI_DISABLED work is that Actions minutes are the thing
# being conserved. A test suite that verifies the kill switch by triggering a
# real workflow run spends exactly what the switch exists to save, so this
# runs both CI-gate suites with the GitHub CLI replaced on PATH by a stub that
# records every invocation and exits 127, and then asserts the recording is
# empty. Not "the suites passed" — that they never reached for the network at
# all.
#
# The suites' own hermetic stubs (tests/test_merge_and_hook.sh prepends its
# own tmpdir/bin) take precedence over this one, which is fine: what matters
# is that nothing escapes to the real `gh`.
#
# Run: bash tests/test_ci_zero_actions_minutes.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0

STUB_DIR="$(mktemp -d)"
CALL_LOG="$STUB_DIR/gh-calls.log"
: > "$CALL_LOG"

cat > "$STUB_DIR/gh" <<STUBEOF
#!/usr/bin/env bash
echo "\$*" >> "$CALL_LOG"
exit 127
STUBEOF
chmod +x "$STUB_DIR/gh"

for suite in tests/test_ci_status_check.sh tests/test_merge_and_hook.sh; do
  echo "=== running $suite with a network-refusing gh on PATH ==="
  if env PATH="$STUB_DIR:$PATH" bash "$REPO_ROOT/$suite" >"$STUB_DIR/out.txt" 2>&1; then
    echo "  PASS: $suite passes offline"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $suite did not pass offline"; tail -25 "$STUB_DIR/out.txt"; FAIL=$((FAIL + 1))
  fi
done

if [ ! -s "$CALL_LOG" ]; then
  echo "  PASS: the real gh was never invoked — zero Actions minutes, zero API calls"
  PASS=$((PASS + 1))
else
  echo "  FAIL: the real gh was invoked $(wc -l < "$CALL_LOG") time(s):"
  sed 's/^/        /' "$CALL_LOG"
  FAIL=$((FAIL + 1))
fi

rm -rf "$STUB_DIR"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
