#!/usr/bin/env bash
# tests/test_panel_helpers_repo_scope.sh — D#2156.
#
# scripts/lib/panel-helpers.sh hardcoded the repo name `autonomous-forever`
# in three GraphQL queries. It kept working today only because GitHub still
# resolves that name through a rename redirect to `fulcrumaxe` — this is a
# Repo Scope Invariant violation, not a live break.
#
# Two of the three functions also could not tell "zero comments" apart from
# "the query broke": count_specialist_comments and get_discussion_id both
# converged every failure path on printing a bare 0 / empty string with exit
# status 0. The caller at scripts/loop-phased-step5.sh:1047 then re-swallowed
# even a distinguishable failure with `|| echo 0`, so a broken quorum query
# stalled a Discussion in DISCUSSING-needs-panel forever, logging a line
# ("waiting for specialist comments (0/N) — no action this iteration") that
# is byte-identical to the line for a panel that just hasn't posted yet.
# Fixing only panel-helpers.sh does not fix that stall — the call site has to
# stop re-swallowing too. Both files are covered below.
#
# set_discussion_status already propagated failure via `|| return 1` before
# this fix — only its hardcoded repo slug changes here, not its error
# handling.
#
# Run: bash tests/test_panel_helpers_repo_scope.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PANEL_HELPERS="$REPO_ROOT/scripts/lib/panel-helpers.sh"
STEP5_SCRIPT="$REPO_ROOT/scripts/loop-phased-step5.sh"

PASS=0
FAIL=0
SKIP=0

# -----------------------------------------------------------------------
# Test harness
# -----------------------------------------------------------------------
assert_exit_0() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then
    echo "  PASS: $label (exit 0)"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected exit 0, got $rc)"; FAIL=$((FAIL + 1))
  fi
}

assert_nonzero() {
  local label="$1" rc="$2"
  if [ "$rc" -ne 0 ]; then
    echo "  PASS: $label (exit $rc)"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected non-zero exit, got 0)"; FAIL=$((FAIL + 1))
  fi
}

assert_equals() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    echo "  PASS: $label"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected [$expected], got [$actual])"; FAIL=$((FAIL + 1))
  fi
}

assert_empty() {
  local label="$1" actual="$2"
  if [ -z "$actual" ]; then
    echo "  PASS: $label"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected empty stdout, got [$actual])"; FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF -- "$needle"; then
    echo "  PASS: $label"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label"
    echo "        expected to contain: $needle"
    echo "        actual output:"
    echo "$haystack" | head -20 | sed 's/^/          /'
    FAIL=$((FAIL + 1))
  fi
}

assert_not_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF -- "$needle"; then
    echo "  FAIL: $label"
    echo "        expected NOT to contain: $needle"
    echo "        actual output:"
    echo "$haystack" | head -20 | sed 's/^/          /'
    FAIL=$((FAIL + 1))
  else
    echo "  PASS: $label"; PASS=$((PASS + 1))
  fi
}

skip_check() {
  local label="$1"
  echo "  SKIP: $label"
  SKIP=$((SKIP + 1))
}

# Independently computes the specialist-comment count for a live Discussion —
# a separate GraphQL query and a separate scan of the comment bodies, not a
# call into count_specialist_comments. This is what the live assertion below
# diffs against instead of a remembered snapshot value. Same "agent field in
# SPECIALIST_ROLES" definition as panel-helpers.sh's count on purpose — the
# point is freshness (queried this run) not an independent definition.
_independent_specialist_comment_count() {
  local disc_num="$1"
  gh api graphql -f query="
    query {
      repository(owner:\"autonomous-agent-7\", name:\"fulcrumaxe\") {
        discussion(number: $disc_num) {
          comments(first: 50) { nodes { body } }
        }
      }
    }
  " 2>/dev/null | python3 -c "
import json, sys, re

SPECIALIST_ROLES = {
    'technical-architect', 'security-expert', 'cost-analyst',
    'product-owner', 'performance-expert'
}

data = json.load(sys.stdin)
comments = data['data']['repository']['discussion']['comments']['nodes']
count = 0
for c in comments:
    body = c.get('body', '')
    m = re.search(r'\"agent\"\s*:\s*\"([^\"]+)\"', body)
    if m and m.group(1) in SPECIALIST_ROLES:
        count += 1
print(count)
" 2>/dev/null
}

