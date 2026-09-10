#!/usr/bin/env bash
# tests/test_assert_not_contains_dash_needle.sh — regression test for D#2369.
#
# `assert_not_contains` (and its `_assert_not_contains` cousins) call
# `grep -qF "$needle"` with no `--` separator. A needle beginning with `-`
# gets parsed by grep as an option, grep exits non-zero (usage error, not
# "absent"), and the helper reads that as "the forbidden string is not
# present" — so it PASSES even when the string IS present. That is the
# defect: a negative assertion that reports success on the exact input that
# should make it fail.
#
# This suite extracts each helper's actual function body straight out of its
# own file (via brace-matching, not a copy pasted here) and calls it with a
# leading-dash needle that genuinely IS present in the haystack. A correctly
# behaving helper must report failure. Before the fix, all 18 report success
# instead — vacuously.
#
# GATE 1 rule (D#2369 Spec item 1): this suite must not use
# assert_not_contains/_assert_not_contains to check anything about itself —
# that would be circular, checking the defect with the defect. Every
# assertion below is a pure-bash string test or an explicit rc comparison.
#
# Run: bash tests/test_assert_not_contains_dash_needle.sh
# Expects (post-fix): all 18 cases PASS, exit 0.
# Expects (pre-fix):  all 18 cases FAIL, exit 1 — this is what proves the bug.

set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

PASS=0
FAIL=0

# Extract a function's full body (definition line through its matching closing
# brace) from a source file, by counting braces. Robust to balanced
# ${var} / ${var:-x} parameter-expansion braces inside the body, since those
# always net to zero within a line.
extract_func() {
  local file="$1" funcname="$2"
  awk -v fn="$funcname" '
    $0 ~ "^" fn "[[:space:]]*\\(\\)" { started = 1 }
    started {
      print
      o = gsub(/\{/, "{")
      c = gsub(/\}/, "}")
      depth += o - c
      if (depth == 0) exit
    }
  ' "$file"
}

