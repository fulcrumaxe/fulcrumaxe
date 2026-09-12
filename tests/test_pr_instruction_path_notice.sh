#!/usr/bin/env bash
# tests/test_pr_instruction_path_notice.sh — D#2434 AC-6, AC-7, AC-10, AC-12,
# plus the two security findings from review round 2: comment injection
# (finding #1) and marker-spoofing to disable the detector (finding #2).
#
# Drives the real scripts/pr-instruction-path-notice.sh (which in turn runs
# the real scripts/lib/instruction_paths.py) against a stubbed `gh` on PATH.
# Only the network boundary is faked; the label/comment/idempotency logic
# under test is the real thing, not a description of it (D#2377).
#
# Run: bash tests/test_pr_instruction_path_notice.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

echo "=== test_pr_instruction_path_notice.sh ==="

FIXTURE_ROOT=$(mktemp -d)
trap 'rm -rf "$FIXTURE_ROOT"' EXIT

mkdir -p "$FIXTURE_ROOT/bin" "$FIXTURE_ROOT/state" "$FIXTURE_ROOT/prs"
export AUTONOMOUS_TEAM_STATE_DIR="$FIXTURE_ROOT/state"
export GH_CALL_LOG="$FIXTURE_ROOT/gh-calls.log"
: > "$GH_CALL_LOG"

# team-log comments (the AC-7 tripwire's second surface) go to a fixed Issue
# number so the fake `gh` needs no real jq-filtered `issue list` behaviour —
# rotate-team-log.sh's own LOG_OVERRIDE test hook exists for exactly this.
export LOG_OVERRIDE="999"

# The identity `gh api user` reports — this is what the notice script's
# idempotency check filters comments by (D#2434 review round 2, finding #2).
export FIXTURE_BOT_LOGIN="fixture-bot"

export FIXTURE_DIR="$FIXTURE_ROOT/prs"

_write_pr_files() {
  local pr="$1"; shift
  python3 -c "
import json, sys
paths = sys.argv[1:]
print(json.dumps([{'filename': p} for p in paths]))
" "$@" > "$FIXTURE_DIR/pr-$pr-files.json"
}

_write_pr_meta() {
  local pr="$1" head_repo="$2" base_repo="$3"
  python3 -c "
import json, sys
print(json.dumps({
    'user': {'login': 'fixture-author', 'id': 1},
    'labels': [],
    'head': {'repo': {'full_name': sys.argv[1]}},
    'base': {'repo': {'full_name': sys.argv[2]}},
}))
" "$head_repo" "$base_repo" > "$FIXTURE_DIR/pr-$pr-meta.json"
}

_reset_pr_comments() {
  local pr="$1"
  echo '{"comments": []}' > "$FIXTURE_DIR/pr-$pr-comments.json"
}

# _add_pr_comment <pr> <author> <body> — seed a comment as if some account
# had already posted it, for the marker-spoofing attack test below.
_add_pr_comment() {
  local pr="$1" author="$2" body="$3"
  python3 - "$FIXTURE_DIR/pr-$pr-comments.json" "$author" "$body" <<'PY'
import json, sys
path, author, body = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {"comments": []}
data.setdefault("comments", []).append({"body": body, "author": {"login": author}})
with open(path, "w") as f:
    json.dump(data, f)
PY
}

# ── gh stub ──────────────────────────────────────────────────────────────────
cat > "$FIXTURE_ROOT/bin/gh" <<'GHEOF'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"