# -----------------------------------------------------------------------
# Static checks — repo scope, slug centralization, call-site re-swallow
# -----------------------------------------------------------------------
echo "=== Static checks ==="

AF_COUNT=$(grep -c 'autonomous-forever' "$PANEL_HELPERS")
assert_equals "no 'autonomous-forever' left in panel-helpers.sh" "0" "$AF_COUNT"

SLUG_SOURCE_LINES=$(grep -c 'autonomous-agent-7/fulcrumaxe\|config.json' "$PANEL_HELPERS")
if [ "$SLUG_SOURCE_LINES" -ge 1 ]; then
  echo "  PASS: slug is resolved from config.json with a hard-coded fallback ($SLUG_SOURCE_LINES matching line(s))"
  PASS=$((PASS + 1))
else
  echo "  FAIL: expected a config.json read or hard-coded fallback slug, found neither"
  FAIL=$((FAIL + 1))
fi

FULCRUMAXE_OCCURRENCES=$(grep -o 'fulcrumaxe' "$PANEL_HELPERS" | wc -l | tr -d ' ')
if [ "$FULCRUMAXE_OCCURRENCES" -le 2 ]; then
  echo "  PASS: literal 'fulcrumaxe' appears at most twice ($FULCRUMAXE_OCCURRENCES occurrence(s)) — slug is centralized, not per-function"
  PASS=$((PASS + 1))
else
  echo "  FAIL: literal 'fulcrumaxe' appears $FULCRUMAXE_OCCURRENCES times — expected <= 2 (one per function would mean it isn't centralized)"
  FAIL=$((FAIL + 1))
fi

RESWALLOW_COUNT=$(grep -c 'count_specialist_comments "$P_DISC_NUM" 2>/dev/null || echo 0' "$STEP5_SCRIPT")
assert_equals "call site no longer re-swallows count_specialist_comments's failure into echo 0" "0" "$RESWALLOW_COUNT"

bash -n "$PANEL_HELPERS"
assert_exit_0 "bash -n panel-helpers.sh" "$?"
bash -n "$STEP5_SCRIPT"
assert_exit_0 "bash -n loop-phased-step5.sh" "$?"

# -----------------------------------------------------------------------
# Discriminating tests — a broken query must not look like a genuine zero.
# A stub `gh` earlier on PATH that exits 1 and prints nothing simulates the
# query breaking (network error, bad slug, malformed response, etc.).
# -----------------------------------------------------------------------
echo ""
echo "=== Discriminating tests: broken query vs genuine zero ==="

FAIL_STUB_DIR=$(mktemp -d)
cat > "$FAIL_STUB_DIR/gh" <<'STUBEOF'
#!/usr/bin/env bash
exit 1
STUBEOF
chmod +x "$FAIL_STUB_DIR/gh"

STDOUT_CSC_FAIL=$(
  export PATH="$FAIL_STUB_DIR:$PATH"
  source "$PANEL_HELPERS"
  count_specialist_comments 2155
)
RC_CSC_FAIL=$?
assert_nonzero "count_specialist_comments: broken query exits non-zero" "$RC_CSC_FAIL"
assert_empty "count_specialist_comments: broken query prints nothing (not a bare 0)" "$STDOUT_CSC_FAIL"

STDOUT_GDI_FAIL=$(
  export PATH="$FAIL_STUB_DIR:$PATH"
  source "$PANEL_HELPERS"
  get_discussion_id 2156
)
RC_GDI_FAIL=$?
assert_nonzero "get_discussion_id: broken query exits non-zero" "$RC_GDI_FAIL"
assert_empty "get_discussion_id: broken query prints nothing" "$STDOUT_GDI_FAIL"

