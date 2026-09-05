#!/usr/bin/env bash
# tests/test_resolve_spec_text.sh — fixture suite for scripts/lib/resolve-spec-text.sh (D#2008).
#
# Sources the real resolver function (not a paraphrase — the same reasoning
# as tests/test_spec_ready_gate.sh: spawn-agent.sh cannot be executed under
# the sandbox, and this is the only way to exercise the real logic instead
# of a re-implementation of it). Network is faked with a stub `gh` on PATH
# so this stays a pure offline fixture test — no live Discussion reads.
#
# Fixtures:
#   A. Body-only Spec (D#1997's shape) — no linked comment, output == body.
#   B. Spec pinned in a linked comment (D#1944's shape) — body has no
#      ## Spec (Acceptance) heading, but links a frozen-comment URL on
#      line 2; output must carry the comment's body too.
#   C. A discussioncomment link appears, but no comment with that databaseId
#      exists — output is body-only, no crash.
#   D. A discussioncomment link appears on line 15 (past the first-10-lines
#      window) — must be treated as prose, not the authoritative pointer;
#      output is body-only even though a matching comment exists.
#
# Usage: bash tests/test_resolve_spec_text.sh
# Exit 0 = all assertions passed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/resolve-spec-text.sh
source "$REPO_ROOT/scripts/lib/resolve-spec-text.sh"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# ── Fake `gh` on PATH ───────────────────────────────────────────────────────
# Serves canned GraphQL responses keyed off the discussion number embedded in
# the query string (`discussion(number:<N>)`), so resolve_spec_text's own
# argument construction is exercised for real — only the network transport
# is stubbed.
FAKE_BIN_DIR=$(mktemp -d)

cat > "$FAKE_BIN_DIR/gh" <<'FAKE_GH_EOF'
#!/usr/bin/env bash
# Stub gh — only handles `gh api graphql -f query=...`, dispatched by the
# discussion number embedded in the query text. Discussions 1005/1006 also
# branch on whether the query carries an `after:` cursor, to exercise the
# resolver's pagination loop.
if [[ "$1" == "api" && "$2" == "graphql" ]]; then
  QUERY="$3"
  for arg in "$@"; do
    case "$arg" in
      -f) : ;;
      query=*) QUERY="${arg#query=}" ;;
    esac
  done
  NUM=$(echo "$*" | grep -oE 'discussion\(number:[0-9]+\)' | grep -oE '[0-9]+')
  HAS_AFTER=$(echo "$*" | grep -c 'after:' || true)
  case "$NUM" in
    1001) cat "$FAKE_GH_FIXTURE_DIR/1001.json" ;;
    1002) cat "$FAKE_GH_FIXTURE_DIR/1002.json" ;;
    1003) cat "$FAKE_GH_FIXTURE_DIR/1003.json" ;;
    1004) cat "$FAKE_GH_FIXTURE_DIR/1004.json" ;;
    1005)
      if [[ "$HAS_AFTER" -gt 0 ]]; then
        cat "$FAKE_GH_FIXTURE_DIR/1005_page1.json"
      else
        cat "$FAKE_GH_FIXTURE_DIR/1005_page0.json"
      fi
      ;;
    1006) cat "$FAKE_GH_FIXTURE_DIR/1006_page.json" ;;
    *) echo '{"data":{"repository":{"discussion":null}}}' ;;
  esac
  exit 0
fi
echo "fake gh: unhandled invocation: $*" >&2
exit 1
FAKE_GH_EOF
chmod +x "$FAKE_BIN_DIR/gh"

export FAKE_GH_FIXTURE_DIR
FAKE_GH_FIXTURE_DIR=$(mktemp -d)
trap 'rm -rf "$FAKE_BIN_DIR" "$FAKE_GH_FIXTURE_DIR"' EXIT

export PATH="$FAKE_BIN_DIR:$PATH"

# ── Fixture A: body-only (D#1997's shape) ───────────────────────────────────
python3 -c "
import json
body = '## Spec (Acceptance)\n\n1. do the thing\n   \`echo one\`\n'
print(json.dumps({'data': {'repository': {'discussion': {
    'body': body,
    'comments': {'nodes': []},
}}}}))
" > "$FAKE_GH_FIXTURE_DIR/1001.json"

OUT_A=$(resolve_spec_text 1001)
if echo "$OUT_A" | grep -q '^## Spec (Acceptance)$'; then
  _pass "A: body-only Spec heading present in resolved output"
else
  _fail "A: body-only Spec heading missing from resolved output"
fi
if echo "$OUT_A" | grep -q 'SPEC_TEXT_FROM_COMMENT'; then
  _fail "A: no comment was linked, but a comment marker was printed anyway"
else
  _pass "A: no comment marker printed when nothing is linked"
fi

# ── Fixture B: Spec pinned in a linked comment (D#1944's shape) ────────────
python3 -c "
import json
body = 'Spec is FROZEN — read it here: [Spec comment](https://github.com/x/y/discussions/1002#discussioncomment-555).\n\nProblem statement prose, no Spec heading here.\n'
comment_body = '## Spec (Acceptance)\n\n1. do the other thing\n   \`echo two\`\n'
print(json.dumps({'data': {'repository': {'discussion': {
    'body': body,
    'comments': {'nodes': [{'databaseId': 555, 'body': comment_body}]},
}}}}))
" > "$FAKE_GH_FIXTURE_DIR/1002.json"

OUT_B=$(resolve_spec_text 1002)
BODY_HEADING_COUNT=$(echo "$OUT_B" | grep -c '^## Spec (Acceptance)$')
if [[ "$BODY_HEADING_COUNT" -eq 1 ]]; then
  _pass "B: linked-comment Spec heading appears exactly once in resolved output"
