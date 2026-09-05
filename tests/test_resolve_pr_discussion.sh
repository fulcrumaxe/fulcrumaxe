#!/usr/bin/env bash
# tests/test_resolve_pr_discussion.sh — unit tests for the failure diagnostic
# in scripts/lib/resolve-pr-discussion.sh (D#2348 PR-f2).
#
# The bodies below are not invented. Each one is the verbatim stdout of a real
# `gh api graphql ... --jq '.data.repository.discussion.id'` run against the
# live API from the operator host (Linux) on 2026-09-04 with the project token
# — the same seven cases the function's header table records. Keeping the real
# bytes here is the point: the whole diagnostic turns on which shape GitHub
# actually returns, and a hand-written approximation is exactly the kind of
# unmeasured confident value this change exists to correct.
#
# Run: bash tests/test_resolve_pr_discussion.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/resolve-pr-discussion.sh
source "$REPO_ROOT/scripts/lib/resolve-pr-discussion.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# assert_contains <label> <haystack> <needle>
assert_contains() {
  local label="$1" hay="$2" needle="$3"
  case "$hay" in
    *"$needle"*) pass "$label" ;;
    *) fail "$label — expected to find '$needle' in: $hay" ;;
  esac
}

# assert_not_contains <label> <haystack> <needle>
assert_not_contains() {
  local label="$1" hay="$2" needle="$3"
  case "$hay" in
    *"$needle"*) fail "$label — did NOT expect '$needle' in: $hay" ;;
    *) pass "$label" ;;
  esac
}

DISC_REPO="example-org/private-discussions"

# ── Measured body: number exists but is a PR, not a Discussion ──────────────
# fulcrumaxe#2361 is PR #2361. Byte-identical to a number that exists nowhere.
BODY_BAD_NUMBER='{"data":{"repository":{"discussion":null}},"errors":[{"type":"NOT_FOUND","path":["repository","discussion"],"locations":[{"line":1,"column":69}],"message":"Could not resolve to a Discussion with the number of 2361."}]}'

# ── Measured body: repo slug does not exist ────────────────────────────────
BODY_NO_SUCH_REPO='{"data":{"repository":null},"errors":[{"type":"NOT_FOUND","path":["repository"],"locations":[{"line":1,"column":9}],"message":"Could not resolve to a Repository with the name '"'"'autonomous-agent-7/no-such-repo-xyz-42'"'"'."}]}'

# ── Measured body: repo EXISTS and is private; this token cannot read it ───
# Measured against github/github. Identical shape to the line above — GitHub
# answers NOT_FOUND rather than FORBIDDEN so it does not leak whether a
# private repo exists. This collision is the whole reason the message for
# this branch has to name both causes.
BODY_NO_ACCESS='{"data":{"repository":null},"errors":[{"type":"NOT_FOUND","path":["repository"],"locations":[{"line":1,"column":9}],"message":"Could not resolve to a Repository with the name '"'"'github/github'"'"'."}]}'

# ── Measured body: bad credentials ─────────────────────────────────────────
BODY_BAD_CREDS='{ "message": "Bad credentials", "documentation_url": "https://docs.github.com/rest", "status": "401" }'

# ── Measured body: unreachable host — stdout is EMPTY ──────────────────────
BODY_UNREACHABLE=''

echo "=== _rpd_failure_cause: bad number ==="
OUT="$(_rpd_failure_cause "$BODY_BAD_NUMBER" "$DISC_REPO" 2361)"
assert_contains "names the PR body as the thing to fix" "$OUT" "PR body"
assert_contains "names the candidate number" "$OUT" "#2361"
assert_contains "names the repo it looked in" "$OUT" "$DISC_REPO"
assert_not_contains "does not tell the operator to retry" "$OUT" "retry"

echo ""
echo "=== _rpd_failure_cause: repo slug does not exist ==="
OUT="$(_rpd_failure_cause "$BODY_NO_SUCH_REPO" "$DISC_REPO" 2348)"
assert_contains "names the not-existing possibility" "$OUT" "does not exist"
assert_contains "names the token-scope possibility too" "$OUT" "lacks access"
assert_contains "points at the discussion_repo setting" "$OUT" "discussion_repo"
assert_contains "says it is permanent" "$OUT" "Not transient"

