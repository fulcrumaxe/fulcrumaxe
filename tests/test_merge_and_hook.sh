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
if [[ "$ARGS" == *"--json labels"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  printf '%s\n' "${STUB_PR_LABELS:-}"
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

# `gh pr view <PR> --repo ... --json files --jq '.files[].path'` (D#1614 provenance gate)
if [[ "$ARGS" == *"--json files"* ]]; then
  echo "GH_ARGS: $ARGS" >&2
  printf '%s\n' "${STUB_PR_FILES:-}"
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
    printf '%s' '[{"name":"tui","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"dashboard","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"ts-backend","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"backend (import-smoke)","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""}]'
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
# and only the non-matrix job appears.
# ═══════════════════════════════════════════════════════════════════════════
MISSING_MATRIX='[{"name":"backend (import-smoke)","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""}]'

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