if [[ "$1" == "api" && "$2" == "user" ]]; then
  # The real script calls `gh api user --jq '.login'` — real gh applies the
  # --jq filter itself and prints the bare string. This stub mimics that
  # output directly rather than echoing unfiltered JSON, which is what a
  # naive stub would do and would silently corrupt BOT_LOGIN.
  echo "$FIXTURE_BOT_LOGIN"
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "view" ]]; then
  pr="$3"
  field=""
  i=4
  while [[ $i -le $# ]]; do
    arg="${!i}"
    if [[ "$arg" == "--json" ]]; then
      j=$((i + 1))
      field="${!j}"
    fi
    i=$((i + 1))
  done
  case "$field" in
    comments) cat "$FIXTURE_DIR/pr-$pr-comments.json" 2>/dev/null || echo '{"comments":[]}' ;;
    *) echo '{}' ;;
  esac
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  pr="$3"
  body=""
  i=4
  while [[ $i -le $# ]]; do
    arg="${!i}"
    if [[ "$arg" == "--body" ]]; then
      j=$((i + 1))
      body="${!j}"
    fi
    i=$((i + 1))
  done
  python3 - "$FIXTURE_DIR/pr-$pr-comments.json" "$body" "$FIXTURE_BOT_LOGIN" <<'PY'
import json, sys
path, body, author = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {"comments": []}
data.setdefault("comments", []).append({"body": body, "author": {"login": author}})
with open(path, "w") as f:
    json.dump(data, f)
PY
  exit 0
fi

if [[ "$1" == "label" && "$2" == "create" ]]; then
  exit 0
fi

if [[ "$1" == "issue" ]]; then
  # rotate-team-log.sh's calls, driven with LOG_OVERRIDE so it never needs
  # real jq-filtered `issue list` output from this stub.
  case "$2" in
    comment) exit 0 ;;
    close) exit 0 ;;
    list) echo '[]' ; exit 0 ;;
    *) exit 0 ;;
  esac
fi

if [[ "$1" == "api" ]]; then
  shift
  joined="$*"
  if [[ "$joined" == *"-X POST"* && "$joined" == *"/labels"* ]]; then
    exit 0
  fi
  if [[ "$joined" == *"--paginate"* && "$joined" == *"/files"* ]]; then
    pr=$(echo "$joined" | grep -oE 'pulls/[0-9]+' | head -1 | grep -oE '[0-9]+')
    cat "$FIXTURE_DIR/pr-$pr-files.json" 2>/dev/null || echo '[]'
    exit 0
  fi
  if [[ "$joined" == *"/pulls/"* ]]; then
    pr=$(echo "$joined" | grep -oE 'pulls/[0-9]+' | head -1 | grep -oE '[0-9]+')
    cat "$FIXTURE_DIR/pr-$pr-meta.json" 2>/dev/null || echo '{}'
    exit 0
  fi
  if [[ "$joined" == *"/issues/"* ]]; then
    # rotate-team-log.sh's _true_comment_count
    echo '0'
    exit 0
  fi
  echo '{}'
  exit 0
fi

echo "fake gh: unhandled args: $*" >&2
exit 1
GHEOF
chmod +x "$FIXTURE_ROOT/bin/gh"
export PATH="$FIXTURE_ROOT/bin:$PATH"

NOTICE="$REPO_ROOT/scripts/pr-instruction-path-notice.sh"

# ── AC-6 / AC-5-adjacent — quiet on a PR with no instruction-bearing path ───
_write_pr_files 500 "scripts/lib/external_intake_gate.py" "scripts/lib/trust_id_resolver.py"
_write_pr_meta 500 "example-org/example-code" "example-org/example-code"
_reset_pr_comments 500
: > "$GH_CALL_LOG"

OUT=$(bash "$NOTICE" 500 false 2>&1)
RC=$?

if [[ $RC -eq 0 ]]; then
  pass "AC-6: quiet PR exits 0"
else
  fail "AC-6: quiet PR exited $RC: $OUT"
fi
if grep -qE '(label create|-X POST.*labels|pr comment)' "$GH_CALL_LOG"; then
  fail "AC-6: a label or comment call was made on a PR with no instruction-bearing path: $(cat "$GH_CALL_LOG")"
else
  pass "AC-6: no label/comment call on a PR touching no instruction-bearing path"
fi

# ── AC-6 — the real path: label applied, one comment posted ────────────────
_write_pr_files 501 ".claude/agents/browser-tester.md" "dashboard/src/lib/dashboardReady.ts"
_write_pr_meta 501 "example-org/example-code" "example-org/example-code"
_reset_pr_comments 501
: > "$GH_CALL_LOG"

OUT=$(bash "$NOTICE" 501 false 2>&1)
RC=$?

