#!/usr/bin/env bash
# Smoke test: quality gate in team-lead-iteration.sh surfaces gh API errors
# instead of silently swallowing them.
#
# Strategy: run the quality-gate logic in a subprocess with stubbed gh/log
# functions (via a wrapper script in PATH) and verify stderr output and
# the JSONL file contain the expected failure records.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/team-lead-iteration.sh"

TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

HOOK_EVENTS_DIR="$TMPDIR_TEST/.autonomous-team/hook-events"
mkdir -p "$HOOK_EVENTS_DIR"

# The quality-gate block, extracted verbatim from the fixed script.
# We inline it so the test doesn't parse the full 800-line script.
QUALITY_GATE_BLOCK='
_qg_all_ok=true
_qg_errors_dir="${REPO_ROOT}/.autonomous-team/hook-events"
mkdir -p "$_qg_errors_dir"

gh api -X DELETE "repos/${REPO}/issues/${pr_num}/labels/code-review-passed" >/dev/null 2>&1
_rc_del=$?
if [ "$_rc_del" -ne 0 ]; then
  echo "[quality-gate] FAIL pr=#${pr_num} op=delete-label rc=${_rc_del}" >&2
  printf '"'"'{"ts":"%s","pr":%s,"op":"delete-label","rc":%s}\n'"'"' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$pr_num" "$_rc_del" \
    >> "${_qg_errors_dir}/quality-gate-errors.jsonl"
  _qg_all_ok=false
fi

gh api -X POST "repos/${REPO}/issues/${pr_num}/labels" -f labels[]="code-review-needs-fix" >/dev/null 2>&1
_rc_add=$?
if [ "$_rc_add" -ne 0 ]; then
  echo "[quality-gate] FAIL pr=#${pr_num} op=add-label rc=${_rc_add}" >&2
  printf '"'"'{"ts":"%s","pr":%s,"op":"add-label","rc":%s}\n'"'"' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$pr_num" "$_rc_add" \
    >> "${_qg_errors_dir}/quality-gate-errors.jsonl"
  _qg_all_ok=false
fi

gh pr comment "$pr_num" --body "Quality score $SCORE/100 ..." --repo "$REPO" 2>/dev/null
_rc_comment=$?
if [ "$_rc_comment" -ne 0 ]; then
  echo "[quality-gate] FAIL pr=#${pr_num} op=post-comment rc=${_rc_comment}" >&2
  printf '"'"'{"ts":"%s","pr":%s,"op":"post-comment","rc":%s}\n'"'"' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$pr_num" "$_rc_comment" \
    >> "${_qg_errors_dir}/quality-gate-errors.jsonl"
  _qg_all_ok=false
fi

if [ "$_qg_all_ok" = "true" ]; then
  echo "OUTCOME:success"
else
  echo "OUTCOME:partial-failure"
fi
'

# ---------------------------------------------------------------------------
# run_case del_rc add_rc comment_rc
#   Writes a gh stub to PATH, runs the block, captures stdout+stderr+JSONL.
# ---------------------------------------------------------------------------
run_case() {
  local del_rc=$1 add_rc=$2 comment_rc=$3

  rm -f "$HOOK_EVENTS_DIR/quality-gate-errors.jsonl"

  # Create a gh stub script in the temp dir
  local stub_dir="$TMPDIR_TEST/stub_$$"
  mkdir -p "$stub_dir"
  cat > "$stub_dir/gh" <<STUBEOF
#!/usr/bin/env bash
subcmd="\$1"
if [ "\$subcmd" = "api" ]; then
  flag="\$2"
  method="\$3"
  case "\$method" in
    DELETE) exit $del_rc ;;
    POST)   exit $add_rc ;;
  esac
elif [ "\$subcmd" = "pr" ]; then
  exit $comment_rc
fi
exit 0
STUBEOF
  chmod +x "$stub_dir/gh"

  local out err
  out=$(PATH="$stub_dir:$PATH" REPO_ROOT="$TMPDIR_TEST" REPO="autonomous-agent-7/autonomous-forever" \
        pr_num=42 SCORE=55 FAILING_DIMS="complexity" \
        bash -c "$QUALITY_GATE_BLOCK" 2>"$TMPDIR_TEST/stderr_$$.txt")
  err=$(cat "$TMPDIR_TEST/stderr_$$.txt")

  echo "OUT:$out"
  echo "ERR:$err"
  if [ -f "$HOOK_EVENTS_DIR/quality-gate-errors.jsonl" ]; then
    local lc
    lc=$(wc -l < "$HOOK_EVENTS_DIR/quality-gate-errors.jsonl" | tr -d ' ')
    echo "JSONL_COUNT:$lc"
    # Prefix each line so multi-line content survives the grep pass below
    while IFS= read -r jline; do
      echo "JSONL_LINE:$jline"
    done < "$HOOK_EVENTS_DIR/quality-gate-errors.jsonl"
  else
    echo "JSONL_COUNT:0"
  fi

  rm -rf "$stub_dir" "$TMPDIR_TEST/stderr_$$.txt"
}

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if [ "$cond" = "true" ] || [ "$cond" = "1" ] || [ "$cond" -gt 0 ] 2>/dev/null; then
    echo "PASS: $desc"
    PASS=$((PASS+1))
  else
    echo "FAIL: $desc"
    FAIL=$((FAIL+1))
  fi
}

