#!/usr/bin/env bash
# tests/test_step5_seam_merge_guard.sh — D#2647: the merging phase must
# never let a mocked read feed a real `gh pr merge`.
#
# Run: bash tests/test_step5_seam_merge_guard.sh
# Expects: all assertions pass, exit 0
#
# The suite runs scripts/loop-phased-step5.sh's real merging phase and the
# real _gh_merge, with a stub `gh` first on PATH that logs every invocation
# and answers reads from fixture env vars — no network call anywhere, on
# either the base script or the head script.
#
# The script under test is overridable so the identical harness runs
# against a base checkout for the differential in section A:
#   STEP5_UNDER_TEST=/path/to/base/loop-phased-step5.sh bash tests/test_step5_seam_merge_guard.sh
# Default: this tree's scripts/loop-phased-step5.sh.
#
# Stub-gh prior art: tests/test_ci_status_check.sh:481-536 (a much smaller
# exit-127 stub). This one has to answer real reads instead of refusing all
# of them, because section A cases run with no test-mode flag at all — that
# is the point being tested.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${STEP5_UNDER_TEST:-$REAL_REPO_ROOT/scripts/loop-phased-step5.sh}"

# shellcheck source=lib/blackboard-fixture.sh
source "$REAL_REPO_ROOT/tests/lib/blackboard-fixture.sh"
blackboard_scratch_state_dir || {
  echo "FATAL: could not create scratch state dir" >&2
  exit 1
}
SCRATCH_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
BB_PR_STATE_DIR="$(blackboard_pr_state_dir "$REAL_REPO_ROOT")" || {
  echo "FATAL: could not resolve blackboard pr_state dir" >&2
  exit 1
}
STUB_ROOT="$(mktemp -d)"
trap 'rm -rf "$SCRATCH_STATE_DIR" "$STUB_ROOT"' EXIT

PASS=0
FAIL=0

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $label"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected [$expected], got [$actual])"; FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    echo "  PASS: $label"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $label — expected to contain: $needle"
    printf '%s\n' "$haystack" | tail -20 | sed 's/^/        /'
    FAIL=$((FAIL + 1))
  fi
}

