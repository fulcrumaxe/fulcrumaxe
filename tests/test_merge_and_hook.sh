#!/usr/bin/env bash
# tests/test_merge_and_hook.sh — hermetic tests for scripts/merge-and-hook.sh
#
# Tests:
#   Exit-code propagation from post-merge-hook.sh
#   Two-Gate marker enforcement (D#1176)
#   HG-7 external-provenance forces security review, incl. the fail-closed
#   Discussion-derivation fix (D#1588 Batch B security-needs-fix round)
#
# Run: bash tests/test_merge_and_hook.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/merge-and-hook.sh"
TWO_GATE_LIB="$REPO_ROOT/scripts/lib/two-gate-check.sh"
REPO_RESOLVE_LIB="$REPO_ROOT/scripts/lib/repo-resolve.sh"
RESOLVE_PR_DISC_LIB="$REPO_ROOT/scripts/lib/resolve-pr-discussion.sh"
CI_STATUS_LIB="$REPO_ROOT/scripts/lib/ci-status-check.sh"
PR_DEPENDENTS_LIB="$REPO_ROOT/scripts/lib/pr-dependents.sh"
DASHBOARD_TOUCHED_SCRIPT="$REPO_ROOT/scripts/check-pr-dashboard-touched.sh"
MERGE_GATE_LABELS_LIB="$REPO_ROOT/scripts/lib/merge-gate-labels.sh"

# D#2455: the label set under test is read from the shared definition at
# runtime, never restated. Every case below iterates these arrays, so adding a
# ninth label to scripts/lib/merge-gate-labels.sh extends this suite's coverage
# with no edit here — and a count assertion (which would go on passing while
# the ninth label went unenforced) is never written.
# shellcheck source=../scripts/lib/merge-gate-labels.sh
source "$MERGE_GATE_LABELS_LIB"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

assert_exit() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" -eq "$expected" ]]; then
    pass "$label"
  else
    fail "$label — expected exit $expected, got $actual"
  fi
}

assert_contains() {
  local label="$1" expected_substr="$2" actual="$3"
  # `--` so a substring that starts with a dash (e.g. --bypass-reason) is
  # matched as a pattern instead of parsed as a grep option.
  if echo "$actual" | grep -qF -- "$expected_substr"; then
    pass "$label"
  else
    fail "$label — expected to contain: $expected_substr"
  fi
}

assert_not_contains() {
  local label="$1" absent_substr="$2" actual="$3"
  if echo "$actual" | grep -qF -- "$absent_substr"; then
    fail "$label — expected NOT to contain: $absent_substr"
  else
    pass "$label"
  fi
}