if [[ $RC -eq 0 ]]; then
  pass "AC-6: matching PR exits 0"
else
  fail "AC-6: matching PR exited $RC: $OUT"
fi

if grep -q "label create instruction-paths-touched" "$GH_CALL_LOG"; then
  pass "AC-6: label create was called for the matching PR"
else
  fail "AC-6: label create was not called: $(cat "$GH_CALL_LOG")"
fi

if grep -qE 'api -X POST repos/.*issues/501/labels' "$GH_CALL_LOG"; then
  pass "AC-6: label was applied to the PR via REST"
else
  fail "AC-6: label-apply REST call missing: $(cat "$GH_CALL_LOG")"
fi

COMMENT_COUNT=$(python3 -c "import json; print(len(json.load(open('$FIXTURE_DIR/pr-501-comments.json'))['comments']))")
if [[ "$COMMENT_COUNT" -eq 1 ]]; then
  pass "AC-6: exactly one comment was posted"
else
  fail "AC-6: expected exactly one comment, got $COMMENT_COUNT"
fi

COMMENT_BODY=$(python3 -c "import json; print(json.load(open('$FIXTURE_DIR/pr-501-comments.json'))['comments'][0]['body'])")
if echo "$COMMENT_BODY" | grep -qF ".claude/agents/browser-tester.md"; then
  pass "AC-6: comment lists the matched path"
else
  fail "AC-6: comment does not list the matched path: $COMMENT_BODY"
fi
if echo "$COMMENT_BODY" | grep -qi "human-gated"; then
  pass "AC-6: comment states that review is human-gated"
else
  fail "AC-6: comment does not state review is human-gated"
fi
if echo "$COMMENT_BODY" | grep -qi "intake-approved"; then
  pass "AC-6: comment states what intake-approved means on this PR"
else
  fail "AC-6: comment does not explain intake-approved semantics"
fi
if echo "$COMMENT_BODY" | grep -qF '```text'; then
  pass "finding #1: the matched-paths block is fenced"
else
  fail "finding #1: the matched-paths block is not fenced: $COMMENT_BODY"
fi

# ── AC-6 idempotency — a second run applies no second label, posts no ──────
# second comment.
BEFORE_LABEL_CALLS=$(grep -c "label create" "$GH_CALL_LOG")
BEFORE_POST_CALLS=$(grep -cE 'api -X POST repos/.*issues/501/labels' "$GH_CALL_LOG")

OUT=$(bash "$NOTICE" 501 false 2>&1)
RC=$?

AFTER_LABEL_CALLS=$(grep -c "label create" "$GH_CALL_LOG")
AFTER_POST_CALLS=$(grep -cE 'api -X POST repos/.*issues/501/labels' "$GH_CALL_LOG")
AFTER_COMMENT_COUNT=$(python3 -c "import json; print(len(json.load(open('$FIXTURE_DIR/pr-501-comments.json'))['comments']))")

if [[ $RC -eq 0 ]]; then
  pass "AC-6: second run exits 0"
else
  fail "AC-6: second run exited $RC: $OUT"
fi
if [[ "$AFTER_COMMENT_COUNT" -eq 1 ]]; then
  pass "AC-6: idempotent — comment count unchanged after a second run"
else
  fail "AC-6: idempotency broken — comment count went from 1 to $AFTER_COMMENT_COUNT"
fi
if [[ "$AFTER_LABEL_CALLS" -eq "$BEFORE_LABEL_CALLS" && "$AFTER_POST_CALLS" -eq "$BEFORE_POST_CALLS" ]]; then
  pass "AC-6: idempotent — no second label create/apply call"
else
  fail "AC-6: a second run issued another label call ($BEFORE_LABEL_CALLS/$BEFORE_POST_CALLS -> $AFTER_LABEL_CALLS/$AFTER_POST_CALLS)"
fi