# -----------------------------------------------------------------------
# Stub gh — logs every call's full argv as one line, answers reads from
# fixture env vars, exits 1 on anything it doesn't recognise.
# -----------------------------------------------------------------------
_write_stub_gh() {
  local dir="$1"
  mkdir -p "$dir"
  cat > "$dir/gh" << 'STUBEOF'
#!/usr/bin/env python3
import json, os, re, sys

LOG = os.environ.get("STUB_GH_LOG")
if LOG:
    with open(LOG, "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\n")

args = sys.argv[1:]


def out(text=""):
    sys.stdout.write(text)


def find_flag_value(flag):
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


if not args:
    sys.exit(1)

cmd = args[0]

if cmd == "pr" and len(args) > 1 and args[1] == "view":
    json_fields = find_flag_value("--json") or ""
    jq = find_flag_value("--jq") or ""
    if "labels" in json_fields.split(","):
        labels = [l for l in os.environ.get("STUB_PR_LABELS", "").split(",") if l]
        m = re.search(r'contains\(\["([^"]+)"\]\)', jq)
        if m:
            out("true\n" if m.group(1) in labels else "false\n")
            sys.exit(0)
        if jq.strip() == "[.labels[].name]":
            out(json.dumps(labels) + "\n")
            sys.exit(0)
        # Unrecognised --jq shape on a labels read.
        sys.exit(1)
    if "headRefOid" in json_fields.split(","):
        out(os.environ.get("STUB_HEAD_SHA", "") + "\n")
        sys.exit(0)
    if "files" in json_fields.split(","):
        out(json.dumps({"files": []}) + "\n")
        sys.exit(0)
    sys.exit(1)

if cmd == "pr" and len(args) > 1 and args[1] == "diff":
    if "--name-only" in args:
        out(os.environ.get("STUB_CHANGED_FILES", ""))
        sys.exit(0)
    out(os.environ.get("STUB_DIFF_CONTENT", ""))
    sys.exit(0)

if cmd == "pr" and len(args) > 1 and args[1] == "merge":
    rc = int(os.environ.get("STUB_MERGE_EXIT_CODE", "0"))
    msg = os.environ.get("STUB_MERGE_OUTPUT", "")
    if msg:
        sys.stderr.write(msg)
    sys.exit(rc)

if cmd == "pr" and len(args) > 1 and args[1] == "comment":
    sys.exit(0)

if cmd == "api":
    joined = " ".join(args)
    if "-i" in args and "actions/variables/CI_DISABLED" in joined:
        if os.environ.get("STUB_CI_DISABLED", "") == "true":
            out("HTTP/2.0 200 OK\r\n\r\n" + json.dumps({"value": "true"}))
        else:
            out("HTTP/2.0 404 Not Found\r\n\r\n")
        sys.exit(0)
    if "/check-runs" in joined:
        out(os.environ.get("STUB_CHECK_RUNS_JSON", "[]"))
        sys.exit(0)
    if "/timeline" in joined:
        out(os.environ.get("STUB_TIMELINE", ""))
        sys.exit(0)
    if re.search(r"issues/\d+/labels", joined):
        sys.exit(0)
    if "graphql" in args:
        query = find_flag_value("-f") or ""
        # -f can repeat; scan all -f/--field/-F values for one starting "query="
        qval = ""
        for i, a in enumerate(args):
            if a in ("-f", "--field") and i + 1 < len(args) and args[i + 1].startswith("query="):
                qval = args[i + 1][len("query="):]
                break
        has_num = any(a in ("-F",) and i + 1 < len(args) and args[i + 1].startswith("num=") for i, a in enumerate(args))
        if has_num or "discussion(number:$num)" in qval:
            out(os.environ.get("STUB_INTAKE_DISC_JSON",
                json.dumps({"data": {"repository": {"discussion": {
                    "id": "D_stub", "title": "stub", "body": "stub",
                    "author": {"login": "stub-author"},
                    "lastEditedAt": None, "editor": None,
                    "userContentEdits": {"totalCount": 0},
                    "labels": {"nodes": []},
                }}}})))
            sys.exit(0)
        if "discussions(first:50" in qval:
            out(json.dumps({"data": {"repository": {"discussions": {"nodes": []}}}}))
            sys.exit(0)
        sys.exit(1)

sys.exit(1)
STUBEOF
  chmod +x "$dir/gh"
}

# -----------------------------------------------------------------------
# Fixture helpers (same shapes tests/test_merge_gate.sh already uses)
# -----------------------------------------------------------------------
_make_config_file() {
  local tmpfile
  tmpfile=$(mktemp --suffix='.json')
  cat > "$tmpfile" << 'JSON'
{
  "gates": {
    "phased_orchestration": true,
    "phased_code_review": true,
    "debater_pass": false
  },
  "policies": {},
  "settings": {},
  "audit_log": []
}
JSON
  echo "$tmpfile"
}

_write_snapshot_spec_ready() {
  local path="$1" disc_num="$2"
  python3 -c "
import json
from datetime import datetime, timezone
snap = {
    'discussions': [
        {'number': $disc_num, 'title': 'Seam guard test $disc_num',
         'body': '<!-- STATUS:SPEC_READY --> spec content'}
    ],
    'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
}
json.dump(snap, open('$path', 'w'))
"
}

_write_pr_state_merging() {
  local pr_num="$1" disc_num="$2"
  mkdir -p "$BB_PR_STATE_DIR"
  python3 -c "
import json
entry = {
    'value': {
        'pr': $pr_num, 'discussion': $disc_num, 'phase': 'merging',
        'spawned_phases': [], 'completed_phases': [],
        'needs_security_review': False, 'fix_cycle_count': 0,
        'respawn_count': 0, 'last_envelope': {}, 'blocked_reason': None,
        'created_at': '2026-01-01T00:00:00+00:00',
        'updated_at': '2026-01-01T00:00:00+00:00'
    },
    'version': 1, 'updated_at': '2026-01-01T00:00:00+00:00', 'updated_by': 'test'
}
json.dump(entry, open('$BB_PR_STATE_DIR/$pr_num.json', 'w'), indent=2)
"
}

_remove_pr_state_entry() {
  rm -f "$BB_PR_STATE_DIR/$1.json"
}