echo ""
echo "=== _rpd_failure_cause: repo exists, token cannot read it ==="
# The single likeliest cutover misconfiguration: a CI token scoped to the
# public code repo only. It must NOT be told to go edit a correct PR body.
OUT="$(_rpd_failure_cause "$BODY_NO_ACCESS" "$DISC_REPO" 2348)"
assert_contains "names the token-scope possibility" "$OUT" "lacks access"
assert_contains "names the not-existing possibility too" "$OUT" "does not exist"
assert_not_contains "does not send the operator to the PR body" "$OUT" "PR body"

echo ""
echo "=== _rpd_failure_cause: bad credentials ==="
OUT="$(_rpd_failure_cause "$BODY_BAD_CREDS" "$DISC_REPO" 2348)"
assert_contains "names the token as rejected" "$OUT" "token was rejected"
assert_not_contains "does not blame the PR body" "$OUT" "PR body"

echo ""
echo "=== _rpd_failure_cause: unreachable host (empty body) ==="
# The retry advice lives here and only here — this is the one measured case
# that clears itself.
OUT="$(_rpd_failure_cause "$BODY_UNREACHABLE" "$DISC_REPO" 2348)"
assert_contains "says GitHub was unreachable" "$OUT" "unreachable"
assert_contains "tells the operator to retry" "$OUT" "retry"

OUT="$(_rpd_failure_cause "   " "$DISC_REPO" 2348)"
assert_contains "whitespace-only body is treated as empty" "$OUT" "retry"

echo ""
echo "=== _rpd_failure_cause: unrecognised body ==="
OUT="$(_rpd_failure_cause '{"data":{"repository":{"discussion":{"id":null}}}}' "$DISC_REPO" 2348)"
assert_contains "falls through to a neutral line" "$OUT" "unrecognised"

echo ""
echo "=== the two shapes really are distinct (regression guard) ==="
# A Discussion comment asserted that a no-access repo returns the
# "repository":{"discussion":null} shape. It does not — measured. If someone
# ever "fixes" the branches back the other way, these two assertions fail.
BAD_NUM_OUT="$(_rpd_failure_cause "$BODY_BAD_NUMBER" "$DISC_REPO" 2361)"
NO_ACCESS_OUT="$(_rpd_failure_cause "$BODY_NO_ACCESS" "$DISC_REPO" 2348)"
if [[ "$BAD_NUM_OUT" != "$NO_ACCESS_OUT" ]]; then
  pass "bad-number and no-access produce different guidance"
else
  fail "bad-number and no-access produce identical guidance"
fi

echo ""
echo "=== stderr wiring: the cause line is actually emitted ==="
# Exercise the real loop, not just the helper: stub gh so `gh pr view` yields
# a body with a closing reference and the GraphQL validation fails the way a
# no-access Discussion repo fails.
STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT
cat > "$STUB_DIR/gh" <<STUBEOF
#!/usr/bin/env bash
if [[ "\$1" == "pr" ]]; then
  echo "Fixes D#2348"
  exit 0
fi
printf '%s' '{"data":{"repository":null},"errors":[{"type":"NOT_FOUND","path":["repository"],"message":"Could not resolve to a Repository with the name '"'"'x/y'"'"'."}]}'
exit 1
STUBEOF
chmod +x "$STUB_DIR/gh"

ERR_FILE="$STUB_DIR/stderr.txt"
PATH="$STUB_DIR:$PATH" resolve_pr_discussion 999 "example-org/public-code" "$DISC_REPO" \
  >/dev/null 2>"$ERR_FILE"
RC=$?
ERR="$(cat "$ERR_FILE")"

if [[ "$RC" -ne 0 ]]; then
  pass "unresolvable candidate still returns non-zero (fail-closed contract intact)"
else
  fail "expected non-zero return for an unresolvable candidate, got $RC"
fi
assert_contains "raw body still printed" "$ERR" "did not validate against"
assert_contains "cause line printed" "$ERR" "cause:"
assert_contains "cause names the token scope" "$ERR" "lacks access"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