# ── finding #2 — marker spoofing: the PR's own author pre-posts our exact
# marker text, and the detector must NOT treat that as "already posted".
# It must still apply the label and post its own (author-attributed) comment.
_write_pr_files 505 ".claude/agents/browser-tester.md"
_write_pr_meta 505 "example-org/example-code" "example-org/example-code"
_reset_pr_comments 505
_add_pr_comment 505 "fixture-author" "totally unrelated <!-- instruction-paths-touched-notice:v1 --> spoofed marker, not from us"
: > "$GH_CALL_LOG"

OUT=$(bash "$NOTICE" 505 false 2>&1)
RC=$?

if [[ $RC -eq 0 ]]; then
  pass "finding #2: run against a marker-spoofed PR still exits 0"
else
  fail "finding #2: run against a marker-spoofed PR exited $RC: $OUT"
fi
if grep -q "label create instruction-paths-touched" "$GH_CALL_LOG"; then
  pass "finding #2: label is still applied despite the spoofed marker from the PR author"
else
  fail "finding #2: detector was silenced by a marker posted by the PR's own author — the vulnerability is not fixed"
fi
POST_SPOOF_COMMENT_COUNT=$(python3 -c "import json; print(len(json.load(open('$FIXTURE_DIR/pr-505-comments.json'))['comments']))")
if [[ "$POST_SPOOF_COMMENT_COUNT" -eq 2 ]]; then
  pass "finding #2: our own comment was posted alongside the spoofed one (2 total)"
else
  fail "finding #2: expected 2 comments (spoofed + ours), got $POST_SPOOF_COMMENT_COUNT"
fi

# Now confirm the OTHER direction still works: a genuine second run (by us,
# same PR, same state) after our own comment landed IS idempotent.
BEFORE_SPOOF_LABEL_CALLS=$(grep -c "label create" "$GH_CALL_LOG")
bash "$NOTICE" 505 false >/dev/null 2>&1
AFTER_SPOOF_LABEL_CALLS=$(grep -c "label create" "$GH_CALL_LOG")
AFTER_SPOOF_COMMENT_COUNT=$(python3 -c "import json; print(len(json.load(open('$FIXTURE_DIR/pr-505-comments.json'))['comments']))")
if [[ "$AFTER_SPOOF_COMMENT_COUNT" -eq 2 && "$AFTER_SPOOF_LABEL_CALLS" -eq "$BEFORE_SPOOF_LABEL_CALLS" ]]; then
  pass "finding #2: idempotency against OUR OWN marker still holds after the spoofing attempt"
else
  fail "finding #2: idempotency against our own marker broke (comments=$AFTER_SPOOF_COMMENT_COUNT, label_calls before/after=$BEFORE_SPOOF_LABEL_CALLS/$AFTER_SPOOF_LABEL_CALLS)"
fi

# ── AC-7 — the expiry tripwire fires only when cross_repository is true ────
_write_pr_files 502 ".mcp.json"
_write_pr_meta 502 "stranger/fork" "example-org/example-code"
_reset_pr_comments 502

OUT=$(bash "$NOTICE" 502 true 2>&1)
if echo "$OUT" | grep -q "EXPIRY-TRIPWIRE"; then
  pass "AC-7: tripwire marker present when cross_repository is true"
else
  fail "AC-7: tripwire marker missing on a cross-repository PR: $OUT"
fi
if echo "$OUT" | grep -q "D#2434"; then
  pass "AC-7: tripwire marker names this Discussion"
else
  fail "AC-7: tripwire marker does not name the Discussion: $OUT"
fi

_write_pr_files 503 ".mcp.json"
_write_pr_meta 503 "example-org/example-code" "example-org/example-code"
_reset_pr_comments 503

OUT=$(bash "$NOTICE" 503 false 2>&1)
if echo "$OUT" | grep -q "EXPIRY-TRIPWIRE"; then
  fail "AC-7: tripwire marker fired on a same-repo PR: $OUT"
else
  pass "AC-7: tripwire marker absent on a same-repo PR"
fi

# The tripwire must never gate anything — same-repo and cross-repo PRs that
# both touch an instruction-bearing path both still get the label+comment.
COMMENT_COUNT_502=$(python3 -c "import json; print(len(json.load(open('$FIXTURE_DIR/pr-502-comments.json'))['comments']))")
if [[ "$COMMENT_COUNT_502" -eq 1 ]]; then
  pass "AC-7: cross_repository=true PR still gets the normal notice (signal, not a gate)"