# All-green check-runs fixture (8 required checks, github-actions app, success).
ALL_GREEN_CHECK_RUNS=$(python3 -c "
import json
names = ['tui', 'dashboard', 'ts-backend', 'backend (import-smoke)',
         'publish denylist', 'preflight (always-on gates)',
         'PR link policy', 'PR mutation evidence']
print(json.dumps([
    {'name': n, 'status': 'completed', 'conclusion': 'success',
     'app': {'slug': 'github-actions'}} for n in names
]))
")

# _run_step5 PR DISC — writes fixtures, runs the script under test with
# whatever STUB_*/env vars the caller already exported, cleans up after.
# Sets OUTPUT and RC.
_run_step5() {
  local pr="$1" disc="$2"
  shift 2
  local cfg snap
  cfg=$(_make_config_file)
  snap=$(mktemp --suffix='.json')
  _write_snapshot_spec_ready "$snap" "$disc"
  _write_pr_state_merging "$pr" "$disc"

  STUB_GH_LOG="$STUB_ROOT/gh-calls-$pr.log"
  : > "$STUB_GH_LOG"

  # A single `env` call takes every NAME=value pair (including the
  # caller's own "$@" fixture overrides and the dynamic CI_PR_FILES_<pr>
  # key) as plain strings, so nothing here depends on bash's own
  # prefix-assignment parsing recognising a quoted or dynamically-built
  # assignment word.
  OUTPUT=$(
    env \
      AF_CONTROL_PLANE_CONFIG="$cfg" SNAPSHOT_PATH="$snap" \
      REPO_ROOT="$REAL_REPO_ROOT" \
      AUTONOMOUS_TEAM_REPO="test-owner/test-repo" \
      HOOKS_DISABLED=1 PR_DEPENDENTS_DISABLE=1 \
      "CI_PR_FILES_${pr}=README.md" \
      STUB_GH_LOG="$STUB_GH_LOG" \
      PATH="$STUB_DIR:$PATH" \
      "$@" \
      bash "$SCRIPT" 2>&1
  )
  RC=$?
  # grep -c exits 1 (not 0) when the count is zero, so `|| echo 0` after it
  # would append a SECOND line on top of the "0" grep already printed.
  # Capture stdout unconditionally instead and default only if truly empty
  # (e.g. the log file is missing).
  MERGE_CALLS=$(grep -c '^pr merge' "$STUB_GH_LOG" 2>/dev/null)
  MERGE_CALLS="${MERGE_CALLS:-0}"

  _remove_pr_state_entry "$pr"
  rm -f "$cfg" "$snap"
}

STUB_DIR="$STUB_ROOT/bin"
_write_stub_gh "$STUB_DIR"

# =========================================================================
# Section A — base-vs-head differential (no test flag set).
# Stub reports: CI green with head SHA "S", no pass labels, acceptance-failed
# present — cases 1-5 share this baseline; case 6 overrides it.
# =========================================================================
echo ""
echo "=== A1: SPAWN_AGENT=echo alone -> 0 merge calls, real labels read ==="
_run_step5 90101 90201 \
  SPAWN_AGENT=echo \
  STUB_PR_LABELS="acceptance-failed" \
  STUB_HEAD_SHA="deadbeefS" \
  STUB_CHECK_RUNS_JSON="$ALL_GREEN_CHECK_RUNS"
assert_eq "A1: exit 0" "0" "$RC"
assert_eq "A1: zero merge calls" "0" "$MERGE_CALLS"
assert_contains "A1: a real labels read happened (read-through)" "pr view 90101" "$(cat "$STUB_ROOT/gh-calls-90101.log")"

echo ""
echo "=== A2: SPAWN_AGENT=/x alone -> 0 merge calls ==="
_run_step5 90102 90202 \
  SPAWN_AGENT=/x \
  STUB_PR_LABELS="acceptance-failed" \
  STUB_HEAD_SHA="deadbeefS" \
  STUB_CHECK_RUNS_JSON="$ALL_GREEN_CHECK_RUNS"
assert_eq "A2: exit 0" "0" "$RC"
assert_eq "A2: zero merge calls" "0" "$MERGE_CALLS"

echo ""
echo "=== A3: SPAWN_AGENT=echo + forged HAS_LABEL_* -> 0 merge calls (mock inert) ==="
_run_step5 90103 90203 \
  SPAWN_AGENT=echo \
  HAS_LABEL_90103_code_review_passed=yes \
  STUB_PR_LABELS="acceptance-failed" \
  STUB_HEAD_SHA="deadbeefS" \
  STUB_CHECK_RUNS_JSON="$ALL_GREEN_CHECK_RUNS"
assert_eq "A3: exit 0" "0" "$RC"
assert_eq "A3: zero merge calls" "0" "$MERGE_CALLS"

echo ""
echo "=== A4: SPAWN_AGENT=/x + forged HAS_LABEL_* -> 0 merge calls ==="
_run_step5 90104 90204 \
  SPAWN_AGENT=/x \
  HAS_LABEL_90104_code_review_passed=yes \
  STUB_PR_LABELS="acceptance-failed" \
  STUB_HEAD_SHA="deadbeefS" \
  STUB_CHECK_RUNS_JSON="$ALL_GREEN_CHECK_RUNS"
assert_eq "A4: exit 0" "0" "$RC"
assert_eq "A4: zero merge calls" "0" "$MERGE_CALLS"

echo ""
echo "=== A5: stale-label invalidation read LIVE (no acceptance-failed, but a force-push after the label) ==="
STALE_TIMELINE=$(printf '2026-01-01T00:00:00Z\tlabeled\tcode-review-passed\n2026-01-02T00:00:00Z\thead_ref_force_pushed\t\n')
_run_step5 90105 90205 \
  SPAWN_AGENT=echo \
  HAS_LABEL_90105_code_review_passed=yes \
  STUB_PR_LABELS="code-review-passed" \
  STUB_TIMELINE="$STALE_TIMELINE" \
  STUB_HEAD_SHA="deadbeefS" \
  STUB_CHECK_RUNS_JSON="$ALL_GREEN_CHECK_RUNS"
assert_eq "A5: exit 0" "0" "$RC"
assert_eq "A5: zero merge calls (label invalidated live)" "0" "$MERGE_CALLS"

echo ""
echo "=== A6: pin positive control -> exactly 1 merge call, pinned to S ==="
_run_step5 90106 90206 \
  SPAWN_AGENT=echo \
  STUB_PR_LABELS="code-review-passed" \
  STUB_HEAD_SHA="cafef00dS" \
  STUB_CHECK_RUNS_JSON="$ALL_GREEN_CHECK_RUNS"
assert_eq "A6: exit 0" "0" "$RC"
assert_eq "A6: exactly one merge call" "1" "$MERGE_CALLS"
assert_contains "A6: pinned to the CI-resolved SHA" "--match-head-commit cafef00dS" "$(cat "$STUB_ROOT/gh-calls-90106.log")"

# =========================================================================
# Section B — guard (head only; base has no guard to test).
# =========================================================================
echo ""
echo "=== B7: STEP5_TEST_MODE=1 + forged labels, GH_MERGE unset -> refused ==="
_run_step5 90107 90207 \
  STEP5_TEST_MODE=1 SPAWN_AGENT=echo \
  HAS_LABEL_90107_code_review_passed=yes \
  STUB_PR_LABELS="acceptance-failed"
assert_eq "B7: exit 0" "0" "$RC"
assert_eq "B7: zero merge calls" "0" "$MERGE_CALLS"
assert_contains "B7: refusal message present" "refusing real merge" "$OUTPUT"
AUDIT_FILE="$SCRATCH_STATE_DIR/audit.jsonl"
if [ -f "$AUDIT_FILE" ]; then
  REFUSAL_ROWS=$(grep -c '"kind": "step5_seam_merge_refused"' "$AUDIT_FILE" 2>/dev/null)
  REFUSAL_ROWS="${REFUSAL_ROWS:-0}"
else
  REFUSAL_ROWS=0
fi
assert_eq "B7: exactly one audit row" "1" "$REFUSAL_ROWS"
assert_contains "B7: audit row names the PR" "\"pr\": 90107" "$(cat "$AUDIT_FILE" 2>/dev/null || true)"
rm -f "$AUDIT_FILE"

echo ""
echo "=== B8: CI_STATUS_TEST_MODE=1 alone (no step5 flag) -> refused ==="
_run_step5 90108 90208 \
  CI_STATUS_TEST_MODE=1 \
  STUB_PR_LABELS="code-review-passed" \
  STUB_HEAD_SHA="deadbeefS" \
  STUB_CHECK_RUNS_JSON="$ALL_GREEN_CHECK_RUNS"
assert_eq "B8: exit 0" "0" "$RC"
assert_eq "B8: zero merge calls" "0" "$MERGE_CALLS"
assert_contains "B8: refusal message present" "refusing real merge" "$OUTPUT"
if [ -f "$AUDIT_FILE" ]; then
  REFUSAL_ROWS=$(grep -c '"kind": "step5_seam_merge_refused"' "$AUDIT_FILE" 2>/dev/null)
  REFUSAL_ROWS="${REFUSAL_ROWS:-0}"
else
  REFUSAL_ROWS=0
fi
assert_eq "B8: exactly one audit row" "1" "$REFUSAL_ROWS"
rm -f "$AUDIT_FILE"

echo ""
echo "=== B9: STEP5_TEST_MODE=1 + GH_MERGE=echo -> write-mock path still works ==="
_run_step5 90109 90209 \
  STEP5_TEST_MODE=1 SPAWN_AGENT=echo GH_MERGE=echo \
  HAS_LABEL_90109_code_review_passed=yes
assert_eq "B9: exit 0" "0" "$RC"
assert_contains "B9: GH_MERGE mock still reachable" "GH_MERGE_ARGS:" "$OUTPUT"
assert_eq "B9: zero real merge calls in the stub log" "0" "$MERGE_CALLS"

# =========================================================================
# Section C — warnings and unification.
# =========================================================================
echo ""
echo "=== C10: leaked seams outside test mode all warn once ==="
_run_step5 90110 90210 \
  SPAWN_AGENT=echo SPEC_READY_MOCK='[]' DISCUSSING_MOCK='[]' \
  STUB_PR_LABELS="acceptance-failed" \
  STUB_HEAD_SHA="deadbeefS" \
  STUB_CHECK_RUNS_JSON="$ALL_GREEN_CHECK_RUNS"
assert_eq "C10: exit 0" "0" "$RC"
assert_contains "C10: warns about SPAWN_AGENT" "ignoring SPAWN_AGENT" "$OUTPUT"
assert_contains "C10: warns about SPEC_READY_MOCK" "ignoring SPEC_READY_MOCK" "$OUTPUT"
assert_contains "C10: warns about DISCUSSING_MOCK" "ignoring DISCUSSING_MOCK" "$OUTPUT"
assert_contains "C10: each warning names STEP5_TEST_MODE" "set STEP5_TEST_MODE=1 to honour it" "$OUTPUT"
assert_contains "C10: Discussion reads went live (spec-ready query answered by stub)" "graphql" "$(cat "$STUB_ROOT/gh-calls-90110.log")"

echo ""
echo "=== C11: no gate read keys on \${SPAWN_AGENT:-} ==="
if grep -nE '\$\{SPAWN_AGENT:-\}' "$SCRIPT" > /tmp/c11-matches-$$.txt; then
  echo "  FAIL: C11: found \${SPAWN_AGENT:-} reference(s) in $SCRIPT"
  cat /tmp/c11-matches-$$.txt | sed 's/^/        /'
  FAIL=$((FAIL + 1))
else
  echo "  PASS: C11: no \${SPAWN_AGENT:-} references remain"
  PASS=$((PASS + 1))
fi
rm -f /tmp/c11-matches-$$.txt

echo ""
echo "=== C12: no -n \"\$SPAWN_AGENT style predicate remains ==="
if grep -nE '\-n "\$\{SPAWN_AGENT' "$SCRIPT" > /tmp/c12-matches-$$.txt; then
  echo "  FAIL: C12: found a -n \"\${SPAWN_AGENT... predicate in $SCRIPT"
  cat /tmp/c12-matches-$$.txt | sed 's/^/        /'
  FAIL=$((FAIL + 1))
else
  echo "  PASS: C12: no -n \"\${SPAWN_AGENT... predicate remains"
  PASS=$((PASS + 1))
fi
rm -f /tmp/c12-matches-$$.txt

# -----------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
