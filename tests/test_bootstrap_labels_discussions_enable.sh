#!/usr/bin/env bash
# tests/test_bootstrap_labels_discussions_enable.sh — D#2217 regression test.
#
# scripts/bootstrap-github-labels.sh is the script that already has the
# admin-level `gh` token by the time it runs (it just created 11 labels
# with it) — this test locks in that it also uses that same token to flip
# `has_discussions=true` on the target repo, and that a failure there is
# loud (nonzero exit + the exact manual command), not silently swallowed
# the way the original bug's "0 SPEC_READY" reporting was.
#
# Exercises the REAL script end-to-end with a stubbed `gh` binary on PATH
# (the only offline way to test an external GitHub API call without
# mutating a real repo's settings) — everything except the `gh` executable
# itself is the genuine guarded code path, not a mock of our own logic.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/bootstrap-github-labels.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TMPDIR="$(mktemp -d)"
STUBDIR="$TMPDIR/bin"
mkdir -p "$STUBDIR"
CALL_LOG="$TMPDIR/gh-calls.log"

cat > "$STUBDIR/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GH_STUB_LOG"
if [[ "$1" == "label" && "$2" == "create" ]]; then
  exit 0
fi
if [[ "$1" == "api" ]]; then
  full="$*"
  if [[ "$full" == *"has_discussions=true"* ]]; then
    if [[ "${GH_STUB_FAIL_DISCUSSIONS:-0}" == "1" ]]; then
      echo "stub: HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    exit 0
  fi
fi
exit 0
STUB
chmod +x "$STUBDIR/gh"

echo ""
echo "=== test_bootstrap_labels_discussions_enable ==="

echo ""
echo "--- success path: token has admin, PATCH succeeds ---"
: > "$CALL_LOG"
OUT="$(GH_STUB_LOG="$CALL_LOG" PATH="$STUBDIR:$PATH" bash "$SCRIPT" --repo acme/widgets 2>&1)"
RC=$?

if [[ $RC -eq 0 ]]; then
  pass "script exits 0 when labels + has_discussions both succeed"
else
  fail "expected exit 0, got $RC. Output:\n$OUT"
fi

if grep -qF "api -X PATCH repos/acme/widgets -F has_discussions=true" "$CALL_LOG"; then
  pass "invoked gh api -X PATCH repos/<repo> -F has_discussions=true for the right repo"
else
  fail "did not find the has_discussions PATCH invocation in gh call log:
$(cat "$CALL_LOG")"
fi

if echo "$OUT" | grep -q "GitHub Discussions enabled for acme/widgets"; then
  pass "prints confirmation that Discussions was enabled"
else
  fail "missing confirmation message. Output:
$OUT"
fi

echo ""
echo "--- failure path: token lacks admin, PATCH fails loudly ---"
: > "$CALL_LOG"
OUT2="$(GH_STUB_LOG="$CALL_LOG" GH_STUB_FAIL_DISCUSSIONS=1 PATH="$STUBDIR:$PATH" bash "$SCRIPT" --repo acme/widgets 2>&1)"
RC2=$?

if [[ $RC2 -ne 0 ]]; then
  pass "script exits nonzero when has_discussions PATCH fails"
else
  fail "expected nonzero exit when PATCH fails, got 0"
fi

if echo "$OUT2" | grep -qF "gh api -X PATCH repos/acme/widgets -F has_discussions=true"; then
  pass "warning names the exact manual fallback command"
else
  fail "missing manual fallback command in failure output. Output:
$OUT2"
fi

if echo "$OUT2" | grep -q "queue can never fill"; then
  pass "warning explains the consequence, not just the error"
else
  fail "warning doesn't explain why this matters. Output:
$OUT2"
fi

rm -rf "$TMPDIR"

echo ""
if [[ $FAIL -eq 0 ]]; then
  echo "=== test_bootstrap_labels_discussions_enable: PASS ($PASS/$PASS) ==="
  exit 0
else
  echo "=== test_bootstrap_labels_discussions_enable: FAIL ($FAIL failed, $PASS passed) ==="
  exit 1
fi