# Build a temp dir with stubs for gh, python3, post-merge-hook.sh, and the lib files.
# $1 = tmpdir
# $2 = hook_exit_code (what post-merge-hook.sh should return)
#
# Default stub behavior (all overridable via env vars read by the stubs):
#   - `gh pr view <PR> --json body --jq .body` returns a body with a resolvable
#     "Closes D#4200" reference (override via STUB_PR_BODY).
#   - `gh api graphql ... discussion(number:4200) ... id` returns a valid id
#     (override via STUB_DISC_INVALID=1 to make resolution fail).
#   - `gh pr view <PR> --json labels --jq '.labels[].name'` returns
#     STUB_PR_LABELS (default: empty — no security-review-passed).
#   - `python3 .../external_intake_gate.py security-required <N>` exits
#     STUB_SEC_REQUIRED_RC (default: 1 — not required) so existing tests that
#     only care about hook-exit propagation / Two-Gate enforcement aren't
#     touched by the HG-7 path.
setup_stubs() {
  local tmpdir="$1" hook_exit="$2"
  mkdir -p "$tmpdir/bin" "$tmpdir/scripts/lib" "$tmpdir/logs" "$tmpdir/state"

  # Stub gh — args-aware so the HG-7 discussion-resolution path gets sane data.
  cat > "$tmpdir/bin/gh" <<'GHEOF'
#!/usr/bin/env bash
ARGS="$*"

# Optional call log. The wrapper redirects some `gh pr view` stderr to
# /dev/null, which swallows the GH_ARGS trace below — this file is how a test
# can still count invocations (D#1965 MF-3 needs the attempt count).
if [[ -n "${STUB_CALL_LOG:-}" ]]; then
  echo "$ARGS" >> "$STUB_CALL_LOG"
fi

# `gh pr view <PR> --repo ... --json body --jq .body`  (resolve_pr_discussion)
if [[ "$ARGS" == *"--json body"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  echo "${STUB_PR_BODY:-Closes D#4200}"
  exit 0
fi

# `gh pr view <PR> --repo ... --json labels --jq '.labels[].name'`
#
# Deliberately silent about GH_ARGS, for the same reason the `pr diff` branch
# below is: since D#2455 the wrapper fetches labels on EVERY run, before any
# other gate, and several tests here use the absence of "GH_ARGS:" to prove no
# merge was attempted. Tests that need to count label fetches use
# STUB_CALL_LOG.
#
# code-review-passed is appended by default (D#2455): it is now required on
# every merge, and every test written before that gate existed expects the
# merge to proceed. STUB_PR_LABELS_EXACT=1 suppresses the default and hands the
# wrapper exactly what STUB_PR_LABELS says — which is how the tests for the
# required-pass gate itself drive a PR that is missing it.
if [[ "$ARGS" == *"--json labels"* ]]; then
  printf '%s\n' "${STUB_PR_LABELS:-}"
  if [[ "${STUB_PR_LABELS_EXACT:-0}" != "1" ]]; then
    echo "code-review-passed"
  fi
  exit 0
fi

# `gh api graphql ... discussion(number:N) { id } ...`
if [[ "$ARGS" == *"graphql"* && "$ARGS" == *"discussion(number:"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  if [[ "${STUB_DISC_INVALID:-0}" == "1" ]]; then
    echo "null"
  else
    echo "D_kwDOFakeDiscussionId"
  fi
  exit 0
fi

# `gh pr diff --name-only <PR> --repo ...` (D#2332 browser-test gate, via
# check-pr-dashboard-touched.sh). Deliberately silent about GH_ARGS: this
# stub's stdout is piped straight into that script's `grep -q '^dashboard/'`,
# and every pre-existing test here asserts on GH_ARGS to prove whether a merge
# was attempted. Default empty => "no dashboard files touched" => the gate is
# inert for every test that predates it.
if [[ "$ARGS" == *"pr diff"* ]]; then
  printf '%s' "${STUB_PR_DIFF_FILES:-}"
  exit 0
fi

# `gh pr view <PR> --repo ... --json files --jq '.files[].path'` (D#1614 provenance gate)
if [[ "$ARGS" == *"--json files"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  printf '%s\n' "${STUB_PR_FILES:-}"
  exit 0
fi

# `gh pr view <PR> --repo ... --json mergeable,mergeStateStatus --jq ...`
# (D#2339 mergeability probe). The wrapper's --jq joins the two fields with a
# pipe, so the stub returns the already-joined string the same way every other
# branch here returns post-jq output. Default MERGEABLE|CLEAN, so the probe is
# inert for every test that predates it.
#
# STUB_MERGEABLE_SEQ is for the tests that need the answer to CHANGE between
# calls — the async-UNKNOWN retry, and a branch that goes conflicting during
# the CI wait. Semicolon-separated answers, one consumed per call, the last
# one repeating; the counter lives in STUB_MERGEABLE_SEQ_COUNTER because each
# stub invocation is a fresh process.
if [[ "$ARGS" == *"--json mergeable"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  if [[ -n "${STUB_MERGEABLE_SEQ:-}" ]]; then
    _n=0
    if [[ -n "${STUB_MERGEABLE_SEQ_COUNTER:-}" && -s "${STUB_MERGEABLE_SEQ_COUNTER}" ]]; then
      _n=$(cat "$STUB_MERGEABLE_SEQ_COUNTER")
    fi
    IFS=';' read -r -a _seq <<< "$STUB_MERGEABLE_SEQ"
    _idx="$_n"
    if [[ "$_idx" -ge "${#_seq[@]}" ]]; then _idx=$(( ${#_seq[@]} - 1 )); fi
    printf '%s\n' "${_seq[$_idx]}"
    if [[ -n "${STUB_MERGEABLE_SEQ_COUNTER:-}" ]]; then echo $(( _n + 1 )) > "$STUB_MERGEABLE_SEQ_COUNTER"; fi
    exit 0
  fi
  printf '%s\n' "${STUB_MERGEABLE:-MERGEABLE|CLEAN}"
  exit 0
fi

# `gh pr view <PR> --repo ... --json headRefOid --jq .headRefOid` (D#1614 CI gate head SHA)
if [[ "$ARGS" == *"--json headRefOid"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  echo "${STUB_HEAD_SHA:-deadbeefcafe0000}"
  exit 0
fi

# `gh api repos/.../commits/<sha>/check-runs --jq '.check_runs'` (D#1614 CI gate).
# Default stub: all four required checks green, posted by github-actions —
# existing Two-Gate/HG-7 tests don't care about CI status, so they get an
# all-green default unless a test explicitly overrides STUB_CI_CHECK_RUNS.
if [[ "$ARGS" == *"check-runs"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  if [[ -n "${STUB_CI_CHECK_RUNS:-}" ]]; then
    printf '%s' "$STUB_CI_CHECK_RUNS"
  else
    printf '%s' '[{"name":"tui","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"dashboard","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"ts-backend","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"backend (import-smoke)","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"open-source export audit","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""}]'
  fi
  exit 0
fi

# `gh pr view <PR> --repo ... --json baseRefName --jq .baseRefName` (D#1965
# conflicting-file computation needs the base ref name).
if [[ "$ARGS" == *"--json baseRefName"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  echo "${STUB_BASE_REF:-main}"
  exit 0
fi

# `gh pr merge ...` — D#1965: force a realistic merge failure so the wrapper's
# diagnostic path can be exercised hermetically. STUB_MERGE_RC is gh's exit
# code, STUB_MERGE_OUT the payload gh would print (which is what
# ci_merge_sha_pinned classifies and surfaces).
if [[ "$ARGS" == *"pr merge"* ]]; then
  if [[ "${STUB_MERGE_RC:-0}" != "0" ]]; then
    printf '%s\n' "${STUB_MERGE_OUT:-unspecified merge failure}" >&2
    exit "${STUB_MERGE_RC}"
  fi
  echo "GH_ARGS: $ARGS"
  exit 0
fi

# Everything else — generic success stub, args visible for assertions.
echo "GH_ARGS: $ARGS"
exit 0
GHEOF
  chmod +x "$tmpdir/bin/gh"

  # Stub python3 — only intercepts external_intake_gate.py security-required;
  # anything else falls through to the REAL interpreter (D#1614 introduces
  # genuine python3 -c calls for JSON/CI-status evaluation that must actually
  # run). The real path is baked in as an absolute path resolved with the
  # test's normal (unstubbed) PATH — falling back to `env python3` here would
  # re-resolve "python3" through the stub dir (which is prepended to PATH when
  # this stub runs) and recurse forever.
  local real_python3
  real_python3="$(command -v python3)"
  cat > "$tmpdir/bin/python3" <<PYEOF
#!/usr/bin/env bash
if [[ "\$1" == *external_intake_gate.py* && "\$2" == "security-required" ]]; then
  rc="\${STUB_SEC_REQUIRED_RC:-1}"
  case "\$rc" in
    0) echo "true" ;;
    1) echo "false" ;;
    *) echo "unknown" ;;
  esac
  exit "\$rc"
fi
exec "$real_python3" "\$@"
PYEOF
  chmod +x "$tmpdir/bin/python3"

  # Copy real lib files so SCRIPT_DIR resolution works from tmpdir/scripts/
  cp "$TWO_GATE_LIB"        "$tmpdir/scripts/lib/two-gate-check.sh"
  cp "$REPO_RESOLVE_LIB"    "$tmpdir/scripts/lib/repo-resolve.sh"
  cp "$RESOLVE_PR_DISC_LIB" "$tmpdir/scripts/lib/resolve-pr-discussion.sh"
  cp "$CI_STATUS_LIB"       "$tmpdir/scripts/lib/ci-status-check.sh"
  cp "$PR_DEPENDENTS_LIB"   "$tmpdir/scripts/lib/pr-dependents.sh"
  # D#2455: the merge-gate label vocabulary. Copied, never re-declared here —
  # a test that restated the labels would pass against a wrapper reading a
  # different list, which is the defect this gate exists to close.
  cp "$MERGE_GATE_LABELS_LIB" "$tmpdir/scripts/lib/merge-gate-labels.sh"
  # D#2332: the browser-test gate shells out to this, so it has to exist beside
  # the copied merge-and-hook.sh. It resolves the code repo through the copied
  # repo-resolve.sh, which finds no config.json under tmpdir and falls through
  # to AUTONOMOUS_TEAM_REPO — which run_script sets.
  cp "$DASHBOARD_TOUCHED_SCRIPT" "$tmpdir/scripts/check-pr-dashboard-touched.sh"

  # Stub post-merge-hook.sh — exits with the requested code
  cat > "$tmpdir/scripts/post-merge-hook.sh" <<EOF
#!/usr/bin/env bash
# Stub post-merge-hook — exits $hook_exit
echo "stub post-merge-hook exit=$hook_exit"
exit $hook_exit
EOF
  chmod +x "$tmpdir/scripts/post-merge-hook.sh"
}

# Run merge-and-hook.sh with stubs injected via PATH and SCRIPT_DIR override.
# We copy the real script to the temp dir so its SCRIPT_DIR resolves to the
# stub directory containing lib/ and post-merge-hook.sh.
run_script() {
  local tmpdir="$1"
  shift
  # Copy the real script into tmpdir/scripts/ so SCRIPT_DIR == tmpdir/scripts
  cp "$SCRIPT" "$tmpdir/scripts/merge-and-hook.sh"
  # D#2020: pin the new pr-dependents.sh lookup to test mode with a "no
  # dependents" default, keyed off the --pr value being invoked, so every
  # pre-existing test here keeps its original "no open dependents" behavior
  # (branch deleted as before) instead of falling through to the generic gh
  # stub below, which doesn't return valid JSON for the new lookups.
  local _pr_num="" _prev=""
  for _arg in "$@"; do
    if [[ "$_prev" == "--pr" ]]; then _pr_num="$_arg"; fi
    _prev="$_arg"
  done
  # Inject stub bin dir first in PATH so gh/python3 are overridden
  # D#1944: the CI gate now reads the CI_DISABLED repo variable first. Pin it
  # through the test seam so no test reaches the network — HTTP_404 means
  # "authoritatively absent", i.e. CI is on, which is what every pre-existing
  # test here assumes. Tests about the stand-down override it.
  env PATH="$tmpdir/bin:$PATH" \
    AUTONOMOUS_TEAM_REPO="${AUTONOMOUS_TEAM_REPO:-autonomous-agent-7/fulcrumaxe}" \
    AUTONOMOUS_TEAM_LOG_FILE="$tmpdir/logs/team.log" \
    AUTONOMOUS_TEAM_STATE_DIR="$tmpdir/state" \
    MERGE_AND_HOOK_LOG_DIR="$tmpdir/logs" \
    CI_STATUS_TEST_MODE=1 \
    CI_KILL_SWITCH_OVERRIDE="${CI_KILL_SWITCH_OVERRIDE:-HTTP_404}" \
    CI_STATUS_TEST_AUDIT_FILE="$tmpdir/state/audit.jsonl" \
    CI_MERGE_PROBE_ATTEMPTS="${CI_MERGE_PROBE_ATTEMPTS:-1}" \
    CI_MERGE_PROBE_INTERVAL="${CI_MERGE_PROBE_INTERVAL:-0}" \
    PR_DEPENDENTS_TEST_MODE="${PR_DEPENDENTS_TEST_MODE:-1}" \
    "PR_DEP_HEADREF_${_pr_num}=${PR_DEP_HEADREF_OVERRIDE:-test-branch-$_pr_num}" \
    PR_DEP_OPEN_LIST_JSON="${PR_DEP_OPEN_LIST_JSON:-[]}" \
    bash "$tmpdir/scripts/merge-and-hook.sh" "$@" 2>&1
}

# ── Test 1: hook exits 1 — exit code propagates ───────────────────────────────
echo "Test 1: hook exits 1 — exit code propagates"
T1=$(mktemp -d)
setup_stubs "$T1" 1
# Provide Gate markers so two-gate passes
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
run_script "$T1" --pr 999 > "$T1/out.txt" 2>&1
RC=$?
assert_exit "test1: exit code is 1" 1 "$RC"
unset TWO_GATE_PR_BODY_999
rm -rf "$T1"

# ── Test 2: hook exits 0 — exit code is 0 ────────────────────────────────────
echo "Test 2: hook exits 0 — exit code is 0"
T2=$(mktemp -d)
setup_stubs "$T2" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
run_script "$T2" --pr 999 > "$T2/out.txt" 2>&1
RC=$?
assert_exit "test2: exit code is 0" 0 "$RC"
unset TWO_GATE_PR_BODY_999
rm -rf "$T2"

# ── Test 3: hook exits 42 — exit code propagates exactly ─────────────────────
echo "Test 3: hook exits 42 — arbitrary non-zero exit propagates"
T3=$(mktemp -d)
setup_stubs "$T3" 42
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
run_script "$T3" --pr 999 > "$T3/out.txt" 2>&1
RC=$?
assert_exit "test3: exit code is 42" 42 "$RC"
unset TWO_GATE_PR_BODY_999
rm -rf "$T3"

# ── Test TG-1: Gate markers present — merge proceeds ─────────────────────────
echo "Test TG-1: Both Gate markers present — merge proceeds"
T_TG1=$(mktemp -d)
setup_stubs "$T_TG1" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
OUT_TG1=$(run_script "$T_TG1" --pr 999 2>&1)
RC_TG1=$?
assert_exit "TG-1: exits 0 when gates pass" 0 "$RC_TG1"
assert_contains "TG-1: Two-Gate check passed message" "Two-Gate check passed" "$OUT_TG1"
assert_contains "TG-1: gh pr merge was called" "GH_ARGS:" "$OUT_TG1"
assert_not_contains "TG-1: no Two-Gate FAIL message" "Two-Gate check FAILED" "$OUT_TG1"
unset TWO_GATE_PR_BODY_999
rm -rf "$T_TG1"

# ── Test TG-2: Missing Gate markers — exits 1, no merge ──────────────────────
echo "Test TG-2: Missing Gate markers — exits 1, no merge called"
T_TG2=$(mktemp -d)
setup_stubs "$T_TG2" 0
export TWO_GATE_PR_BODY_999="This PR has no gate markers at all."
OUT_TG2=$(run_script "$T_TG2" --pr 999 2>&1)
RC_TG2=$?
assert_exit "TG-2: exits 1 when gates missing" 1 "$RC_TG2"
assert_contains "TG-2: Two-Gate FAIL message appears" "Two-Gate check FAILED" "$OUT_TG2"
assert_not_contains "TG-2: gh pr merge NOT called" "GH_ARGS:" "$OUT_TG2"
unset TWO_GATE_PR_BODY_999
rm -rf "$T_TG2"

# ── Test TG-3: Only Gate 1 present — exits 1, no merge ───────────────────────
echo "Test TG-3: Only Gate 1 present, Gate 2 missing — exits 1"
T_TG3=$(mktemp -d)
setup_stubs "$T_TG3" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nSome text but no Gate 2."
OUT_TG3=$(run_script "$T_TG3" --pr 999 2>&1)
RC_TG3=$?
assert_exit "TG-3: exits 1 when Gate 2 missing" 1 "$RC_TG3"
assert_contains "TG-3: FAIL mentions Gate 2" "Gate 2" "$OUT_TG3"
assert_not_contains "TG-3: gh pr merge NOT called" "GH_ARGS:" "$OUT_TG3"
unset TWO_GATE_PR_BODY_999
rm -rf "$T_TG3"

# ── Test TG-4: --force-no-two-gate bypasses check, audit row written ──────────
echo "Test TG-4: --force-no-two-gate set — merge proceeds, audit row written"
T_TG4=$(mktemp -d)
setup_stubs "$T_TG4" 0
# Body with NO markers — normally would block
export TWO_GATE_PR_BODY_999="wiki update only, no code changes."
OUT_TG4=$(run_script "$T_TG4" --pr 999 --force-no-two-gate --bypass-reason "wiki-only PR" 2>&1)
RC_TG4=$?
assert_exit "TG-4: exits 0 with force flag" 0 "$RC_TG4"
assert_contains "TG-4: bypass warning appears" "WARNING: --force-no-two-gate" "$OUT_TG4"
assert_contains "TG-4: gh pr merge was called" "GH_ARGS:" "$OUT_TG4"
# Verify audit row was written
AUDIT_FILE="$T_TG4/state/audit.jsonl"
if [[ -f "$AUDIT_FILE" ]]; then
  AUDIT_CONTENT=$(cat "$AUDIT_FILE")
  assert_contains "TG-4: audit kind is manual_merge_two_gate_bypass" "manual_merge_two_gate_bypass" "$AUDIT_CONTENT"
  assert_contains "TG-4: audit has PR number" '"pr":999' "$AUDIT_CONTENT"
  assert_contains "TG-4: audit has reason" "wiki-only PR" "$AUDIT_CONTENT"
else
  fail "TG-4: audit file not written at $AUDIT_FILE"
fi
unset TWO_GATE_PR_BODY_999
rm -rf "$T_TG4"

# ── Test TG-5: --force-no-two-gate without reason — audit row still written ───
echo "Test TG-5: --force-no-two-gate without bypass reason — audit row still written"
T_TG5=$(mktemp -d)
setup_stubs "$T_TG5" 0
export TWO_GATE_PR_BODY_999="no markers here"
OUT_TG5=$(run_script "$T_TG5" --pr 999 --force-no-two-gate 2>&1)
RC_TG5=$?
assert_exit "TG-5: exits 0 with force flag (no reason)" 0 "$RC_TG5"
AUDIT_FILE_TG5="$T_TG5/state/audit.jsonl"
if [[ -f "$AUDIT_FILE_TG5" ]]; then
  assert_contains "TG-5: audit row present" "manual_merge_two_gate_bypass" "$(cat "$AUDIT_FILE_TG5")"
else
  fail "TG-5: audit file not written"
fi
unset TWO_GATE_PR_BODY_999
rm -rf "$T_TG5"

# ── Test HG7-1: no --discussion flag, but PR body has a resolvable Closes D#N,
#    Discussion is provenance:external, security-review-passed ABSENT — merge
#    is refused even though --discussion was never passed. This is the exact
#    bypass the security review flagged: HG-7 must not be skippable just by
#    omitting the flag.
echo "Test HG7-1: no --discussion, derived Discussion is external + label absent — merge refused"
T_HG1=$(mktemp -d)
setup_stubs "$T_HG1" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_SEC_REQUIRED_RC=0   # security-required exits 0 == required (label present)
export STUB_PR_LABELS="code-review-passed"  # no security-review-passed
OUT_HG1=$(run_script "$T_HG1" --pr 999 2>&1)
RC_HG1=$?
assert_exit "HG7-1: exits 1 — merge refused" 1 "$RC_HG1"
assert_contains "HG7-1: cites the derived Discussion" "Auto-detected Discussion #4200" "$OUT_HG1"
assert_contains "HG7-1: refuses for missing security-review-passed" "lacks the security-review-passed label" "$OUT_HG1"
assert_not_contains "HG7-1: gh pr merge NOT called" "pr merge" "$OUT_HG1"
unset TWO_GATE_PR_BODY_999 STUB_SEC_REQUIRED_RC STUB_PR_LABELS
rm -rf "$T_HG1"

# ── Test HG7-2: no --discussion flag, derived Discussion external, label
#    PRESENT — merge proceeds.
echo "Test HG7-2: no --discussion, derived Discussion is external + label present — merge proceeds"
T_HG2=$(mktemp -d)
setup_stubs "$T_HG2" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_SEC_REQUIRED_RC=0
export STUB_PR_LABELS="security-review-passed"
OUT_HG2=$(run_script "$T_HG2" --pr 999 2>&1)
RC_HG2=$?
assert_exit "HG7-2: exits 0 — merge proceeds" 0 "$RC_HG2"
assert_contains "HG7-2: HG-7 requirement satisfied message" "HG-7 requirement satisfied" "$OUT_HG2"
unset TWO_GATE_PR_BODY_999 STUB_SEC_REQUIRED_RC STUB_PR_LABELS
rm -rf "$T_HG2"

# ── Test HG7-3: Discussion cannot be resolved at all (no --discussion, no
#    resolvable Closes/Fixes/Resolves reference in the PR body) — fail closed,
#    refuse the direct-merge shortcut outright rather than silently skipping
#    the HG-7 check.
echo "Test HG7-3: Discussion unresolvable — merge refused (fail closed)"
T_HG3=$(mktemp -d)
setup_stubs "$T_HG3" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_BODY="Just a plain description, no closing keyword at all."
OUT_HG3=$(run_script "$T_HG3" --pr 999 2>&1)
RC_HG3=$?
assert_exit "HG7-3: exits 1 — merge refused" 1 "$RC_HG3"
assert_contains "HG7-3: cites inability to resolve Discussion" "could not resolve a Discussion number" "$OUT_HG3"
assert_not_contains "HG7-3: gh pr merge NOT called" "pr merge" "$OUT_HG3"
unset TWO_GATE_PR_BODY_999 STUB_PR_BODY
rm -rf "$T_HG3"

# ── Test HG7-4: Discussion cannot be resolved even when GraphQL validation
#    rejects the only candidate number (Issue/PR sharing the number, not a
#    real Discussion) — same fail-closed refusal.
echo "Test HG7-4: candidate number fails GraphQL validation — merge refused (fail closed)"
T_HG4=$(mktemp -d)
setup_stubs "$T_HG4" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_DISC_INVALID=1
OUT_HG4=$(run_script "$T_HG4" --pr 999 2>&1)
RC_HG4=$?
assert_exit "HG7-4: exits 1 — merge refused" 1 "$RC_HG4"
assert_contains "HG7-4: cites inability to resolve Discussion" "could not resolve a Discussion number" "$OUT_HG4"
unset TWO_GATE_PR_BODY_999 STUB_DISC_INVALID
rm -rf "$T_HG4"

# ── Test HG7-5: external_intake_gate.py fetch fails (rc=3, "unknown") —
#    treated as fail-closed/required, not "not required". Merge refused when
#    security-review-passed is absent.
echo "Test HG7-5: security-required fetch-failure (rc=3) treated as required — merge refused"
T_HG5=$(mktemp -d)
setup_stubs "$T_HG5" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_SEC_REQUIRED_RC=3
export STUB_PR_LABELS=""
OUT_HG5=$(run_script "$T_HG5" --pr 999 2>&1)
RC_HG5=$?
assert_exit "HG7-5: exits 1 — merge refused" 1 "$RC_HG5"
assert_contains "HG7-5: cites fetch failure / fail-closed" "GitHub API fetch failed/unknown" "$OUT_HG5"
assert_contains "HG7-5: refuses for missing label" "lacks the security-review-passed label" "$OUT_HG5"
unset TWO_GATE_PR_BODY_999 STUB_SEC_REQUIRED_RC STUB_PR_LABELS
rm -rf "$T_HG5"

# ── Test HG7-6: explicit --discussion still works and takes precedence over
#    derivation (no gh pr view --json body call needed to resolve it).
echo "Test HG7-6: explicit --discussion honored, not-required — merge proceeds"
T_HG6=$(mktemp -d)
setup_stubs "$T_HG6" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_SEC_REQUIRED_RC=1
OUT_HG6=$(run_script "$T_HG6" --pr 999 --discussion 55 2>&1)
RC_HG6=$?
assert_exit "HG7-6: exits 0 — merge proceeds" 0 "$RC_HG6"
assert_not_contains "HG7-6: does not need to auto-detect (explicit disc given)" "Auto-detected Discussion" "$OUT_HG6"
unset TWO_GATE_PR_BODY_999 STUB_SEC_REQUIRED_RC
rm -rf "$T_HG6"

# ═══════════════════════════════════════════════════════════════════════════
# D#1965 — merge failures must print a diagnostic instead of exiting silently.
#
# Root cause these cover: `set -euo pipefail` (merge-and-hook.sh:32) plus a
# BARE `ci_merge_sha_pinned ...` call in the merge loop. Under `set -e` a
# non-zero return from a bare call aborts the shell immediately, so `_MRC=$?`
# never ran and BOTH error branches were unreachable dead code — including the
# D#1614 409 head-moved retry.
#
# MF-1/MF-2/MF-3 must FAIL against an unpatched merge-and-hook.sh, and the
# failure must be the SILENCE (a missing message), not a harness error.
# ═══════════════════════════════════════════════════════════════════════════

# ── Test MF-1: merge fails with a conflict payload — diagnostic prints ───────
echo "Test MF-1: merge fails (conflict payload) — names the cause, exits 1"
T_MF1=$(mktemp -d)
setup_stubs "$T_MF1" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGE_RC=1
export STUB_MERGE_OUT="failed to merge pull request: Pull Request is not mergeable (HTTP 405)"
OUT_MF1=$(run_script "$T_MF1" --pr 999 2>&1)
RC_MF1=$?
assert_exit "MF-1: exits 1 on merge failure" 1 "$RC_MF1"
assert_contains "MF-1: surfaces the merge failure at all" "merge command failed:" "$OUT_MF1"
assert_contains "MF-1: quotes gh's real complaint" "Pull Request is not mergeable" "$OUT_MF1"
assert_contains "MF-1: classifies it as a conflict" "conflicts with its base" "$OUT_MF1"
assert_contains "MF-1: states the remedy" "resolve the conflicts, then re-run" "$OUT_MF1"
# AC-5: the file list is best-effort, but its absence must never be silent —
# when it cannot be computed the output must say so AND say why.
assert_contains "MF-1: conflicting-file list is accounted for" "conflicting files:" "$OUT_MF1"
if echo "$OUT_MF1" | grep -qF "conflicting files: unavailable ("; then
  pass "MF-1: unavailable file list states its reason"
elif echo "$OUT_MF1" | grep -qE '^\[merge-and-hook\]   \S'; then
  pass "MF-1: conflicting file list computed and printed"
else
  fail "MF-1: conflicting-file line was neither a real list nor an explained degradation"
fi
unset TWO_GATE_PR_BODY_999 STUB_MERGE_RC STUB_MERGE_OUT
rm -rf "$T_MF1"

# ── Test MF-2: merge fails, NOT a conflict — must not claim a conflict ───────
echo "Test MF-2: merge fails (permissions payload) — no bogus conflict claim"
T_MF2=$(mktemp -d)
setup_stubs "$T_MF2" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGE_RC=1
export STUB_MERGE_OUT="HTTP 403: Resource not accessible by integration"
OUT_MF2=$(run_script "$T_MF2" --pr 999 2>&1)
RC_MF2=$?
assert_exit "MF-2: exits 1 on merge failure" 1 "$RC_MF2"
assert_contains "MF-2: surfaces the raw reason" "merge command failed:" "$OUT_MF2"
assert_contains "MF-2: quotes the permissions error" "Resource not accessible" "$OUT_MF2"
assert_not_contains "MF-2: does NOT claim a conflict" "conflicts with its base" "$OUT_MF2"
assert_not_contains "MF-2: does NOT print a conflicting-file line" "conflicting files:" "$OUT_MF2"
unset TWO_GATE_PR_BODY_999 STUB_MERGE_RC STUB_MERGE_OUT
rm -rf "$T_MF2"

# ── Test MF-3: 409 head-moved — the D#1614 retry is reachable and retries ────
# CI_MERGE_MODE=conflict makes ci_merge_sha_pinned return 9 on every attempt.
# At T0 the script aborted with exit 9 on attempt 1, so the retry never ran —
# see the attempt-count note below for why the count must be >=3, not >=2.
echo "Test MF-3: 409 head-moved — re-gates and retries instead of aborting"
T_MF3=$(mktemp -d)
setup_stubs "$T_MF3" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export CI_MERGE_MODE=conflict
export STUB_CALL_LOG="$T_MF3/gh-calls.log"
: > "$STUB_CALL_LOG"
OUT_MF3=$(run_script "$T_MF3" --pr 999 2>&1)
RC_MF3=$?
assert_exit "MF-3: exits 1 after the bounded retry (not 9)" 1 "$RC_MF3"
assert_contains "MF-3: announces the head-moved retry" "head-moved conflict" "$OUT_MF3"
assert_contains "MF-3: reports the final failure" "ERROR: merge failed for PR #999" "$OUT_MF3"
# Attempt count. TWO headRefOid lookups happen before the merge loop is ever
# entered — check_ci_status --wait, then the _CUR_HEAD TOCTOU re-read — so an
# unpatched wrapper that aborts on attempt 1 still logs 2. Only the retry
# branch's own re-resolve pushes it to 3+, which is what proves attempt 2 ran.
# Measured: T0 (unpatched, rc=9) = 2 lookups; T1 (patched, rc=1) = 4.
# Counting `pr merge` calls is NOT an alternative here: CI_MERGE_MODE=conflict
# returns 9 before gh is reached, so that count is 0 on both sides.
HEADREF_CALLS=$(grep -c 'headRefOid' "$STUB_CALL_LOG" 2>/dev/null || true)
if [[ "${HEADREF_CALLS:-0}" -ge 3 ]]; then
  pass "MF-3: attempt 2 ran (headRefOid re-resolved, ${HEADREF_CALLS} lookups)"
else
  fail "MF-3: retry never ran — expected >=3 headRefOid lookups, got ${HEADREF_CALLS:-0}"
fi
unset TWO_GATE_PR_BODY_999 CI_MERGE_MODE STUB_CALL_LOG
rm -rf "$T_MF3"

# ── Test MF-4: ci_conflicting_files names real paths, and degrades honestly ──
# Unit-level, because AC-5 forbids BOTH a silently-missing list and a
# fabricated one. Builds a genuine two-sided conflict using only git plumbing
# (write-tree / commit-tree) — no branch, checkout, or reset needed, so the
# two sibling commits exist as raw objects with no refs pointing at them.
echo "Test MF-4: ci_conflicting_files — real paths when computable, reason when not"
T_MF4=$(mktemp -d)
MF4_SIDE_A=""
MF4_SIDE_B=""
if (cd "$T_MF4" && git init -q . 2>/dev/null); then
  MF4_REFS=$(
    cd "$T_MF4" || exit 1
    export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t.local
    export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t.local
    printf 'base\n' > conflicted.txt
    printf 'stable\n' > untouched.txt
    git add conflicted.txt untouched.txt
    c0=$(git commit-tree "$(git write-tree)" -m base)
    printf 'theirs\n' > conflicted.txt
    git add conflicted.txt
    ca=$(git commit-tree "$(git write-tree)" -p "$c0" -m theirs)
    printf 'ours\n' > conflicted.txt
    git add conflicted.txt
    cb=$(git commit-tree "$(git write-tree)" -p "$c0" -m ours)
    printf '%s %s\n' "$ca" "$cb"
  )
  MF4_SIDE_A=$(echo "$MF4_REFS" | awk '{print $1}')
  MF4_SIDE_B=$(echo "$MF4_REFS" | awk '{print $2}')
fi

# shellcheck source=/dev/null
source "$CI_STATUS_LIB"

if [[ -n "$MF4_SIDE_A" && -n "$MF4_SIDE_B" ]]; then
  ci_conflicting_files "$MF4_SIDE_A" "$MF4_SIDE_B" "$T_MF4"
  assert_contains "MF-4: names the genuinely conflicting file" "conflicted.txt" "$CI_CONFLICT_FILES"
  assert_not_contains "MF-4: does not fabricate unrelated paths" "untouched.txt" "$CI_CONFLICT_FILES"
  if [[ -z "$CI_CONFLICT_FILES_REASON" ]]; then
    pass "MF-4: no degradation reason set when the list is real"
  else
    fail "MF-4: reason should be empty on success, got '$CI_CONFLICT_FILES_REASON'"
  fi
else
  fail "MF-4: could not build the throwaway conflict repo"
fi

# Degradation A: refs absent from the local object store must yield an EMPTY
# list plus a stated reason — never a silent empty, never a guess.
ci_conflicting_files "no-such-base-ref" "no-such-head-ref" "$T_MF4"
if [[ -z "$CI_CONFLICT_FILES" && -n "$CI_CONFLICT_FILES_REASON" ]]; then
  pass "MF-4: absent refs degrade with an explicit reason ($CI_CONFLICT_FILES_REASON)"
else
  fail "MF-4: uncomputable list must be empty WITH a reason — files='$CI_CONFLICT_FILES' reason='$CI_CONFLICT_FILES_REASON'"
fi

# Degradation B: repo_dir that is not a git repo at all.
MF4_NOREPO=$(mktemp -d)
ci_conflicting_files main deadbeef "$MF4_NOREPO"
if [[ -z "$CI_CONFLICT_FILES" && "$CI_CONFLICT_FILES_REASON" == *"not a git repository"* ]]; then
  pass "MF-4: non-repo dir degrades with an explicit reason"
else
  fail "MF-4: non-repo dir must say so — files='$CI_CONFLICT_FILES' reason='$CI_CONFLICT_FILES_REASON'"
fi
rm -rf "$T_MF4" "$MF4_NOREPO"

# ── Test MF-5: the wrapper's conflict diagnostic on REALLY resolvable refs ───
# MF-1 exercises the degradation branch (its stub SHA is not a real object).
# This one points the wrapper at a genuine repo via CI_CONFLICT_REPO_DIR so the
# computable branch runs end-to-end and prints an actual file list.
echo "Test MF-5: conflict diagnostic prints a real file list when refs resolve"
T_MF5=$(mktemp -d)
setup_stubs "$T_MF5" 0
MF5_REPO=$(mktemp -d)
MF5_HEAD=""
if (cd "$MF5_REPO" && git init -q . 2>/dev/null); then
  MF5_HEAD=$(
    cd "$MF5_REPO" || exit 1
    export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t.local
    export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t.local
    printf 'base\n' > shared.txt
    printf 'quiet\n' > other.txt
    git add shared.txt other.txt
    c0=$(git commit-tree "$(git write-tree)" -m base)
    git update-ref refs/heads/main "$c0"
    printf 'theirs\n' > shared.txt
    git add shared.txt
    ca=$(git commit-tree "$(git write-tree)" -p "$c0" -m theirs)
    git update-ref refs/heads/main "$ca"
    printf 'ours\n' > shared.txt
    git add shared.txt
    git commit-tree "$(git write-tree)" -p "$c0" -m ours
  )
fi
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGE_RC=1
export STUB_MERGE_OUT="failed to merge pull request: Pull Request is not mergeable (HTTP 405)"
export STUB_HEAD_SHA="$MF5_HEAD"
export CI_CONFLICT_REPO_DIR="$MF5_REPO"
OUT_MF5=$(run_script "$T_MF5" --pr 999 2>&1)
RC_MF5=$?
assert_exit "MF-5: exits 1 on merge failure" 1 "$RC_MF5"
assert_contains "MF-5: classifies it as a conflict" "conflicts with its base" "$OUT_MF5"
assert_contains "MF-5: prints the real conflicting path" "shared.txt" "$OUT_MF5"
assert_not_contains "MF-5: does not name non-conflicting files" "other.txt" "$OUT_MF5"
assert_not_contains "MF-5: did not fall back to the unavailable branch" "conflicting files: unavailable" "$OUT_MF5"
unset TWO_GATE_PR_BODY_999 STUB_MERGE_RC STUB_MERGE_OUT STUB_HEAD_SHA CI_CONFLICT_REPO_DIR
rm -rf "$T_MF5" "$MF5_REPO"

# ═══════════════════════════════════════════════════════════════════════════
# D#1944 — CI_DISABLED stand-down, and what the bypass row is worth.
#
# MISSING_MATRIX is the live shape of the bug: a job-level `if:` is evaluated
# before matrix expansion, so tui/dashboard/ts-backend never register at all
# and only the non-matrix jobs appear.
# ═══════════════════════════════════════════════════════════════════════════
MISSING_MATRIX='[{"name":"backend (import-smoke)","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"open-source export audit","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""}]'

_audit_count() {
  local file="$1" kind="$2" n
  [[ -f "$file" ]] || { echo 0; return 0; }
  # grep -c prints 0 and exits 1 on no match — the exit code must not be
  # allowed to append a second line to the count.
  n=$(grep -c "\"kind\": \"$kind\"" "$file" 2>/dev/null || true)
  echo "${n:-0}"
}

# ── Test CD-1: CI_DISABLED=true — gate stands down, merge proceeds ──────────
# The distinction this test exists for: a stand-down writes ci_gate_stood_down
# and NOT manual_merge_ci_bypass. If the two collapsed into one kind, the
# bypass row would go back to meaning nothing, which is the whole reason the
# stand-down is a separate outcome rather than another use of --force-no-ci.
echo "Test CD-1: CI_DISABLED=true — stand-down row written, bypass row not, merge proceeds"
T_CD1=$(mktemp -d)
setup_stubs "$T_CD1" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export CI_KILL_SWITCH_OVERRIDE=true
export STUB_CI_CHECK_RUNS="$MISSING_MATRIX"
OUT_CD1=$(run_script "$T_CD1" --pr 999 2>&1)
RC_CD1=$?
assert_exit "CD-1: exits 0 — merge proceeds" 0 "$RC_CD1"
assert_contains "CD-1: says the gate stood down" "CI gate STOOD DOWN" "$OUT_CD1"
assert_contains "CD-1: says the merge is not CI-verified" "NOT CI-verified" "$OUT_CD1"
assert_contains "CD-1: gh pr merge was called" "GH_ARGS:" "$OUT_CD1"
AUDIT_CD1="$T_CD1/state/audit.jsonl"
N_STOOD=$(_audit_count "$AUDIT_CD1" "ci_gate_stood_down")
N_BYPASS=$(_audit_count "$AUDIT_CD1" "manual_merge_ci_bypass")
if [[ "$N_STOOD" -eq 1 ]]; then pass "CD-1: exactly 1 ci_gate_stood_down row"; else fail "CD-1: expected 1 ci_gate_stood_down row, got $N_STOOD"; fi
if [[ "$N_BYPASS" -eq 0 ]]; then pass "CD-1: zero manual_merge_ci_bypass rows"; else fail "CD-1: expected 0 manual_merge_ci_bypass rows, got $N_BYPASS"; fi
# D#2271 PR-a: the stand-down row already records the decline — the new
# ci_note_merge_if_unverified fallback (kind=ci_gate_unverified_merge) must
# see _CI_AUDIT_WRITTEN=true from this branch and add nothing on top of it.
# A total of exactly 1 row is the only way to see a silent double-write.
N_TOTAL_CD1=$(grep -c '"kind"' "$AUDIT_CD1" 2>/dev/null || true)
if [[ "${N_TOTAL_CD1:-0}" -eq 1 ]]; then
  pass "CD-1: exactly 1 kind-bearing row total (no unverified-merge double-write)"
else
  fail "CD-1: expected exactly 1 kind-bearing row total, got ${N_TOTAL_CD1:-0}"
fi
if python3 -c '
import json, sys
for line in open(sys.argv[1]):
    row = json.loads(line)
    if row.get("kind") == "ci_gate_stood_down":
        assert row.get("pr") == 999, row
        assert row.get("reason"), "reason must be non-empty"
        sys.exit(0)
sys.exit(1)
' "$AUDIT_CD1" 2>/dev/null; then
  pass "CD-1: stood-down row carries pr=999 and a non-empty reason"
else
  fail "CD-1: stood-down row missing pr/reason"
fi
unset TWO_GATE_PR_BODY_999 CI_KILL_SWITCH_OVERRIDE STUB_CI_CHECK_RUNS
rm -rf "$T_CD1"

# ── Test CD-2: --force-no-ci records what was actually red ──────────────────
# Before this change the bypass short-circuited BEFORE the gate ran, so
# head_sha / failing_checks / run_url went into every row as empty strings —
# 37 stored rows, none of which can tell you what was overridden. Asserting
# only that the row exists would have passed against that version too.
echo "Test CD-2: --force-no-ci — bypass row records the real head SHA and failing checks"
T_CD2=$(mktemp -d)
setup_stubs "$T_CD2" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_CI_CHECK_RUNS="$MISSING_MATRIX"
export STUB_HEAD_SHA="cafebabe1944"
OUT_CD2=$(run_script "$T_CD2" --pr 999 --force-no-ci --bypass-reason "x" 2>&1)
RC_CD2=$?
assert_exit "CD-2: exits 0 — bypass merges" 0 "$RC_CD2"
AUDIT_CD2="$T_CD2/state/audit.jsonl"
if python3 -c '
import json, sys
for line in open(sys.argv[1]):
    row = json.loads(line)
    if row.get("kind") == "manual_merge_ci_bypass":
        assert row.get("head_sha"), "head_sha is empty"
        assert row.get("failing_checks"), "failing_checks is empty"
        assert "tui" in row["failing_checks"], row["failing_checks"]
        sys.exit(0)
sys.exit(1)
' "$AUDIT_CD2" 2>/dev/null; then
  pass "CD-2: bypass row has a real head_sha and failing_checks naming tui"
else
  fail "CD-2: bypass row still has empty head_sha/failing_checks — the gate did not run before the override"
fi
N_TOTAL_CD2=$(grep -c '"kind"' "$AUDIT_CD2" 2>/dev/null || true)
if [[ "${N_TOTAL_CD2:-0}" -eq 1 ]]; then
  pass "CD-2: exactly 1 kind-bearing row total (no unverified-merge double-write)"
else
  fail "CD-2: expected exactly 1 kind-bearing row total, got ${N_TOTAL_CD2:-0}"
fi
unset TWO_GATE_PR_BODY_999 STUB_CI_CHECK_RUNS STUB_HEAD_SHA
rm -rf "$T_CD2"

# ── Test CD-3: --force-no-ci without a reason is refused outright ───────────
echo "Test CD-3: --force-no-ci with no/empty --bypass-reason — refused, nothing written"
for _variant in "missing" "empty"; do
  T_CD3=$(mktemp -d)
  setup_stubs "$T_CD3" 0
  export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
  if [[ "$_variant" == "missing" ]]; then
    OUT_CD3=$(run_script "$T_CD3" --pr 999 --force-no-ci 2>&1)
  else
    OUT_CD3=$(run_script "$T_CD3" --pr 999 --force-no-ci --bypass-reason "" 2>&1)
  fi
  RC_CD3=$?
  if [[ "$RC_CD3" -ne 0 ]]; then pass "CD-3/$_variant: exits non-zero"; else fail "CD-3/$_variant: expected non-zero exit, got 0"; fi
  assert_contains "CD-3/$_variant: names the missing flag" "--bypass-reason" "$OUT_CD3"
  assert_not_contains "CD-3/$_variant: gh pr merge NOT called" "pr merge" "$OUT_CD3"
  AUDIT_CD3="$T_CD3/state/audit.jsonl"
  if [[ ! -s "$AUDIT_CD3" ]]; then
    pass "CD-3/$_variant: audit file is empty — zero rows written"
  else
    fail "CD-3/$_variant: audit rows were written: $(cat "$AUDIT_CD3")"
  fi
  unset TWO_GATE_PR_BODY_999
  rm -rf "$T_CD3"
done

# ── Test CD-4 (D#2271 AC-1/AC-3): a genuinely green merge writes exactly one
#    ci_gate_verified row and nothing else — the positive marker this whole
#    streak design resets on. ───────────────────────────────────────────────
echo "Test CD-4: green CI — exactly one ci_gate_verified row, no fallback row"
T_CD4=$(mktemp -d)
setup_stubs "$T_CD4" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
OUT_CD4=$(run_script "$T_CD4" --pr 999 2>&1)
RC_CD4=$?
assert_exit "CD-4: exits 0 — merge proceeds" 0 "$RC_CD4"
AUDIT_CD4="$T_CD4/state/audit.jsonl"
N_VERIFIED_CD4=$(_audit_count "$AUDIT_CD4" "ci_gate_verified")
N_TOTAL_CD4=$(grep -c '"kind"' "$AUDIT_CD4" 2>/dev/null || true)
if [[ "$N_VERIFIED_CD4" -eq 1 ]]; then pass "CD-4: exactly 1 ci_gate_verified row"; else fail "CD-4: expected 1 ci_gate_verified row, got $N_VERIFIED_CD4"; fi
if [[ "${N_TOTAL_CD4:-0}" -eq 1 ]]; then
  pass "CD-4: exactly 1 kind-bearing row total (verified merge writes no fallback row)"
else
  fail "CD-4: expected exactly 1 kind-bearing row total, got ${N_TOTAL_CD4:-0}: $(cat "$AUDIT_CD4" 2>/dev/null)"
fi
unset TWO_GATE_PR_BODY_999
rm -rf "$T_CD4"

# ── Test CD-5 (D#2271 AC-4, mechanism level): a merge that proceeds without
#    CI_STATUS_STATE reaching "pass" and without any decline-reason row
#    already written leaves the fallback marker — this is the property that
#    makes a FUTURE bypass which writes nothing of its own still visible to
#    backend/gate_streak.py. There is no such bypass in this codebase today
#    (PR-a adds none — see CLAUDE.md Merge Gate Protocol / the Spec's
#    Pushback section), so this exercises ci_note_merge_if_unverified
#    directly rather than inventing a throwaway escape hatch just to drive
#    it through the full script. ───────────────────────────────────────────
echo "Test CD-5: a merge with no prior audit row still leaves the fallback marker"
AUDIT_CD5="$(mktemp)"
(
  source "$CI_STATUS_LIB"
  export CI_STATUS_TEST_MODE=1
  export CI_STATUS_TEST_AUDIT_FILE="$AUDIT_CD5"
  CI_STATUS_STATE="fail"
  ci_note_merge_if_unverified 12345 "silentbypasssha" "false"
)
if grep -q '"kind": "ci_gate_unverified_merge"' "$AUDIT_CD5" 2>/dev/null; then
  pass "CD-5: fallback row written when nothing else recorded the decline"
else
  fail "CD-5: expected a ci_gate_unverified_merge row, got: $(cat "$AUDIT_CD5" 2>/dev/null)"
fi
rm -f "$AUDIT_CD5"

# ══ D#2332: browser-test gate on the manual merge path ════════════════════════
# The loop auto-merge path refuses a dashboard PR without browser-test-passed.
# This path did not, so a five-file dashboard PR reached main carrying exactly
# one label. Every test below drives the REAL scripts/merge-and-hook.sh — only
# gh and post-merge-hook.sh are stubbed, so the gate ordering, the exit codes
# and the audit write are the production ones.

# A diff that trips check-pr-dashboard-touched.sh's `grep -q '^dashboard/'`.
BT_DASHBOARD_DIFF='dashboard/src/api/client.ts
dashboard/src/pages/stats/AnalystFindingsTile.tsx
backend/server.py'

# ── Test BT-1: dashboard PR, no browser-test-passed — refused ────────────────
echo "Test BT-1: dashboard PR without browser-test-passed — refused, not merged"
T_BT1=$(mktemp -d)
setup_stubs "$T_BT1" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_DIFF_FILES="$BT_DASHBOARD_DIFF"
export STUB_PR_LABELS="code-review-passed"
OUT_BT1=$(run_script "$T_BT1" --pr 999 2>&1)
RC_BT1=$?
assert_exit "BT-1: exits 1" 1 "$RC_BT1"
assert_contains "BT-1: names the missing label" "does not carry the browser-test-passed label" "$OUT_BT1"
assert_contains "BT-1: names the dashboard as the reason" "touches dashboard/" "$OUT_BT1"
assert_not_contains "BT-1: no merge happened" "PR #999 merged." "$OUT_BT1"
assert_not_contains "BT-1: refused before the Two-Gate step ran" "Two-Gate check passed" "$OUT_BT1"
unset TWO_GATE_PR_BODY_999 STUB_PR_DIFF_FILES STUB_PR_LABELS
rm -rf "$T_BT1"

# ── Test BT-2: dashboard PR WITH browser-test-passed — proceeds ──────────────
echo "Test BT-2: dashboard PR carrying browser-test-passed — proceeds unchanged"
T_BT2=$(mktemp -d)
setup_stubs "$T_BT2" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_DIFF_FILES="$BT_DASHBOARD_DIFF"
export STUB_PR_LABELS="code-review-passed
browser-test-passed"
OUT_BT2=$(run_script "$T_BT2" --pr 999 2>&1)
RC_BT2=$?
assert_exit "BT-2: exits 0" 0 "$RC_BT2"
assert_contains "BT-2: gate reports satisfied" "browser-test gate satisfied" "$OUT_BT2"
assert_contains "BT-2: the later gates still ran" "Two-Gate check passed" "$OUT_BT2"
assert_contains "BT-2: merge happened" "PR #999 merged." "$OUT_BT2"
unset TWO_GATE_PR_BODY_999 STUB_PR_DIFF_FILES STUB_PR_LABELS
rm -rf "$T_BT2"

# ── Test BT-3: non-dashboard PR — gate is inert either way ───────────────────
echo "Test BT-3: PR touching no dashboard file — unaffected by the gate"
T_BT3=$(mktemp -d)
setup_stubs "$T_BT3" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_DIFF_FILES='backend/server.py
scripts/merge-and-hook.sh'
export STUB_PR_LABELS="code-review-passed"
OUT_BT3=$(run_script "$T_BT3" --pr 999 2>&1)
RC_BT3=$?
assert_exit "BT-3: exits 0" 0 "$RC_BT3"
assert_contains "BT-3: merge happened" "PR #999 merged." "$OUT_BT3"
assert_not_contains "BT-3: gate said nothing at all" "browser-test" "$OUT_BT3"
unset TWO_GATE_PR_BODY_999 STUB_PR_DIFF_FILES STUB_PR_LABELS
rm -rf "$T_BT3"

# ── Test BT-4: --force-no-browser-test with no reason — refused, no audit row ─
#    The refusal must land before ANY side effect, so a rejected invocation
#    leaves the audit trail byte-identical. Seeded with a row first so the
#    assertion is "unchanged", not "still empty".
echo "Test BT-4: --force-no-browser-test without --bypass-reason — refused, audit unchanged"
T_BT4=$(mktemp -d)
setup_stubs "$T_BT4" 0
AUDIT_BT4="$T_BT4/state/audit.jsonl"
printf '%s\n' '{"kind":"ci_gate_verified","pr":1,"reason":"seed"}' > "$AUDIT_BT4"
N_BEFORE_BT4=$(wc -l < "$AUDIT_BT4")
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_DIFF_FILES="$BT_DASHBOARD_DIFF"
export STUB_PR_LABELS="code-review-passed"
OUT_BT4=$(run_script "$T_BT4" --pr 999 --force-no-browser-test 2>&1)
RC_BT4=$?
N_AFTER_BT4=$(wc -l < "$AUDIT_BT4")
assert_exit "BT-4: exits 1" 1 "$RC_BT4"
assert_contains "BT-4: says the reason is required" "--force-no-browser-test requires --bypass-reason" "$OUT_BT4"
assert_not_contains "BT-4: no merge happened" "PR #999 merged." "$OUT_BT4"
if [[ "$N_BEFORE_BT4" -eq "$N_AFTER_BT4" ]]; then
  pass "BT-4: audit line count unchanged ($N_BEFORE_BT4)"
else
  fail "BT-4: audit line count changed $N_BEFORE_BT4 -> $N_AFTER_BT4: $(cat "$AUDIT_BT4")"
fi
unset TWO_GATE_PR_BODY_999 STUB_PR_DIFF_FILES STUB_PR_LABELS
rm -rf "$T_BT4"

# ── Test BT-5: --force-no-browser-test WITH a reason — merges, one audit row ──
echo "Test BT-5: --force-no-browser-test --bypass-reason — merges, one audit row"
T_BT5=$(mktemp -d)
setup_stubs "$T_BT5" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_DIFF_FILES="$BT_DASHBOARD_DIFF"
export STUB_PR_LABELS="code-review-passed"
OUT_BT5=$(run_script "$T_BT5" --pr 999 --force-no-browser-test --bypass-reason "css-only, no rendered change" 2>&1)
RC_BT5=$?
AUDIT_BT5="$T_BT5/state/audit.jsonl"
assert_exit "BT-5: exits 0" 0 "$RC_BT5"
assert_contains "BT-5: loud warning on the bypass" "--force-no-browser-test used for PR #999" "$OUT_BT5"
assert_contains "BT-5: reason echoed" "css-only, no rendered change" "$OUT_BT5"
assert_contains "BT-5: merge happened" "PR #999 merged." "$OUT_BT5"
N_BT5=$(_audit_count "$AUDIT_BT5" "manual_merge_browser_test_bypass")
if [[ "$N_BT5" -eq 1 ]]; then
  pass "BT-5: exactly 1 manual_merge_browser_test_bypass row"
else
  fail "BT-5: expected 1 manual_merge_browser_test_bypass row, got $N_BT5: $(cat "$AUDIT_BT5" 2>/dev/null)"
fi
assert_contains "BT-5: audit row carries the PR number" '"pr": 999' "$(cat "$AUDIT_BT5" 2>/dev/null)"
assert_contains "BT-5: audit row carries the reason" "css-only, no rendered change" "$(cat "$AUDIT_BT5" 2>/dev/null)"
unset TWO_GATE_PR_BODY_999 STUB_PR_DIFF_FILES STUB_PR_LABELS
rm -rf "$T_BT5"

# ── Test BT-6: the refusal lands before the CI-status wait ───────────────────
#    merge-and-hook.sh blocks up to CI_MAX_WAIT_SECONDS (1200s) on the CI gate.
#    A gate that refused only after that wait would cost 20 minutes per blocked
#    dashboard merge. Proven by the call log: a refused BT-1 must never have
#    asked GitHub for a check-run.
echo "Test BT-6: refusal happens before any check-runs fetch (i.e. before the CI wait)"
T_BT6=$(mktemp -d)
setup_stubs "$T_BT6" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_DIFF_FILES="$BT_DASHBOARD_DIFF"
export STUB_PR_LABELS="code-review-passed"
export STUB_CALL_LOG="$T_BT6/gh-calls.log"
OUT_BT6=$(run_script "$T_BT6" --pr 999 2>&1)
RC_BT6=$?
assert_exit "BT-6: exits 1" 1 "$RC_BT6"
# The absence assertion needs the log to exist first. `grep -q … 2>/dev/null`
# on a missing or empty file exits 1, which reads as "no check-runs fetched" —
# so a change that silently stopped the stub writing the log would turn this
# green while measuring nothing. Given what this suite gates, an assertion that
# passes by never running is exactly the shape not to ship.
if [[ ! -s "$STUB_CALL_LOG" ]]; then
  fail "BT-6: call log missing or empty — the check-runs assertion below would pass vacuously"
elif grep -q "check-runs" "$STUB_CALL_LOG" 2>/dev/null; then
  fail "BT-6: CI check-runs were fetched before the browser gate refused: $(cat "$STUB_CALL_LOG")"
else
  pass "BT-6: no check-runs fetch in $(wc -l < "$STUB_CALL_LOG") logged gh call(s) — refused before the CI-status wait"
fi
unset TWO_GATE_PR_BODY_999 STUB_PR_DIFF_FILES STUB_PR_LABELS STUB_CALL_LOG
rm -rf "$T_BT6"

# ── Test BT-7: the dashboard-touched predicate is missing — fail closed ──────
#    check-pr-dashboard-touched.sh returning non-zero means "no dashboard
#    files". An absent script would return 127, which reads identically — a
#    silent fail-open on the one script the gate depends on.
echo "Test BT-7: check-pr-dashboard-touched.sh absent — refuses rather than assuming no"
T_BT7=$(mktemp -d)
setup_stubs "$T_BT7" 0
rm -f "$T_BT7/scripts/check-pr-dashboard-touched.sh"
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_DIFF_FILES="$BT_DASHBOARD_DIFF"
OUT_BT7=$(run_script "$T_BT7" --pr 999 2>&1)
RC_BT7=$?
assert_exit "BT-7: exits 1" 1 "$RC_BT7"
assert_contains "BT-7: says why it cannot decide" "cannot tell whether PR #999 touches the dashboard" "$OUT_BT7"
assert_not_contains "BT-7: no merge happened" "PR #999 merged." "$OUT_BT7"
unset TWO_GATE_PR_BODY_999 STUB_PR_DIFF_FILES
rm -rf "$T_BT7"

# ══ D#2339: a conflicting branch is reported as conflicting, not as slow CI ══
# A CONFLICTING PR gets zero check-runs from GitHub, so the CI wait can never
# succeed on one — it ran its full CI_MAX_WAIT_SECONDS and then reported "CI
# wait timed out ... no github-actions check-runs registered yet", blaming slow
# CI for a branch that could never have merged. Every test below drives the
# REAL scripts/merge-and-hook.sh; only gh and post-merge-hook.sh are stubbed,
# so the gate ordering, the wording and the exit codes are the production ones.

# ── Test MC-1: CONFLICTING — refused, named as a conflict, never as a timeout ─
echo "Test MC-1: CONFLICTING PR — refused as a conflict, not as a CI timeout"
T_MC1=$(mktemp -d)
setup_stubs "$T_MC1" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE="CONFLICTING|DIRTY"
OUT_MC1=$(run_script "$T_MC1" --pr 999 2>&1)
RC_MC1=$?
assert_exit "MC-1: exits 1" 1 "$RC_MC1"
assert_contains "MC-1: says the PR is conflicting" "GitHub reports it as conflicting" "$OUT_MC1"
# The message must name the field that actually fired. Here `mergeable` did.
assert_contains "MC-1: names mergeable as the trigger" "via mergeable=CONFLICTING" "$OUT_MC1"
assert_contains "MC-1: shows the status it observed alongside" "mergeStateStatus=DIRTY" "$OUT_MC1"
# AC-22: the same sentence the direct-merge path prints, not a second phrasing.
assert_contains "MC-1: reuses the direct-merge wording" "cause: this branch conflicts with its base and is not mergeable." "$OUT_MC1"
assert_contains "MC-1: states the same remedy" "resolve the conflicts, then re-run" "$OUT_MC1"
assert_contains "MC-1: accounts for the conflicting-file list" "conflicting files:" "$OUT_MC1"
assert_not_contains "MC-1: never blames the CI wait" "CI wait timed out" "$OUT_MC1"
assert_not_contains "MC-1: no merge happened" "PR #999 merged." "$OUT_MC1"
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE
rm -rf "$T_MC1"

# ── Test MC-2: the refusal lands before any check-runs fetch ─────────────────
#    This is the item the whole change turns on. A conflict check that ran
#    AFTER the CI wait would be nearly worthless: twenty minutes of waiting to
#    be told the branch could never have merged. Proven by the call log —
#    same instrument BT-6 uses — rather than by reading the code.
echo "Test MC-2: refusal happens before any check-runs fetch (i.e. before the CI wait)"
T_MC2=$(mktemp -d)
setup_stubs "$T_MC2" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE="CONFLICTING|DIRTY"
export STUB_CALL_LOG="$T_MC2/gh-calls.log"
: > "$STUB_CALL_LOG"
OUT_MC2=$(run_script "$T_MC2" --pr 999 2>&1)
RC_MC2=$?
assert_exit "MC-2: exits 1" 1 "$RC_MC2"
# The absence assertion needs the log to exist first — `grep -q` on an empty
# file exits 1, which reads as "no check-runs fetched", so a stub that stopped
# writing the log would turn this green while measuring nothing.
if [[ ! -s "$STUB_CALL_LOG" ]]; then
  fail "MC-2: call log missing or empty — the check-runs assertion below would pass vacuously"
elif grep -q "check-runs" "$STUB_CALL_LOG" 2>/dev/null; then
  fail "MC-2: CI check-runs were fetched before the conflict was reported: $(cat "$STUB_CALL_LOG")"
else
  pass "MC-2: no check-runs fetch in $(wc -l < "$STUB_CALL_LOG") logged gh call(s) — refused before the CI-status wait"
fi
if grep -q -- "--json mergeable" "$STUB_CALL_LOG" 2>/dev/null; then
  pass "MC-2: the mergeability probe is what asked GitHub"
else
  fail "MC-2: no mergeable probe in the call log — the refusal came from somewhere else: $(cat "$STUB_CALL_LOG")"
fi
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE STUB_CALL_LOG
rm -rf "$T_MC2"

# ── Test MC-3: timed — refuses in seconds with a long CI wait armed ─────────
#    The check-runs stub returns an empty list, which is exactly the
#    pending-forever shape a conflicting PR produces on the real API — GitHub
#    registers no check-run at all on a head it cannot merge.
#
#    CI_MAX_WAIT_SECONDS is pinned HERE rather than inherited, so an ambient
#    export cannot quietly shrink the wait this test is measuring against and
#    turn the assertion green without the fix. 120s is a stand-in for the
#    production 1200s: long enough that crossing the 30s bar means the wait was
#    actually entered, short enough not to cost twenty minutes to demonstrate.
#    Measured against the unpatched script with these same values: 120s and a
#    "CI wait timed out" verdict on a branch reported CONFLICTING.
echo "Test MC-3: refusal is fast even with a long CI wait armed"
T_MC3=$(mktemp -d)
setup_stubs "$T_MC3" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE="CONFLICTING|DIRTY"
export STUB_CI_CHECK_RUNS='[]'
export CI_MAX_WAIT_SECONDS=120
export CI_POLL_INTERVAL=10
_MC3_T0=$SECONDS
OUT_MC3=$(run_script "$T_MC3" --pr 999 2>&1)
RC_MC3=$?
_MC3_ELAPSED=$(( SECONDS - _MC3_T0 ))
assert_exit "MC-3: exits 1" 1 "$RC_MC3"
if [[ "$_MC3_ELAPSED" -lt 30 ]]; then
  pass "MC-3: refused in ${_MC3_ELAPSED}s (< 30s) with a 120s CI wait armed"
else
  fail "MC-3: took ${_MC3_ELAPSED}s — the refusal is not ahead of the CI wait"
fi
assert_not_contains "MC-3: not reported as a timeout" "CI wait timed out" "$OUT_MC3"
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE STUB_CI_CHECK_RUNS CI_MAX_WAIT_SECONDS CI_POLL_INTERVAL
rm -rf "$T_MC3"

# ── Tests MC-4/5/6: DIRTY, BLOCKED and BEHIND are three different states ─────
#    Only one of them is a conflict. The three runs differ in nothing but the
#    mergeStateStatus string, so this is the discrimination itself and not a
#    claim about it. BLOCKED means a required review or check is missing —
#    this script already refuses that by name, with its own message, and
#    refusing twice for one reason is worse than once. BEHIND is mergeable.
echo "Test MC-4: mergeStateStatus DIRTY with mergeable UNKNOWN — refused"
T_MC4=$(mktemp -d)
setup_stubs "$T_MC4" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE="UNKNOWN|DIRTY"
OUT_MC4=$(run_script "$T_MC4" --pr 999 2>&1)
RC_MC4=$?
assert_exit "MC-4: exits 1" 1 "$RC_MC4"
assert_contains "MC-4: named as a conflict" "conflicts with its base" "$OUT_MC4"
assert_not_contains "MC-4: no merge happened" "PR #999 merged." "$OUT_MC4"
# The refusal is right on this path; the MESSAGE is the thing under test here.
# GitHub returned mergeable=UNKNOWN, so a message saying "mergeable=CONFLICTING"
# would assert a value it never returned — the same defect class as reporting a
# conflicting branch as slow CI, one layer down. The negative assertion is the
# load-bearing one: it fails against a hardcoded trigger string even though the
# refusal itself works.
assert_contains "MC-4: names mergeStateStatus as the trigger" "via mergeStateStatus=DIRTY" "$OUT_MC4"
assert_not_contains "MC-4: never claims GitHub returned mergeable=CONFLICTING" "mergeable=CONFLICTING" "$OUT_MC4"
assert_contains "MC-4: reports the mergeable value actually observed" "observed mergeable=UNKNOWN" "$OUT_MC4"
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE
rm -rf "$T_MC4"

for _mc_state in BLOCKED BEHIND; do
  echo "Test MC-5/6: mergeStateStatus $_mc_state — NOT treated as a conflict"
  T_MC5=$(mktemp -d)
  setup_stubs "$T_MC5" 0
  export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
  export STUB_MERGEABLE="UNKNOWN|$_mc_state"
  OUT_MC5=$(run_script "$T_MC5" --pr 999 2>&1)
  RC_MC5=$?
  assert_exit "MC-5/6/$_mc_state: exits 0 — the probe did not refuse" 0 "$RC_MC5"
  assert_not_contains "MC-5/6/$_mc_state: no conflict claim" "conflicts with its base" "$OUT_MC5"
  assert_contains "MC-5/6/$_mc_state: merge proceeded" "PR #999 merged." "$OUT_MC5"
  unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE
  rm -rf "$T_MC5"
done

# ── Test MC-7: UNKNOWN falls through to the existing wait, unchanged ─────────
#    AC-20: the timeout path is preceded, not replaced. GitHub computes
#    mergeability asynchronously, so a check that treated UNKNOWN as a conflict
#    would refuse a freshly-pushed branch for exactly the wrong reason — it
#    would be at its most confident precisely when it knows least.
echo "Test MC-7: mergeable UNKNOWN — falls through, existing timeout path intact"
T_MC7=$(mktemp -d)
setup_stubs "$T_MC7" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE="UNKNOWN|UNKNOWN"
export STUB_CI_CHECK_RUNS='[]'
export CI_MAX_WAIT_SECONDS=2
export CI_POLL_INTERVAL=1
OUT_MC7=$(run_script "$T_MC7" --pr 999 2>&1)
RC_MC7=$?
assert_exit "MC-7: exits 1 — via the CI gate, not the probe" 1 "$RC_MC7"
assert_contains "MC-7: says the probe was inconclusive" "mergeability probe inconclusive" "$OUT_MC7"
assert_contains "MC-7: the existing timeout path still reports a timeout" "CI wait timed out" "$OUT_MC7"
assert_not_contains "MC-7: never claims a conflict it cannot see" "conflicts with its base" "$OUT_MC7"
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE STUB_CI_CHECK_RUNS CI_MAX_WAIT_SECONDS CI_POLL_INTERVAL
rm -rf "$T_MC7"

# ── Test MC-8: becomes conflicting DURING the wait — re-checked on timeout ───
#    AC-21. main moves while we poll, so a branch that was clean at the probe
#    can be conflicting by the time the wait ends. The sequence stub answers
#    MERGEABLE first (Step 0c) and CONFLICTING afterwards (the re-probe).
echo "Test MC-8: branch goes conflicting during the wait — reported as a conflict, not a timeout"
T_MC8=$(mktemp -d)
setup_stubs "$T_MC8" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE_SEQ="MERGEABLE|CLEAN;CONFLICTING|DIRTY"
export STUB_MERGEABLE_SEQ_COUNTER="$T_MC8/mergeable-seq.count"
: > "$STUB_MERGEABLE_SEQ_COUNTER"
export STUB_CI_CHECK_RUNS='[]'
export CI_MAX_WAIT_SECONDS=2
export CI_POLL_INTERVAL=1
OUT_MC8=$(run_script "$T_MC8" --pr 999 2>&1)
RC_MC8=$?
assert_exit "MC-8: exits 1" 1 "$RC_MC8"
assert_contains "MC-8: says it became conflicting during the wait" "became conflicting while waiting on CI" "$OUT_MC8"
assert_contains "MC-8: same conflict wording as everywhere else" "cause: this branch conflicts with its base" "$OUT_MC8"
assert_not_contains "MC-8: reported as a conflict, not as a timeout" "CI-status gate FAILED" "$OUT_MC8"
assert_not_contains "MC-8: no merge happened" "PR #999 merged." "$OUT_MC8"
AUDIT_MC8="$T_MC8/state/audit.jsonl"
if grep -q "branch conflicts with its base" "$AUDIT_MC8" 2>/dev/null; then
  pass "MC-8: audit row records the conflict as the reason"
else
  fail "MC-8: audit row does not name the conflict: $(cat "$AUDIT_MC8" 2>/dev/null)"
fi
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE_SEQ STUB_MERGEABLE_SEQ_COUNTER STUB_CI_CHECK_RUNS CI_MAX_WAIT_SECONDS CI_POLL_INTERVAL
rm -rf "$T_MC8"

# ── Test MC-9: the probe's own failure is not a refusal ─────────────────────
#    Fail-open on purpose. The probe only ever accelerates a refusal the merge
#    itself already produces (GitHub answers 405 on a conflicting merge), so a
#    probe that blocked on a transient API blip would add a brand-new way to
#    fail where there was previously only a slow one.
echo "Test MC-9: probe returns junk GitHub never emits — falls through, does not refuse"
T_MC9=$(mktemp -d)
setup_stubs "$T_MC9" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE="not-a-state|not-a-status"
OUT_MC9=$(run_script "$T_MC9" --pr 999 2>&1)
RC_MC9=$?
assert_exit "MC-9: exits 0 — merge proceeds on the normal gates" 0 "$RC_MC9"
assert_contains "MC-9: says the probe was inconclusive" "mergeability probe inconclusive" "$OUT_MC9"
assert_contains "MC-9: merge proceeded" "PR #999 merged." "$OUT_MC9"
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE
rm -rf "$T_MC9"

# ── Test MC-10: the bounded retry turns an async UNKNOWN into a real answer ──
#    Measured on the code plane (fulcrumaxe/fulcrumaxe) on 2026-09-06: a single
#    `gh pr list --json mergeable` over the three open PRs returned UNKNOWN for
#    two of them, and one `gh pr view --json mergeable` on each of those two
#    moments later returned MERGEABLE|CLEAN. Without the retry the probe would
#    fall through on exactly the freshly-pushed branches it exists for.
echo "Test MC-10: UNKNOWN then UNKNOWN then CONFLICTING — the retry gets the answer"
T_MC10=$(mktemp -d)
setup_stubs "$T_MC10" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE_SEQ="UNKNOWN|UNKNOWN;UNKNOWN|UNKNOWN;CONFLICTING|DIRTY"
export STUB_MERGEABLE_SEQ_COUNTER="$T_MC10/mergeable-seq.count"
: > "$STUB_MERGEABLE_SEQ_COUNTER"
export CI_MERGE_PROBE_ATTEMPTS=3
export CI_MERGE_PROBE_INTERVAL=0
OUT_MC10=$(run_script "$T_MC10" --pr 999 2>&1)
RC_MC10=$?
assert_exit "MC-10: exits 1" 1 "$RC_MC10"
assert_contains "MC-10: refused as a conflict on the third read" "conflicts with its base" "$OUT_MC10"
_MC10_READS=$(cat "$STUB_MERGEABLE_SEQ_COUNTER" 2>/dev/null || echo 0)
if [[ "${_MC10_READS:-0}" -eq 3 ]]; then
  pass "MC-10: exactly 3 mergeability reads — the retry ran and stopped when it had an answer"
else
  fail "MC-10: expected 3 mergeability reads, got ${_MC10_READS:-0}"
fi
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE_SEQ STUB_MERGEABLE_SEQ_COUNTER CI_MERGE_PROBE_ATTEMPTS CI_MERGE_PROBE_INTERVAL
rm -rf "$T_MC10"

# ── Test MC-11: the retry is bounded — a permanent UNKNOWN stops, not spins ──
echo "Test MC-11: permanently UNKNOWN — exactly CI_MERGE_PROBE_ATTEMPTS reads, then falls through"
T_MC11=$(mktemp -d)
setup_stubs "$T_MC11" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE_SEQ="UNKNOWN|UNKNOWN"
export STUB_MERGEABLE_SEQ_COUNTER="$T_MC11/mergeable-seq.count"
: > "$STUB_MERGEABLE_SEQ_COUNTER"
export CI_MERGE_PROBE_ATTEMPTS=3
export CI_MERGE_PROBE_INTERVAL=0
OUT_MC11=$(run_script "$T_MC11" --pr 999 2>&1)
RC_MC11=$?
assert_exit "MC-11: exits 0 — falls through to the normal gates" 0 "$RC_MC11"
assert_contains "MC-11: reports the reason it could not decide" "computed asynchronously" "$OUT_MC11"
_MC11_READS=$(cat "$STUB_MERGEABLE_SEQ_COUNTER" 2>/dev/null || echo 0)
if [[ "${_MC11_READS:-0}" -eq 3 ]]; then
  pass "MC-11: exactly 3 reads — bounded by CI_MERGE_PROBE_ATTEMPTS"
else
  fail "MC-11: expected exactly 3 reads, got ${_MC11_READS:-0}"
fi
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE_SEQ STUB_MERGEABLE_SEQ_COUNTER CI_MERGE_PROBE_ATTEMPTS CI_MERGE_PROBE_INTERVAL
rm -rf "$T_MC11"

# ── Test MC-12: the timeout re-probe names its trigger honestly too ─────────
#    The re-probe on the CI-wait timeout path prints its own message, so it is
#    a second place the trigger can be hardcoded. Same shape as MC-4: GitHub
#    answers UNKNOWN|DIRTY on the re-read, and the message must say which field
#    fired rather than naming a value that was never returned.
echo "Test MC-12: timeout re-probe fires on DIRTY — message names mergeStateStatus, not mergeable"
T_MC12=$(mktemp -d)
setup_stubs "$T_MC12" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_MERGEABLE_SEQ="MERGEABLE|CLEAN;UNKNOWN|DIRTY"
export STUB_MERGEABLE_SEQ_COUNTER="$T_MC12/mergeable-seq.count"
: > "$STUB_MERGEABLE_SEQ_COUNTER"
export STUB_CI_CHECK_RUNS='[]'
export CI_MAX_WAIT_SECONDS=2
export CI_POLL_INTERVAL=1
OUT_MC12=$(run_script "$T_MC12" --pr 999 2>&1)
RC_MC12=$?
assert_exit "MC-12: exits 1" 1 "$RC_MC12"
assert_contains "MC-12: still reports the during-wait conflict" "became conflicting while waiting on CI" "$OUT_MC12"
assert_contains "MC-12: names mergeStateStatus as the trigger" "via mergeStateStatus=DIRTY" "$OUT_MC12"
assert_not_contains "MC-12: never claims GitHub returned mergeable=CONFLICTING" "mergeable=CONFLICTING" "$OUT_MC12"
assert_contains "MC-12: reports the mergeable value actually observed" "observed mergeable=UNKNOWN" "$OUT_MC12"
assert_not_contains "MC-12: no merge happened" "PR #999 merged." "$OUT_MC12"
unset TWO_GATE_PR_BODY_999 STUB_MERGEABLE_SEQ STUB_MERGEABLE_SEQ_COUNTER STUB_CI_CHECK_RUNS CI_MAX_WAIT_SECONDS CI_POLL_INTERVAL
rm -rf "$T_MC12"

# ─────────────────────────────────────────────────────────────────────────────
# D#2455 — the merge-gate label check. Nine labels gated the loop path and none
# of them gated this one, so a PR held back on purpose merged by hand.
#
# Every case below iterates the arrays sourced from
# scripts/lib/merge-gate-labels.sh at the top of this file. No label string is
# written out here, and no count is asserted: `assert 8 labels` would keep
# passing the day someone adds a ninth to the shared source and the wrapper
# stops enforcing it, which is precisely the failure being guarded against.
# ─────────────────────────────────────────────────────────────────────────────

echo "Test MGL-1: every NACK label in the shared set refuses the merge, naming itself"
MGL_ENFORCED=()
for MGL_LABEL in "${MERGE_GATE_NACK_LABELS[@]}"; do
  T_MGL=$(mktemp -d)
  setup_stubs "$T_MGL" 0
  export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
  # The stub appends code-review-passed, so the only thing wrong with this PR
  # is the NACK label — a refusal here cannot be the required-pass gate firing,
  # and a NACK is proved to outrank a satisfied pass label.
  export STUB_PR_LABELS="$MGL_LABEL"
  OUT_MGL=$(run_script "$T_MGL" --pr 999 2>&1)
  RC_MGL=$?
  assert_exit "MGL-1[$MGL_LABEL]: exits 1" 1 "$RC_MGL"
  assert_contains "MGL-1[$MGL_LABEL]: refusal names the label" "carries the '$MGL_LABEL' label" "$OUT_MGL"
  assert_not_contains "MGL-1[$MGL_LABEL]: no merge happened" "PR #999 merged." "$OUT_MGL"
  # Refused before any other gate: the label read is the stub's one silent
  # branch, so a single GH_ARGS line here would mean a later gate ran first.
  assert_not_contains "MGL-1[$MGL_LABEL]: refused before every other gate" "GH_ARGS:" "$OUT_MGL"
  if [[ "$RC_MGL" -eq 1 ]] && echo "$OUT_MGL" | grep -qF "carries the '$MGL_LABEL' label"; then
    MGL_ENFORCED+=("$MGL_LABEL")
  fi
  unset TWO_GATE_PR_BODY_999 STUB_PR_LABELS
  rm -rf "$T_MGL"
done

# ── Test MGL-2: the set the wrapper enforces equals the shared definition ─────
#    Both sides are runtime values: the left is what the wrapper actually
#    refused on above (observed by running it), the right is the array the loop
#    path iterates. tests/test_merge_gate.sh's MG-EQ makes the same comparison
#    for the loop path against the same array, so the two paths are equal to
#    each other by both being equal to the one definition — and neither test
#    restates a label to check it.
echo "Test MGL-2: enforced set == shared NACK definition (no restated list, no count)"
MGL_WANT=$(printf '%s\n' "${MERGE_GATE_NACK_LABELS[@]}" | sort)
MGL_GOT=$(printf '%s\n' ${MGL_ENFORCED[@]+"${MGL_ENFORCED[@]}"} | sort)
if [[ "$MGL_WANT" == "$MGL_GOT" ]]; then
  pass "MGL-2: merge-and-hook.sh enforces exactly the shared NACK set"
else
  fail "MGL-2: enforced set differs from the shared NACK set — missing: $(comm -23 <(echo "$MGL_WANT") <(echo "$MGL_GOT") | tr '\n' ' ')"
fi

# ── Test MGL-3: a missing required pass label refuses the merge (D#2452) ──────
#    Opposite polarity to MGL-1: not a blocking label present, a required label
#    absent. STUB_PR_LABELS_EXACT stops the stub appending the default, so the
#    PR genuinely lacks the label under test.
echo "Test MGL-3: every required pass label, when absent, refuses the merge"
for MGL_LABEL in "${MERGE_GATE_REQUIRED_PASS_LABELS[@]}"; do
  T_MGL3=$(mktemp -d)
  setup_stubs "$T_MGL3" 0
  export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
  export STUB_PR_LABELS_EXACT=1
  export STUB_PR_LABELS="$(printf '%s\n' "${MERGE_GATE_REQUIRED_PASS_LABELS[@]}" | grep -vxF -- "$MGL_LABEL" || true)"
  OUT_MGL3=$(run_script "$T_MGL3" --pr 999 2>&1)
  RC_MGL3=$?
  assert_exit "MGL-3[$MGL_LABEL]: exits 1" 1 "$RC_MGL3"
  assert_contains "MGL-3[$MGL_LABEL]: refusal names the missing label" "does not carry the required '$MGL_LABEL' label" "$OUT_MGL3"
  assert_not_contains "MGL-3[$MGL_LABEL]: no merge happened" "PR #999 merged." "$OUT_MGL3"
  unset TWO_GATE_PR_BODY_999 STUB_PR_LABELS STUB_PR_LABELS_EXACT
  rm -rf "$T_MGL3"
done

# ── Test MGL-4: a PR that satisfies the shared set still merges ───────────────
#    The gate has to be able to say yes. Without this, a wrapper that refused
#    every PR unconditionally would pass MGL-1 and MGL-3.
echo "Test MGL-4: required labels present, no NACK label — merge proceeds"
T_MGL4=$(mktemp -d)
setup_stubs "$T_MGL4" 0
export TWO_GATE_PR_BODY_999="Gate 1: PASS\nGate 2: PASS"
export STUB_PR_LABELS_EXACT=1
export STUB_PR_LABELS="$(printf '%s\n' "${MERGE_GATE_REQUIRED_PASS_LABELS[@]}")"
OUT_MGL4=$(run_script "$T_MGL4" --pr 999 2>&1)
RC_MGL4=$?
assert_exit "MGL-4: exits 0" 0 "$RC_MGL4"
assert_contains "MGL-4: gate reports itself satisfied" "merge-gate labels OK for PR #999" "$OUT_MGL4"
assert_contains "MGL-4: merge happened" "PR #999 merged." "$OUT_MGL4"
unset TWO_GATE_PR_BODY_999 STUB_PR_LABELS STUB_PR_LABELS_EXACT
rm -rf "$T_MGL4"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