else
  _fail "B: expected exactly 1 occurrence of the Spec heading, got $BODY_HEADING_COUNT"
fi
if echo "$OUT_B" | grep -q '<!-- SPEC_TEXT_FROM_COMMENT:555 -->'; then
  _pass "B: comment-provenance marker present"
else
  _fail "B: comment-provenance marker missing"
fi

# ── Fixture C: link present, but no matching comment exists ────────────────
python3 -c "
import json
body = 'Spec is FROZEN — read it here: [Spec comment](https://github.com/x/y/discussions/1003#discussioncomment-999).\n\nNo such comment actually comes back.\n'
print(json.dumps({'data': {'repository': {'discussion': {
    'body': body,
    'comments': {'nodes': []},
}}}}))
" > "$FAKE_GH_FIXTURE_DIR/1003.json"

if OUT_C=$(resolve_spec_text 1003); then
  if echo "$OUT_C" | grep -q 'SPEC_TEXT_FROM_COMMENT'; then
    _fail "C: no matching comment existed, but a marker was printed anyway"
  else
    _pass "C: missing comment handled without crashing or fabricating a marker"
  fi
else
  _fail "C: resolver exited non-zero on a dangling comment link"
fi

# ── Fixture D: link appears past the first-10-lines window ─────────────────
python3 -c "
import json
prose_lines = '\n'.join(f'Line {i} of unrelated prose.' for i in range(1, 13))
body = prose_lines + '\nSee also: https://github.com/x/y/discussions/1004#discussioncomment-777\n'
comment_body = '## Spec (Acceptance)\n\n1. should NOT be pulled in\n   \`echo three\`\n'
print(json.dumps({'data': {'repository': {'discussion': {
    'body': body,
    'comments': {'nodes': [{'databaseId': 777, 'body': comment_body}]},
}}}}))
" > "$FAKE_GH_FIXTURE_DIR/1004.json"

OUT_D=$(resolve_spec_text 1004)
if echo "$OUT_D" | grep -q 'SPEC_TEXT_FROM_COMMENT'; then
  _fail "D: a link past line 10 was treated as the authoritative freeze pointer"
else
  _pass "D: a link past line 10 is left as prose, not resolved"
fi

# ── Fixture E: frozen-Spec comment sits past comment #100 — pagination ─────
# (D#2008 code review, second round). Page 0 returns 100 filler comments and
# hasNextPage=true; the real target comment only exists on page 1 (after the
# cursor). If the resolver only read the first page, it would silently miss
# this comment — the same failure shape AC4 exists to prevent.
python3 -c "
import json
body = 'Spec is FROZEN — read it here: [Spec comment](https://github.com/x/y/discussions/1005#discussioncomment-888).\n\nProblem statement prose, no Spec heading here.\n'
filler = [{'databaseId': i, 'body': f'filler comment {i}'} for i in range(1, 101)]
print(json.dumps({'data': {'repository': {'discussion': {
    'body': body,
    'comments': {'pageInfo': {'hasNextPage': True, 'endCursor': 'CURSOR1'}, 'nodes': filler},
}}}}))
" > "$FAKE_GH_FIXTURE_DIR/1005_page0.json"

python3 -c "
import json
comment_body = '## Spec (Acceptance)\n\n1. paginated item\n   \`echo paginated\`\n'
print(json.dumps({'data': {'repository': {'discussion': {
    'body': 'unused on page > 0',
    'comments': {'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': [{'databaseId': 888, 'body': comment_body}]},
}}}}))
" > "$FAKE_GH_FIXTURE_DIR/1005_page1.json"

OUT_E=$(resolve_spec_text 1005)
if echo "$OUT_E" | grep -q '<!-- SPEC_TEXT_FROM_COMMENT:888 -->'; then
  _pass "E: comment past #100 is found via pagination"
else
  _fail "E: comment past #100 was NOT found — pagination did not reach page 2"
fi
if echo "$OUT_E" | grep -q 'echo paginated'; then
  _pass "E: paginated comment's real content is present in resolved output"
else
  _fail "E: paginated comment's content is missing from resolved output"
fi

# ── Fixture F: pagination cap hit — must warn loudly, not truncate silently ─
# Every page claims hasNextPage=true forever, simulating a pathological
# Discussion beyond _RESOLVE_SPEC_TEXT_MAX_PAGES pages. The resolver must
# stop (not loop forever) and print a warning to stderr rather than quietly
# returning a truncated result with no indication anything was cut.
python3 -c "
import json
body = 'Spec is FROZEN — read it here: [Spec comment](https://github.com/x/y/discussions/1006#discussioncomment-999).\n\nProblem statement.\n'
print(json.dumps({'data': {'repository': {'discussion': {
    'body': body,
    'comments': {'pageInfo': {'hasNextPage': True, 'endCursor': 'ALWAYS_MORE'}, 'nodes': []},
}}}}))
" > "$FAKE_GH_FIXTURE_DIR/1006_page.json"

STDERR_F=$(mktemp)
OUT_F=$(resolve_spec_text 1006 2>"$STDERR_F")
RC_F=$?
if [[ $RC_F -eq 0 || $RC_F -eq 1 ]] && grep -qi 'more than.*comments' "$STDERR_F"; then
  _pass "F: pagination cap hit prints a loud stderr warning instead of looping forever"
else
  _fail "F: pagination cap was not enforced loudly (rc=$RC_F, stderr: $(cat "$STDERR_F"))"
fi
rm -f "$STDERR_F"

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "test_resolve_spec_text.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