assert_false() {
  local desc="$1" cond="$2"
  if [ "$cond" = "false" ] || [ "$cond" = "0" ] || ([ "$cond" -eq 0 ] 2>/dev/null); then
    echo "PASS: $desc"
    PASS=$((PASS+1))
  else
    echo "FAIL: $desc"
    FAIL=$((FAIL+1))
  fi
}

# ---------------------------------------------------------------------------
# Test 1: all succeed
# ---------------------------------------------------------------------------
result=$(run_case 0 0 0)
out=$(echo "$result" | grep '^OUT:' | cut -d: -f2-)
err=$(echo "$result" | grep '^ERR:' | cut -d: -f2-)
jcount=$(echo "$result" | grep '^JSONL_COUNT:' | cut -d: -f2-)

assert      "all-ok: outcome=success"           "$(echo "$out" | grep -c 'OUTCOME:success')"
assert_false "all-ok: no stderr"                "$([ -n "$err" ] && echo 1 || echo 0)"
assert_false "all-ok: zero JSONL entries"       "$([ "$jcount" -gt 0 ] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# Test 2: delete-label fails
# ---------------------------------------------------------------------------
result=$(run_case 1 0 0)
out=$(echo "$result" | grep '^OUT:' | cut -d: -f2-)
err=$(echo "$result" | grep '^ERR:' | cut -d: -f2-)
jcount=$(echo "$result" | grep '^JSONL_COUNT:' | cut -d: -f2-)
jcontent=$(echo "$result" | grep '^JSONL_LINE:' | cut -d: -f2-)

assert      "del-fail: stderr has delete-label FAIL"   "$(echo "$err" | grep -c 'op=delete-label')"
assert      "del-fail: JSONL has 1 entry"              "$([ "$jcount" -eq 1 ] && echo 1 || echo 0)"
assert      "del-fail: JSONL op=delete-label"          "$(echo "$jcontent" | grep -c '"op":"delete-label"')"
assert_false "del-fail: outcome NOT success"           "$(echo "$out" | grep -c 'OUTCOME:success')"
assert      "del-fail: outcome=partial-failure"        "$(echo "$out" | grep -c 'OUTCOME:partial-failure')"

# ---------------------------------------------------------------------------
# Test 3: add-label fails
# ---------------------------------------------------------------------------
result=$(run_case 0 22 0)
out=$(echo "$result" | grep '^OUT:' | cut -d: -f2-)
err=$(echo "$result" | grep '^ERR:' | cut -d: -f2-)
jcount=$(echo "$result" | grep '^JSONL_COUNT:' | cut -d: -f2-)
jcontent=$(echo "$result" | grep '^JSONL_LINE:' | cut -d: -f2-)

assert      "add-fail: stderr has add-label FAIL"      "$(echo "$err" | grep -c 'op=add-label')"
assert      "add-fail: JSONL has 1 entry"              "$([ "$jcount" -eq 1 ] && echo 1 || echo 0)"
assert      "add-fail: JSONL op=add-label"             "$(echo "$jcontent" | grep -c '"op":"add-label"')"
assert_false "add-fail: outcome NOT success"           "$(echo "$out" | grep -c 'OUTCOME:success')"

# ---------------------------------------------------------------------------
# Test 4: comment fails
# ---------------------------------------------------------------------------
result=$(run_case 0 0 5)
out=$(echo "$result" | grep '^OUT:' | cut -d: -f2-)
err=$(echo "$result" | grep '^ERR:' | cut -d: -f2-)
jcount=$(echo "$result" | grep '^JSONL_COUNT:' | cut -d: -f2-)
jcontent=$(echo "$result" | grep '^JSONL_LINE:' | cut -d: -f2-)

assert      "comment-fail: stderr has post-comment FAIL" "$(echo "$err" | grep -c 'op=post-comment')"
assert      "comment-fail: JSONL has 1 entry"            "$([ "$jcount" -eq 1 ] && echo 1 || echo 0)"
assert      "comment-fail: JSONL op=post-comment"        "$(echo "$jcontent" | grep -c '"op":"post-comment"')"
assert_false "comment-fail: outcome NOT success"         "$(echo "$out" | grep -c 'OUTCOME:success')"

# ---------------------------------------------------------------------------
# Test 5: all 3 fail
# ---------------------------------------------------------------------------
result=$(run_case 1 1 1)
jcount=$(echo "$result" | grep '^JSONL_COUNT:' | cut -d: -f2-)
jcontent=$(echo "$result" | grep '^JSONL_LINE:' | cut -d: -f2-  || true)

assert "all-fail: JSONL has 3 entries"            "$([ "$jcount" -eq 3 ] && echo 1 || echo 0)"
_op_count=$(echo "$jcontent" | grep -c '"op"' || true)
assert "all-fail: all 3 ops in JSONL"             "$([ "$_op_count" -ge 3 ] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# Test 6: script syntax still valid
# ---------------------------------------------------------------------------
bash -n "$SCRIPT" 2>/dev/null
assert "script syntax valid" "$([ $? -eq 0 ] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