# Pairing check: a genuine zero-comment panel must still be a clean, non-error
# zero — a stub `gh` that succeeds with an empty comments array pins this.
OK_ZERO_STUB_DIR=$(mktemp -d)
cat > "$OK_ZERO_STUB_DIR/gh" <<'STUBEOF'
#!/usr/bin/env bash
echo '{"data":{"repository":{"discussion":{"comments":{"nodes":[]}}}}}'
STUBEOF
chmod +x "$OK_ZERO_STUB_DIR/gh"

STDOUT_CSC_ZERO=$(
  export PATH="$OK_ZERO_STUB_DIR:$PATH"
  source "$PANEL_HELPERS"
  count_specialist_comments 9999
)
RC_CSC_ZERO=$?
assert_exit_0 "count_specialist_comments: genuine zero-comment panel exits 0" "$RC_CSC_ZERO"
assert_equals "count_specialist_comments: genuine zero-comment panel prints 0" "0" "$STDOUT_CSC_ZERO"

rm -rf "$FAIL_STUB_DIR" "$OK_ZERO_STUB_DIR"

# -----------------------------------------------------------------------
# Gate-does-not-loosen — the DISCUSSING-needs-panel call site in
# loop-phased-step5.sh must (a) log a line containing WARNING that is
# textually distinct from the waiting-for-comments line, and (b) never
# advance the Discussion to DISCUSSING-panel-ready, when the quorum query
# fails. A positive control (genuine zero, no failure) proves the fix
# doesn't make a real zero look like a failure either.
# -----------------------------------------------------------------------
echo ""
echo "=== Gate does not loosen on a broken quorum query ==="

_make_gate_config() {
  local tmpfile
  tmpfile=$(mktemp --suffix='.json')
  cat > "$tmpfile" <<'JSON'
{
  "gates": {
    "phased_orchestration": true,
    "phased_code_review": false,
    "auto_merge": false,
    "security_review": false,
    "budget_check": false
  },
  "policies": {},
  "settings": {},
  "audit_log": []
}
JSON
  echo "$tmpfile"
}

DISCUSSING_BODY='<!-- STATUS:DISCUSSING-needs-panel -->'
DISCUSSING_MOCK_JSON='[{"number":229900,"title":"[Feature] Panel Stall Test","body":"'"$DISCUSSING_BODY"'"}]'

FAIL_STUB_DIR2=$(mktemp -d)
cat > "$FAIL_STUB_DIR2/gh" <<'STUBEOF'
#!/usr/bin/env bash
exit 1
STUBEOF
chmod +x "$FAIL_STUB_DIR2/gh"

CFG_FAIL=$(_make_gate_config)
OUT_FAIL=$(AF_CONTROL_PLANE_CONFIG="$CFG_FAIL" REPO_ROOT="$REPO_ROOT" \
  SPAWN_AGENT=echo DISCUSSING_MOCK="$DISCUSSING_MOCK_JSON" SPEC_READY_MOCK='[]' \
  PATH="$FAIL_STUB_DIR2:$PATH" \
  bash "$STEP5_SCRIPT" 2>&1)
RC_FAIL=$?

assert_exit_0 "step5 exits 0 even when the quorum query fails" "$RC_FAIL"
assert_contains "broken query logs a WARNING" "WARNING" "$OUT_FAIL"
assert_not_contains "broken query does NOT log the waiting-for-comments line" "waiting for specialist comments" "$OUT_FAIL"
assert_not_contains "broken query does NOT advance to DISCUSSING-panel-ready" "DISCUSSING-panel-ready" "$OUT_FAIL"

rm -f "$CFG_FAIL"
rm -rf "$FAIL_STUB_DIR2"

# Positive control: genuine zero comments, working query — must NOT warn.
OK_STUB_DIR2=$(mktemp -d)
cat > "$OK_STUB_DIR2/gh" <<'STUBEOF'
#!/usr/bin/env bash
echo '{"data":{"repository":{"discussion":{"comments":{"nodes":[]}}}}}'
STUBEOF
chmod +x "$OK_STUB_DIR2/gh"

