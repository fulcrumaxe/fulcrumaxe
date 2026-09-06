#!/usr/bin/env bash
# tests/test_panel_quorum.sh — D#1924 PR-b.
#
# scripts/lib/panel-helpers.sh's count_specialist_comments counts comments,
# not roles: replay D#1810's shape (cost-analyst x1, security-expert x2,
# technical-architect x0, envelope contract satisfied on all three) and
# ACTUAL=3, EXPECTED=3 — the gate advances with a specialist entirely
# missing, because the duplicate conceals the loss. This suite covers the
# role-based fix in scripts/lib/panel-quorum.sh (specialist_roles_present,
# panel_gate_decide) and the mechanical envelope/read-back enforcement added
# to post_specialist_comment in scripts/lib/panel-helpers.sh.
#
# Run: bash tests/test_panel_quorum.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PANEL_HELPERS="$REPO_ROOT/scripts/lib/panel-helpers.sh"
PANEL_QUORUM="$REPO_ROOT/scripts/lib/panel-quorum.sh"

PASS=0
FAIL=0

# -----------------------------------------------------------------------
# Test harness (same shape as tests/test_panel_helpers_repo_scope.sh)
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

assert_lt() {
  local label="$1" a="$2" b="$3"
  if [ "$a" -lt "$b" ]; then
    echo "  PASS: $label ($a < $b)"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected $a < $b)"; FAIL=$((FAIL + 1))
  fi
}

# -----------------------------------------------------------------------
# Stub gh builders. `gh api graphql ... -f query="..."` puts the query text
# in argv, so a python stub can dispatch on substrings of sys.argv without
# reading stdin, and builds JSON responses with json.dumps (no manual shell
# quoting of comment bodies).
# -----------------------------------------------------------------------
_stub_dir() { mktemp -d; }

# gh that always fails — simulates a broken query (network, bad slug, etc).
_write_fail_stub() {
  local dir="$1"
  cat > "$dir/gh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "$dir/gh"
}

# gh that dispatches on the query text: comments() -> $1 (JSON array of
# comment bodies), discussion body query -> $2 (raw body string).
_write_query_stub() {
  local dir="$1" comments_json="$2" disc_body="$3"
  cat > "$dir/gh" <<EOF
#!/usr/bin/env python3
import sys, json
argv = " ".join(sys.argv)
if "comments(first" in argv:
    print(json.dumps({"data": {"repository": {"discussion": {"comments": {"nodes": $comments_json}}}}}))
elif "{ body }" in argv:
    print(json.dumps({"data": {"repository": {"discussion": {"body": $disc_body}}}}))
else:
    print("{}")
EOF
  chmod +x "$dir/gh"
}

# gh for post_specialist_comment's mutation + read-back pair. The read-back
# body is passed via a file (read at runtime with open().read()) instead of
# being embedded as Python source text — embedding a JSON-ish string
# containing its own quotes directly into the heredoc produces invalid
# Python syntax, which is a bug in the test double, not in the code under
# test.
_write_post_stub() {
  local dir="$1" sent_id="$2" readback_body_file="$3"
  cat > "$dir/gh" <<EOF
#!/usr/bin/env python3
import sys, json
argv = " ".join(sys.argv)
if "addDiscussionComment" in argv:
    print(json.dumps({"data": {"addDiscussionComment": {"comment": {"id": "$sent_id"}}}}))
elif "node(id:" in argv:
    with open("$readback_body_file") as f:
        body = f.read()
    print(json.dumps({"data": {"node": {"body": body}}}))
else:
    print("{}")
EOF
  chmod +x "$dir/gh"
}

# gh that would prove it was invoked, by touching a marker file — used to
# assert post_specialist_comment's envelope check short-circuits before any
# network call.
_write_marker_stub() {
  local dir="$1" marker="$2"
  cat > "$dir/gh" <<EOF
#!/usr/bin/env bash
touch "$marker"
exit 1
EOF
  chmod +x "$dir/gh"
}

_cfg_with_timeout() {
  local minutes="$1"
  local tmpfile
  tmpfile=$(mktemp --suffix='.json')
  cat > "$tmpfile" <<JSON
{
  "gates": {},
  "policies": {"pm": {"discussion_timeout_minutes": $minutes}},
  "settings": {},
  "audit_log": []
}
JSON
  echo "$tmpfile"
}

_now_iso()      { date -u +%Y-%m-%dT%H:%M:%SZ; }
_hours_ago_iso() { date -u -d "-$1 hours" +%Y-%m-%dT%H:%M:%SZ; }