# Run one mutation case: source just the extracted function (plus minimal
# stub helpers it may call) into a clean subshell, invoke it with a
# leading-dash needle that IS present in the haystack, and check — via plain
# bash string matching, not the helper under test — whether it reported
# failure (correct) or success (buggy/vacuous).
#
# args: label file funcname argorder
#   argorder describes positional args as the target function expects them:
#     LNH = label, needle, haystack
#     LHN = label, haystack, needle
#     HNL = haystack, needle, label   (the reversed one-liner family)
#     FPL = file,   pattern, label    (test_loop_bootstrap_extended.sh: reads
#                                       a file, pattern stays a regex on purpose)
run_case() {
  local label="$1" file="$2" funcname="$3" argorder="$4"
  local needle="--delete-branch"
  local haystack="before --delete-branch after"
  local body
  body="$(extract_func "$file" "$funcname")"

  if [ -z "$body" ]; then
    echo "  FAIL: $label — could not extract $funcname from $file"
    FAIL=$((FAIL + 1))
    return
  fi

  local out rc call
  case "$argorder" in
    LNH) call="$funcname \"dash-needle-case\" \"$needle\" \"$haystack\"" ;;
    LHN) call="$funcname \"dash-needle-case\" \"$haystack\" \"$needle\"" ;;
    HNL) call="$funcname \"$haystack\" \"$needle\" \"dash-needle-case\"" ;;
    FPL)
      local tmpfile
      tmpfile="$(mktemp)"
      printf '%s\n' "$haystack" > "$tmpfile"
      call="$funcname \"$tmpfile\" \"$needle\" \"dash-needle-case\""
      ;;
    *)
      echo "  FAIL: $label — unknown argorder $argorder"
      FAIL=$((FAIL + 1))
      return
      ;;
  esac

  # Stub every side-function any of the 18 variants might call. None of these
  # is assert_not_contains itself — they're just PASS/FAIL recorders so the
  # extracted body runs standalone without its parent file's harness.
  out="$(bash -c "
    PASS=0; FAIL=0
    pass() { echo \"STUB_PASS \$*\"; }
    fail() { echo \"STUB_FAIL \$*\"; }
    ok() { echo \"STUB_PASS \$*\"; }
    fail_test() { echo \"STUB_FAIL \$*\"; }
    _pass() { echo \"STUB_PASS \$*\"; }
    _fail() { echo \"STUB_FAIL \$*\"; }
    $body
    $call
    echo \"COUNTERS PASS=\$PASS FAIL=\$FAIL\"
  " 2>&1)"
  rc=$?

  if [ "$argorder" = "FPL" ]; then
    rm -f "$tmpfile"
  fi

  # A correct helper must show it detected the (present) forbidden substring:
  # either a stub/echo FAIL marker, or FAIL counter > 0. A buggy helper shows
  # only PASS markers / FAIL=0 because grep errored out and was read as
  # "absent". Pure bash test, not the helper under test.
  if [[ "$out" == *STUB_FAIL* ]] || [[ "$out" == *"  FAIL:"* ]] || [[ "$out" != *"FAIL=0"* ]]; then
    echo "  PASS: $label — correctly reported failure on a present dash-needle"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label — reported success even though the needle IS present (vacuous)"
    echo "        raw output: $out"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== D#2369 regression: assert_not_contains must not pass vacuously on a dash-prefixed needle ==="

run_case "test_ci_status_check.sh"                    tests/test_ci_status_check.sh                    assert_not_contains  LNH
run_case "test_loop_bootstrap_extended.sh"             tests/test_loop_bootstrap_extended.sh            assert_not_contains  FPL
run_case "test_loop_phased_step5.sh"                   tests/test_loop_phased_step5.sh                  assert_not_contains  LNH
run_case "test_merge_gate.sh"                          tests/test_merge_gate.sh                         assert_not_contains  LNH
run_case "test_post_agent_hook_recovery.sh"            tests/test_post_agent_hook_recovery.sh           assert_not_contains  LHN
run_case "test_post_agent_hook_self_observe.sh"        tests/test_post_agent_hook_self_observe.sh       assert_not_contains  LHN
run_case "test_post_merge_hook_browser_queue.sh"       tests/test_post_merge_hook_browser_queue.sh      assert_not_contains  LHN
run_case "test_reaper_auto_triage.sh"                  tests/test_reaper_auto_triage.sh                 _assert_not_contains HNL
run_case "test_reaper_clean_generated_wiki.sh"         tests/test_reaper_clean_generated_wiki.sh        _assert_not_contains HNL
run_case "test_reaper_dryrun_parity.sh"                tests/test_reaper_dryrun_parity.sh               _assert_not_contains HNL
run_case "test_reaper_enumeration_report.sh"           tests/test_reaper_enumeration_report.sh          _assert_not_contains HNL
run_case "test_reaper_git_tracked_removal.sh"          tests/test_reaper_git_tracked_removal.sh         _assert_not_contains HNL
run_case "test_reaper_removal_cap.sh"                  tests/test_reaper_removal_cap.sh                 _assert_not_contains HNL
run_case "test_reaper_safety_gates.sh"                 tests/test_reaper_safety_gates.sh                _assert_not_contains HNL
run_case "test_run_analyst_sweep.sh"                   tests/test_run_analyst_sweep.sh                  assert_not_contains  LNH
run_case "test_self_observe_transcript_discovery.sh"   tests/test_self_observe_transcript_discovery.sh  assert_not_contains  LHN
run_case "test_start_the_day_selfheal.sh"              tests/test_start_the_day_selfheal.sh             assert_not_contains  LNH
run_case "test_triage_orphan_diffs.sh"                 tests/test_triage_orphan_diffs.sh                _assert_not_contains HNL

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [ "$FAIL" -ne 0 ]; then
  exit 1
fi
exit 0