CFG_OK=$(_make_gate_config)
OUT_OK=$(AF_CONTROL_PLANE_CONFIG="$CFG_OK" REPO_ROOT="$REPO_ROOT" \
  SPAWN_AGENT=echo DISCUSSING_MOCK="$DISCUSSING_MOCK_JSON" SPEC_READY_MOCK='[]' \
  PATH="$OK_STUB_DIR2:$PATH" \
  bash "$STEP5_SCRIPT" 2>&1)
RC_OK=$?

assert_exit_0 "step5 exits 0 on a genuine zero-comment panel" "$RC_OK"
# D#1924 PR-b: the gate now names missing roles instead of printing only
# ACTUAL/EXPECTED (item 12) — updated to match, same behavior asserted.
assert_contains "genuine zero still logs a waiting line naming the missing roles" "waiting for specialist roles (" "$OUT_OK"
assert_not_contains "genuine zero does NOT log a WARNING" "WARNING" "$OUT_OK"

rm -f "$CFG_OK"
rm -rf "$OK_STUB_DIR2"

# -----------------------------------------------------------------------
# Live block — success path unchanged, skipped when not authenticated so
# the suite stays runnable offline.
# -----------------------------------------------------------------------
echo ""
if gh auth status >/dev/null 2>&1; then
  echo "=== Live checks (gh authenticated) ==="

  LIVE_ID_2156=$(bash -c "source '$PANEL_HELPERS'; get_discussion_id 2156")
  RC_LIVE_2156=$?
  assert_exit_0 "get_discussion_id 2156 (live)" "$RC_LIVE_2156"
  assert_equals "get_discussion_id 2156 (live) returns the known node id" "D_kwDOR_WlRM4Aott0" "$LIVE_ID_2156"

  LIVE_ID_2158=$(bash -c "source '$PANEL_HELPERS'; get_discussion_id 2158")
  RC_LIVE_2158=$?
  assert_exit_0 "get_discussion_id 2158 (live)" "$RC_LIVE_2158"
  assert_equals "get_discussion_id 2158 (live) returns the known node id" "D_kwDOR_WlRM4Aotuz" "$LIVE_ID_2158"

  LIVE_COUNT_2158=$(bash -c "source '$PANEL_HELPERS'; count_specialist_comments 2158")
  RC_LIVE_COUNT=$?
  assert_exit_0 "count_specialist_comments 2158 (live) exits 0" "$RC_LIVE_COUNT"

  # Differential, not a pinned snapshot: compute the expected count fresh,
  # from a separate query over the same Discussion's comments, and compare.
  # A Discussion that lost every specialist comment must not make this
  # vacuously pass, so >= 1 is asserted too.
  EXPECTED_COUNT_2158=$(_independent_specialist_comment_count 2158)
  RC_EXPECTED_2158=$?
  assert_exit_0 "independent specialist-comment count for 2158 succeeds" "$RC_EXPECTED_2158"
  assert_equals "count_specialist_comments 2158 (live) matches an independently computed count" \
    "$EXPECTED_COUNT_2158" "$LIVE_COUNT_2158"
  if [ "${LIVE_COUNT_2158:-0}" -ge 1 ] 2>/dev/null; then
    echo "  PASS: count_specialist_comments 2158 (live) is >= 1, not vacuously zero"; PASS=$((PASS + 1))
  else
    echo "  FAIL: count_specialist_comments 2158 (live) expected >= 1, got [$LIVE_COUNT_2158]"; FAIL=$((FAIL + 1))
  fi
else
  echo "=== Live checks skipped (gh not authenticated) ==="
  skip_check "get_discussion_id 2156 (live)"
  skip_check "get_discussion_id 2156 (live) returns the known node id"
  skip_check "get_discussion_id 2158 (live)"
  skip_check "get_discussion_id 2158 (live) returns the known node id"
  skip_check "count_specialist_comments 2158 (live) exits 0"
  skip_check "independent specialist-comment count for 2158 succeeds"
  skip_check "count_specialist_comments 2158 (live) matches an independently computed count"
  skip_check "count_specialist_comments 2158 (live) is >= 1, not vacuously zero"
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "========================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