# D#1810's shape via json.dumps-safe python literals: cost-analyst x1,
# security-expert x2, technical-architect x0 — all three envelopes valid.
D1810_COMMENTS='[
  {"body": "## Cost Perspective\n<!-- AGENT_OUTPUT -->\n```json\n{\"agent\": \"cost-analyst\", \"discussion\": 1810, \"verdict\": \"done\"}\n```\n<!-- /AGENT_OUTPUT -->"},
  {"body": "## Security Perspective\n<!-- AGENT_OUTPUT -->\n```json\n{\"agent\": \"security-expert\", \"discussion\": 1810, \"verdict\": \"done\"}\n```\n<!-- /AGENT_OUTPUT -->"},
  {"body": "## Security Perspective (retry)\n<!-- AGENT_OUTPUT -->\n```json\n{\"agent\": \"security-expert\", \"discussion\": 1810, \"verdict\": \"done\"}\n```\n<!-- /AGENT_OUTPUT -->"}
]'

COMPLETE_PANEL_COMMENTS='[
  {"body": "{\"agent\": \"cost-analyst\", \"verdict\": \"done\"}"},
  {"body": "{\"agent\": \"security-expert\", \"verdict\": \"done\"}"},
  {"body": "{\"agent\": \"technical-architect\", \"verdict\": \"done\"}"}
]'

EMPTY_COMMENTS='[]'

# =========================================================================
# specialist_roles_present (item 7, 8)
# =========================================================================
echo "=== specialist_roles_present ==="

STUB=$(_stub_dir); _write_fail_stub "$STUB"
OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; specialist_roles_present 9999")
RC=$?
assert_nonzero "broken query exits non-zero" "$RC"
assert_empty "broken query prints nothing" "$OUT"
rm -rf "$STUB"

STUB=$(_stub_dir); _write_query_stub "$STUB" "$EMPTY_COMMENTS" '""'
OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; specialist_roles_present 9999")
RC=$?
assert_exit_0 "genuine zero-role panel exits 0" "$RC"
assert_empty "genuine zero-role panel prints nothing" "$OUT"
rm -rf "$STUB"

# The load-bearing case: D#1810's exact shape. A duplicate must not conceal
# a missing role — specialist_roles_present must echo strictly fewer lines
# than count_specialist_comments on the identical fixture.
STUB=$(_stub_dir); _write_query_stub "$STUB" "$D1810_COMMENTS" '""'
ROLES_OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; specialist_roles_present 1810")
ROLES_RC=$?
COUNT_OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; count_specialist_comments 1810")
assert_exit_0 "D#1810 shape: specialist_roles_present exits 0" "$ROLES_RC"
assert_equals "D#1810 shape: specialist_roles_present echoes exactly cost-analyst + security-expert" \
  "cost-analyst
security-expert" "$ROLES_OUT"
assert_equals "D#1810 shape: count_specialist_comments still counts 3 (unchanged semantics)" "3" "$COUNT_OUT"
ROLES_LINES=$(echo "$ROLES_OUT" | grep -c .)
assert_lt "D#1810 shape: role count (2) is strictly fewer than comment count (3)" "$ROLES_LINES" "$COUNT_OUT"
rm -rf "$STUB"

STUB=$(_stub_dir); _write_query_stub "$STUB" "$COMPLETE_PANEL_COMMENTS" '""'
OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; specialist_roles_present 2158")
assert_equals "complete panel: all three distinct roles present, sorted" \
  "cost-analyst
security-expert
technical-architect" "$OUT"
rm -rf "$STUB"

# =========================================================================
# panel_gate_decide (items 9, 10)
# =========================================================================
echo ""
echo "=== panel_gate_decide ==="

STUB=$(_stub_dir); _write_fail_stub "$STUB"
OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; panel_gate_decide 9999 'cost-analyst,security-expert,technical-architect'")
RC=$?
assert_nonzero "broken query: panel_gate_decide exits non-zero" "$RC"
assert_empty "broken query: panel_gate_decide prints nothing" "$OUT"
rm -rf "$STUB"

STUB=$(_stub_dir); _write_query_stub "$STUB" "$COMPLETE_PANEL_COMMENTS" '""'
OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; panel_gate_decide 2158 'cost-analyst,security-expert,technical-architect'")
assert_equals "complete panel: panel_gate_decide echoes ready" "ready" "$OUT"
rm -rf "$STUB"