else
  fail "AC-7: cross_repository=true suppressed the normal notice"
fi

# ── AC-10 — refuses to call gh at all when the code plane is unresolved ────
FIXTURE_REPO="$FIXTURE_ROOT/repo-copy"
mkdir -p "$FIXTURE_REPO/scripts/lib"
cp "$REPO_ROOT/scripts/lib/repo-resolve.sh" "$FIXTURE_REPO/scripts/lib/repo-resolve.sh"
cp "$REPO_ROOT/scripts/lib/instruction_paths.py" "$FIXTURE_REPO/scripts/lib/instruction_paths.py"
cp "$REPO_ROOT/scripts/pr-instruction-path-notice.sh" "$FIXTURE_REPO/scripts/pr-instruction-path-notice.sh"
cp "$REPO_ROOT/scripts/rotate-team-log.sh" "$FIXTURE_REPO/scripts/rotate-team-log.sh"
# Deliberately no .autonomous-team/config.json under $FIXTURE_REPO — the
# code plane must have nothing to resolve from.

: > "$GH_CALL_LOG"
OUT=$(env -u AUTONOMOUS_TEAM_REPO bash "$FIXTURE_REPO/scripts/pr-instruction-path-notice.sh" 504 2>&1)
RC=$?

if [[ $RC -ne 0 ]]; then
  pass "AC-10: unresolved code plane exits non-zero (rc=$RC)"
else
  fail "AC-10: unresolved code plane exited 0"
fi
if [[ -s "$GH_CALL_LOG" ]]; then
  fail "AC-10: gh was called despite an unresolved code plane: $(cat "$GH_CALL_LOG")"
else
  pass "AC-10: no gh call was made before the code plane resolved"
fi

# ── hardening: the notice script keeps check-pr's stderr separate from its
# stdout, so noise on stderr from a well-behaved (exit-0) check-pr call
# cannot corrupt the JSON this script goes on to parse (D#2434 review round
# 2 hardening note). Exercised directly at the classify_pr layer, which is
# what check-pr's CLI calls: a `gh` stub that prints unrelated noise to
# stderr on every call but still returns valid data and exit 0.
python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
import instruction_paths as ip

def noisy_gh(args):
    print('noise: unrelated deprecation warning', file=sys.stderr)
    if args[0] == 'api' and '--paginate' in args:
        import json
        return json.dumps([{'filename': '.claude/agents/x.md'}])
    if args[0] == 'api':
        import json
        return json.dumps({'head': {'repo': {'full_name': 'x/x'}}, 'base': {'repo': {'full_name': 'x/x'}}})
    raise AssertionError(args)

result = ip.classify_pr(9999, 'x/x', gh=noisy_gh)
assert result['paths_touched'] == ['.claude/agents/x.md'], result
print('OK: classify_pr result unaffected by stderr noise from gh')
" 2>"$FIXTURE_ROOT/noise-check-stderr.log"
NOISE_TEST_RC=$?
if [[ $NOISE_TEST_RC -eq 0 ]]; then
  pass "hardening: classify_pr's own result is unaffected by stderr noise from gh (module layer)"
else
  fail "hardening: classify_pr broke under stderr noise from gh"
fi

# ── AC-12 — the label changes no classification (already exercised live in
# tests/test_pr_pickup_gate_loop.sh, which passes PR #101-104 through the
# real classify_open_prs with this notice wired in and asserts the same
# NEEDS_* arrays as before the wiring). Referenced here rather than
# duplicated, per D#2377 (assert on the loop, not a second description of
# it).
if bash "$REPO_ROOT/tests/test_pr_pickup_gate_loop.sh" >/dev/null 2>&1; then
  pass "AC-12: classify_open_prs, with the notice wired in, still passes its own suite unchanged"
else
  fail "AC-12: classify_open_prs regressed after wiring in the notice call"
fi

echo
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
  echo "FAILURES DETECTED"
  exit 1
fi
echo "All tests passed."
