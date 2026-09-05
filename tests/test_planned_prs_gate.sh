#!/usr/bin/env bash
# tests/test_planned_prs_gate.sh — fixture suite for scripts/lib/planned-prs-gate.sh
# (D#2272).
#
# scripts/spawn-agent.sh is on the PreToolUse forbidden-command list and cannot be
# executed to test it directly (same reasoning as tests/test_spec_ready_gate.sh),
# so this suite sources the real gate function and exercises it end to end against
# a stubbed `gh` binary on PATH — the only offline way to test the gate's own
# GraphQL fetch without mutating a real Discussion. Everything except the `gh`
# executable itself is the genuine guarded code path, not a mock of our own logic.
#
# Covers Spec (Acceptance) items 4, 5 and 6 for D#2272:
#   4. The gate fails on a Spec missing the marker (body and comments both bare).
#   5. The gate passes when the marker is in a comment, not the body.
#   6. The gate cannot be bypassed by SPAWN_AGENT_ALLOW_NO_PLANNED_PRS alone —
#      the paired _REASON var is required.
# Plus: fetch failure fails closed, and the >100-comments warning fires.
#
# Usage: bash tests/test_planned_prs_gate.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_ROOT
# shellcheck source=scripts/lib/planned-prs-gate.sh
source "$REPO_ROOT/scripts/lib/planned-prs-gate.sh"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

TMPDIR="$(mktemp -d)"
STUBDIR="$TMPDIR/bin"
mkdir -p "$STUBDIR"

# Stub `gh` — responds to the one GraphQL shape planned_prs_gate_check issues,
# keyed by the discussion number embedded in the query text. Anything else
# (label queries, etc.) is not used by this gate and isn't stubbed.
cat > "$STUBDIR/gh" <<'STUB'
#!/usr/bin/env bash
# Only the graphql subcommand is used by planned_prs_gate_check.
if [[ "$1" != "api" || "$2" != "graphql" ]]; then
  exit 1
fi
full="$*"
case "$full" in
  *"discussion(number:100)"*)
    cat <<'JSON'
{"body":"<!-- STATUS:SPEC_READY -->\n\n---\nplanned_prs: 1\n---\n\n## Spec\nOne PR.","comments":{"pageInfo":{"hasNextPage":false},"nodes":[]}}
JSON
    ;;
  *"discussion(number:101)"*)
    cat <<'JSON'
{"body":"<!-- STATUS:SPEC_READY -->\n\n## Spec\nNo frontmatter here at all.","comments":{"pageInfo":{"hasNextPage":false},"nodes":[{"body":"just a comment, no field"}]}}
JSON
    ;;
  *"discussion(number:102)"*)
    cat <<'JSON'
{"body":"<!-- STATUS:SPEC_READY -->\n\n## Spec\nNo frontmatter in the body — it lives in a comment.","comments":{"pageInfo":{"hasNextPage":false},"nodes":[{"body":"## Spec\n\n---\nplanned_prs: 2\n---\n\nTwo planned PRs."}]}}
JSON
    ;;
  *"discussion(number:103)"*)
    # Simulates a failed fetch (gh exits nonzero, no stdout).
    exit 1
    ;;
  *"discussion(number:104)"*)
    cat <<'JSON'
{"body":"<!-- STATUS:SPEC_READY -->\n\n## Spec\nNo frontmatter anywhere.","comments":{"pageInfo":{"hasNextPage":true},"nodes":[{"body":"page 1 of many, no field here"}]}}
JSON
    ;;
  *)
    echo '{}'
    ;;
esac
STUB
chmod +x "$STUBDIR/gh"

echo "=== test_planned_prs_gate ==="

echo ""
echo "--- Item 3 baseline: planned_prs: 1 in the body passes ---"
MSG=$(PATH="$STUBDIR:$PATH" planned_prs_gate_check 100 2>&1)
RC=$?
if [[ $RC -eq 0 ]]; then
  _pass "discussion 100 (planned_prs: 1 in body): gate passes"