# The D#1810 replay itself: a duplicate never substitutes for the missing
# role. Not timed out (SINCE recent) -> waiting, never ready.
CFG=$(_cfg_with_timeout 30)
SINCE_RECENT=$(_now_iso)
STUB=$(_stub_dir); _write_query_stub "$STUB" "$D1810_COMMENTS" "\"<!-- STATUS:DISCUSSING-needs-panel SINCE:$SINCE_RECENT -->\""
OUT=$(AF_CONTROL_PLANE_CONFIG="$CFG" REPO_ROOT="$REPO_ROOT" PATH="$STUB:$PATH" \
  bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; panel_gate_decide 1810 'cost-analyst,security-expert,technical-architect'")
assert_equals "D#1810 replay: panel_gate_decide is waiting (never ready) with technical-architect missing" \
  "waiting:technical-architect" "$OUT"
rm -rf "$STUB"; rm -f "$CFG"

# Expired SINCE -> timeout, same missing-role set.
CFG=$(_cfg_with_timeout 30)
SINCE_EXPIRED=$(_hours_ago_iso 2)
STUB=$(_stub_dir); _write_query_stub "$STUB" "$D1810_COMMENTS" "\"<!-- STATUS:DISCUSSING-needs-panel SINCE:$SINCE_EXPIRED -->\""
OUT=$(AF_CONTROL_PLANE_CONFIG="$CFG" REPO_ROOT="$REPO_ROOT" PATH="$STUB:$PATH" \
  bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; panel_gate_decide 1810 'cost-analyst,security-expert,technical-architect'")
assert_equals "D#1810 replay: expired SINCE yields timeout:technical-architect" \
  "timeout:technical-architect" "$OUT"
rm -rf "$STUB"; rm -f "$CFG"

# Missing/unparseable SINCE fails open toward waiting, never toward ready
# or a false timeout.
CFG=$(_cfg_with_timeout 30)
STUB=$(_stub_dir); _write_query_stub "$STUB" "$D1810_COMMENTS" '"<!-- STATUS:DISCUSSING-needs-panel -->"'
OUT=$(AF_CONTROL_PLANE_CONFIG="$CFG" REPO_ROOT="$REPO_ROOT" PATH="$STUB:$PATH" \
  bash -c "source '$PANEL_HELPERS'; source '$PANEL_QUORUM'; panel_gate_decide 1810 'cost-analyst,security-expert,technical-architect'")
assert_equals "missing SINCE fails open to waiting, not timeout or ready" \
  "waiting:technical-architect" "$OUT"
rm -rf "$STUB"; rm -f "$CFG"

# =========================================================================
# post_specialist_comment (items 13, 14)
# =========================================================================
echo ""
echo "=== post_specialist_comment ==="

MARKER=$(mktemp -u)
STUB=$(_stub_dir); _write_marker_stub "$STUB" "$MARKER"
OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; post_specialist_comment 'D_kwDOtest' 'cost-analyst' '{\"agent\": \"security-expert\"}' 2>/dev/null")
RC=$?
assert_nonzero "envelope/role mismatch: refuses, exits non-zero" "$RC"
assert_empty "envelope/role mismatch: no stdout" "$OUT"
if [ -f "$MARKER" ]; then
  echo "  FAIL: envelope/role mismatch: gh was invoked (marker exists) — should short-circuit before any call"
  FAIL=$((FAIL + 1))
else
  echo "  PASS: envelope/role mismatch: gh was never invoked"
  PASS=$((PASS + 1))
fi
rm -rf "$STUB"; rm -f "$MARKER"

BODY_MATCH='{"agent": "cost-analyst", "verdict": "done"}'
READBACK_FILE=$(mktemp); printf '%s' "$BODY_MATCH" > "$READBACK_FILE"
STUB=$(_stub_dir); _write_post_stub "$STUB" "ID456" "$READBACK_FILE"
OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; post_specialist_comment 'D_kwDOtest' 'cost-analyst' '$BODY_MATCH'" 2>/dev/null)
RC=$?
assert_exit_0 "read-back matches: exits 0" "$RC"
assert_equals "read-back matches: echoes the verified comment id" "ID456" "$OUT"
rm -rf "$STUB"; rm -f "$READBACK_FILE"

BODY_SENT='{"agent": "cost-analyst", "verdict": "done"}'
BODY_READBACK='{"agent": "cost-analyst", "verdict": "STALE-RETRY"}'
READBACK_FILE=$(mktemp); printf '%s' "$BODY_READBACK" > "$READBACK_FILE"
STUB=$(_stub_dir); _write_post_stub "$STUB" "ID789" "$READBACK_FILE"
OUT=$(PATH="$STUB:$PATH" bash -c "source '$PANEL_HELPERS'; post_specialist_comment 'D_kwDOtest' 'cost-analyst' '$BODY_SENT'" 2>/dev/null)
RC=$?
assert_nonzero "read-back mismatch: exits non-zero" "$RC"
assert_empty "read-back mismatch: no stdout" "$OUT"
rm -rf "$STUB"; rm -f "$READBACK_FILE"

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