else
  _fail "discussion 100: expected pass, got rc=$RC: $MSG"
fi

echo ""
echo "--- Item 4: no planned_prs anywhere — real Discussion, real run ---"
MSG=$(PATH="$STUBDIR:$PATH" planned_prs_gate_check 101 2>&1)
RC=$?
if [[ $RC -eq 1 ]]; then
  _pass "discussion 101 (no field anywhere): gate blocks, rc=1"
else
  _fail "discussion 101: expected rc=1, got rc=$RC: $MSG"
fi
if [[ "$MSG" == *"#101"* ]]; then
  _pass "discussion 101: stderr names the Discussion"
else
  _fail "discussion 101: stderr does not name #101: $MSG"
fi
if [[ "$MSG" == *"planned_prs"* ]]; then
  _pass "discussion 101: stderr states how to fix it (mentions planned_prs)"
else
  _fail "discussion 101: stderr doesn't mention planned_prs: $MSG"
fi

echo ""
echo "--- Item 5: marker lives in a comment, not the body ---"
MSG=$(PATH="$STUBDIR:$PATH" planned_prs_gate_check 102 2>&1)
RC=$?
if [[ $RC -eq 0 ]]; then
  _pass "discussion 102 (planned_prs: 2 in comment only): gate passes, rc=0"
else
  _fail "discussion 102: expected rc=0 (body-only implementation would fail this), got rc=$RC: $MSG"
fi

echo ""
echo "--- Fetch failure fails closed ---"
MSG=$(PATH="$STUBDIR:$PATH" planned_prs_gate_check 103 2>&1)
RC=$?
if [[ $RC -eq 1 ]]; then
  _pass "discussion 103 (fetch failed): gate blocks rather than trusting an unread Spec"
else
  _fail "discussion 103: expected rc=1 on fetch failure, got rc=$RC: $MSG"
fi

echo ""
echo "--- >100 comments: non-fatal warning fires ---"
MSG=$(PATH="$STUBDIR:$PATH" planned_prs_gate_check 104 2>&1)
RC=$?
if [[ $RC -eq 1 ]]; then
  _pass "discussion 104: still blocks (no field found on the page it saw)"
else
  _fail "discussion 104: expected rc=1, got rc=$RC"
fi
if [[ "$MSG" == *"more than 100 comments"* ]]; then
  _pass "discussion 104: warns about truncated comment page"
else
  _fail "discussion 104: missing >100-comments warning: $MSG"
fi

echo ""
echo "--- Item 6: env-var bypass requires BOTH vars, not just the flag ---"
MSG=$(PATH="$STUBDIR:$PATH" SPAWN_AGENT_ALLOW_NO_PLANNED_PRS=1 planned_prs_gate_check 101 2>&1)
RC=$?
if [[ $RC -eq 1 ]]; then
  _pass "flag alone (no _REASON): override refused, gate still blocks"
else
  _fail "flag alone should NOT bypass the gate, got rc=$RC: $MSG"
fi

MSG=$(PATH="$STUBDIR:$PATH" SPAWN_AGENT_ALLOW_NO_PLANNED_PRS=1 SPAWN_AGENT_ALLOW_NO_PLANNED_PRS_REASON="umbrella sub-PR, parent D#9000 owns planned_prs" planned_prs_gate_check 101 2>&1)
RC=$?
if [[ $RC -eq 0 ]]; then
  _pass "both vars set: override accepted, gate passes"
else
  _fail "both vars set should bypass the gate, got rc=$RC: $MSG"
fi
if [[ "$MSG" == *"umbrella sub-PR, parent D#9000 owns planned_prs"* ]]; then
  _pass "override warning includes the stated reason"
else
  _fail "override warning does not surface the reason: $MSG"
fi

rm -rf "$TMPDIR"

echo ""
echo "== Summary: $PASS passed, $FAIL failed =="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
