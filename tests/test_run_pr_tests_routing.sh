#!/usr/bin/env bash
# tests/test_run_pr_tests_routing.sh — unit tests for the routing manifest
# and bounded pytest arm added to scripts/run-pr-tests.sh (D#2132 PR-a).
#
# `gh` never makes a real GitHub call; a stubbed `python3` intercepts
# `-m pytest` and delegates everything else (JSON escaping, flaky_sentinel)
# to the real interpreter — the stub-a-command-on-PATH idiom used by
# tests/test_spawn_agent_file_scope.sh and tests/test_sweep_stuck_prs.sh.
#
# Covers PR-a Spec items 1-3: (1) a PR whose files match no suite gets
# suite:null for all of them plus a named stderr line; (2) the pytest
# manifest entry carries timed_out:false and its child actually receives a
# non-empty AUTONOMOUS_TEAM_STATE_DIR; (3) with the bound overridden low, a
# hung pytest is reported timed_out:true and the aggregate exit is non-zero.
#
# Extended for PR-b Spec items 7-10: (7) a changed tests/*.sh suite maps to
# itself in routing and actually runs; (8) the two suites the RUN_ORPHAN_TRIAGE
# arm used to shadow now self-map instead of falling through to pytest; (9) a
# hanging auto-routed suite is killed by its own bound and reported
# timed_out:true; (10) a denylisted suite is reported skipped with a reason,
# never silently absent.
#
# Extended again for D#2177 (test_d2177_*, a separate item enumeration from
# PR-a/PR-b's above — do not confuse test_item2 with test_d2177_item2, they
# cover different acceptance items): the first case arm in the changed-file
# loop used to match `tests/*` unconditionally, so a changed tests/*.sh also
# set RUN_PYTHON=true and triggered a 900s pytest sweep alongside its own bash
# suite -- and because the manifest was only emitted at the end, a PR that
# also hit the pytest arm's bound discarded every result, including a bash
# suite that had already passed. test_d2177_item1-3 cover the narrowed
# routing (bash suites no longer drag in pytest; Python routing, including
# nested tests/**/*.py, and the denylist are unaffected). test_d2177_item4-6
# cover the termination trap: a mid-run TERM (as sent by an enclosing
# `timeout` without --foreground, run_script_bounded's exact shape) still
# emits whatever suites had already completed, marked partial:true, while a
# normal completion never sets that field and still emits exactly one JSON
# object. test_d2177_item7 and item10 are regression guards (dedup, the
# non-goal comment).
#
# Usage: bash tests/test_run_pr_tests_routing.sh — exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REAL_PYTHON3="$(command -v python3)"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

# Throwaway copy of the script with a stubbed gh + python3 on PATH.
#
# flaky_sentinel isolation: run_suite() calls `python3 backend/flaky_sentinel.py`
# via a bare relative path, which resolves against the process's real cwd (the
# actual repo), not $TEST_DIR — a stub file dropped in $TEST_DIR/backend/ is
# never reached. flaky_sentinel.py itself supports FLAKY_HISTORY_PATH as a
# test-isolation override (see backend/flaky_sentinel.py:_history_path), so
# run_script() exports that instead of trying to shadow the script. Do not
# reintroduce a $TEST_DIR/backend/flaky_sentinel.py stub — it would be dead
# code that looks like isolation but isn't.
setup() {
  TEST_DIR=$(mktemp -d)
  mkdir -p "$TEST_DIR/scripts/lib" "$TEST_DIR/tests" "$TEST_DIR/bin"
  cp "$REPO_ROOT/scripts/run-pr-tests.sh" "$TEST_DIR/scripts/"
  cp "$REPO_ROOT/scripts/lib/repo-resolve.sh" "$REPO_ROOT/scripts/lib/worktree-ground-check.sh" "$TEST_DIR/scripts/lib/"
  git init -q "$TEST_DIR"  # run_suite's ground check needs $REPO_ROOT to resolve as a git tree

  # gh stub: `pr diff --name-only` and the `pr view --json files` fallback
  # both return $GH_FILES (newline-separated), regardless of PR number.
  cat > "$TEST_DIR/bin/gh" <<'GHEOF'
#!/usr/bin/env bash
if [[ "$*" == *"pr diff"* || "$*" == *"pr view"* ]]; then
  printf '%s\n' "$GH_FILES"
  exit 0
fi
echo "[]"
GHEOF
  chmod +x "$TEST_DIR/bin/gh"

  # python3 stub: intercepts `-m pytest`, delegates the rest to the real one.
  cat > "$TEST_DIR/bin/python3" <<PYEOF
#!/usr/bin/env bash
if [ "\$1" = "-m" ] && [ "\$2" = "pytest" ]; then
  printf '%s' "\${AUTONOMOUS_TEAM_STATE_DIR:-}" > "\$FAKE_PYTEST_ENV_CAPTURE"
  if [ "\${FAKE_PYTEST_FORK_SLEEP:-0}" != "0" ]; then
    # Reproduces a suite that forks its own child (a worker process, a
    # backgrounded step) and blocks on it. D#2177's forking-grandchild
    # regression only shows up when the bound has to reach *this* process,
    # one level below the direct pytest process timeout supervises.
    sleep "\$FAKE_PYTEST_FORK_SLEEP" &
    wait
  fi
  [ "\${FAKE_PYTEST_SLEEP:-0}" != "0" ] && sleep "\$FAKE_PYTEST_SLEEP"
  exit "\${FAKE_PYTEST_EXIT:-0}"
fi
exec "$REAL_PYTHON3" "\$@"
PYEOF
  chmod +x "$TEST_DIR/bin/python3"
}

teardown() { rm -rf "$TEST_DIR"; }

# Runs the stubbed script for PR number $1; stderr goes to $TEST_DIR/stderr.log.
# Reads GH_FILES / FAKE_PYTEST_* / RUN_PR_TESTS_PYTEST_TIMEOUT from the
# caller's shell — each test sets only what it needs.
# FLAKY_HISTORY_PATH is the isolation boundary for the real flaky_sentinel.py
# that run_suite() actually invokes (see the note in setup()) — without it,
# every run here would append to production ~/.autonomous-forever-state/flaky-history.jsonl.
_run_script_env() {
  export PATH="$TEST_DIR/bin:$PATH" AUTONOMOUS_TEAM_REPO="test/repo"
  export GH_FILES="${GH_FILES:-}" FAKE_PYTEST_ENV_CAPTURE="$TEST_DIR/pytest_env_capture"
  export FAKE_PYTEST_SLEEP="${FAKE_PYTEST_SLEEP:-0}" FAKE_PYTEST_EXIT="${FAKE_PYTEST_EXIT:-0}"
  export FAKE_PYTEST_FORK_SLEEP="${FAKE_PYTEST_FORK_SLEEP:-0}"
  export RUN_PR_TESTS_PYTEST_TIMEOUT="${RUN_PR_TESTS_PYTEST_TIMEOUT:-}"
  export RUN_PR_TESTS_BASH_TIMEOUT="${RUN_PR_TESTS_BASH_TIMEOUT:-}"
  export FLAKY_HISTORY_PATH="$TEST_DIR/flaky-history.jsonl"
}

run_script() {
  ( _run_script_env; bash "$TEST_DIR/scripts/run-pr-tests.sh" "$1" ) 2>"$TEST_DIR/stderr.log"
}

# Same as run_script, but the whole script runs under an enclosing `timeout`
# without --foreground -- i.e. the exact real-world shape that produced the
# reviewer-measured "exit=124, 0 bytes" case (D#2177 item 4/5). $1 = bound
# seconds, $2 = PR number.
#
# --kill-after=5s, not 3s: this must match the repo-wide bounded-run
# convention (`timeout --kill-after=5s <seconds> <cmd>`, see
# scripts/lib/working-principles.sh, hooks/background_rules.py, and
# run-pr-tests.sh's own internal per-suite bound at run_suite()) rather than
# inventing a shorter one here. Measured under load (30 concurrent CPU-bound
# workers, 1-min loadavg 20-98, 12 cores): at 3s, 3/8 repeats of this exact
# harness failed item5 with a 0-byte read (SIGKILL beat the trap's
# _kill_tree + manifest emission); at 5s, 8/8 passed under the same
# generated load. 3s wasn't a deliberate choice, just an unmatched outlier
# against the rest of the codebase's grace margin.
run_script_bounded() {
  local bound="$1" pr="$2"
  ( _run_script_env; timeout --kill-after=5s "$bound" bash "$TEST_DIR/scripts/run-pr-tests.sh" "$pr" ) 2>"$TEST_DIR/stderr.log"
}

# Real production flaky-history.jsonl, resolved the same way flaky_sentinel.py
# resolves it (STATE_DIR, absent any FLAKY_HISTORY_PATH override).
REAL_FLAKY_HISTORY="$("$REAL_PYTHON3" -c 'from backend import state_paths; print(state_paths.STATE_DIR / "flaky-history.jsonl")' 2>/dev/null || echo "")"
real_flaky_history_rows() {
  [ -n "$REAL_FLAKY_HISTORY" ] && [ -f "$REAL_FLAKY_HISTORY" ] && wc -l < "$REAL_FLAKY_HISTORY" || echo 0
}

# $1 = stdout json, $2 = field name -> prints that field of the first
# tests_run entry whose command mentions pytest.
pytest_entry_field() {
  printf '%s' "$1" | "$REAL_PYTHON3" -c "
import json, sys
d = json.load(sys.stdin)
entry = next(e for e in d['tests_run'] if 'pytest' in e['command'])
print(entry.get('$2'))
"
}

# A generic bash-suite fixture prints its own output first; take the
# manifest from the last '{"routing"' onward rather than assuming stdout's
# final line is clean JSON (a real suite's last line isn't always
# newline-terminated, so the manifest can land concatenated after it).
manifest_json() {
  printf '%s' "$1" | "$REAL_PYTHON3" -c '
import sys
s = sys.stdin.read()
idx = s.rfind("{\"routing\"")
print(s[idx:] if idx >= 0 else "")
'
}

# $1 = stdout, $2 = file, $3 = field -> that field of routing[file], or
# __MISSING__ if the file has no routing entry.
routing_field() {
  manifest_json "$1" | "$REAL_PYTHON3" -c "
import json, sys
d = json.load(sys.stdin)
entry = next((e for e in d['routing'] if e['file'] == '$2'), None)
print(entry.get('$3') if entry is not None else '__MISSING__')
"
}

# $1 = stdout, $2 = substring of a tests_run command, $3 = field -> that
# field of the first matching entry, or __MISSING__ if none matched.
tests_run_field() {
  manifest_json "$1" | "$REAL_PYTHON3" -c "
import json, sys
d = json.load(sys.stdin)
entry = next((e for e in d['tests_run'] if '$2' in e['command']), None)
print(entry.get('$3') if entry is not None else '__MISSING__')
"
}

# $1 = stdout, $2 = substring of a tests_run command -> count of tests_run
# entries whose command contains it (dedup guard, item 7).
tests_run_count() {
  manifest_json "$1" | "$REAL_PYTHON3" -c "
import json, sys
d = json.load(sys.stdin)
print(sum(1 for e in d['tests_run'] if '$2' in e['command']))
"
}

# $1 = full raw stdout -> 'ok' iff it parses as exactly one JSON object with
# nothing after it (item 6): decode from the first '{\"routing\"' occurrence
# and confirm the decoder consumes the rest of the string.
assert_single_json_object() {
  printf '%s' "$1" | "$REAL_PYTHON3" -c '
import json, sys
s = sys.stdin.read()
idx = s.find("{\"routing\"")
assert idx >= 0, "no manifest found in stdout"
decoder = json.JSONDecoder()
obj, end = decoder.raw_decode(s, idx)
trailing = s[end:]
assert trailing.strip() == "", f"trailing content after JSON object: {trailing!r}"
print("ok")
'
}

test_item1_unrouted_files_are_null() {
  setup
  # PR #2082's real file list (measured via `gh pr view 2082 --json files`) —
  # five scripts/** files matching none of run-pr-tests.sh's case arms.
  GH_FILES=$'scripts/bootstrap-github-labels.sh\nscripts/coldstart-project.sh\nscripts/coldstart.sh\nscripts/engine-sync/drift-check.sh\nscripts/set-ci-kill-switch.sh'

  local out rc
  out=$(run_script 2082); rc=$?
  if [ "$rc" -ne 0 ]; then fail "item1: exit code 0 (nothing routed)" "got $rc"
  else pass "item1: exit code 0 (nothing routed)"; fi

  local check
  check=$(printf '%s' "$out" | "$REAL_PYTHON3" -c '
import json, sys
d = json.load(sys.stdin)
routing = d["routing"]
assert len(routing) == 5, f"expected 5 routing entries, got {len(routing)}: {routing}"
assert all(e["suite"] is None for e in routing), routing
print("ok")
' 2>&1)
  if [ "$check" = "ok" ]; then pass "item1: routing has 5 entries, all suite:null"
  else fail "item1: routing has 5 entries, all suite:null" "$check"; fi

  local err
  err=$(cat "$TEST_DIR/stderr.log")
  if echo "$err" | grep -q "bootstrap-github-labels.sh" && echo "$err" | grep -q "set-ci-kill-switch.sh"; then
    pass "item1: stderr names the unrouted files"
  else
    fail "item1: stderr names the unrouted files" "stderr was: $err"
  fi
  teardown
}

test_item2_pytest_state_dir_and_no_timeout() {
  setup
  GH_FILES="backend/foo.py"; FAKE_PYTEST_SLEEP=0; FAKE_PYTEST_EXIT=0

  local out
  out=$(run_script 4242)

  local captured
  captured=$(cat "$TEST_DIR/pytest_env_capture" 2>/dev/null || echo "")
  if [ -n "$captured" ]; then
    pass "item2: AUTONOMOUS_TEAM_STATE_DIR exported non-empty to the pytest child"
  else
    fail "item2: AUTONOMOUS_TEAM_STATE_DIR exported non-empty to the pytest child" "captured value was empty"
  fi

  local timed_out_val
  timed_out_val=$(pytest_entry_field "$out" "timed_out")
  if [ "$timed_out_val" = "False" ]; then pass "item2: pytest manifest entry carries timed_out:false"
  else fail "item2: pytest manifest entry carries timed_out:false" "got: $timed_out_val"; fi

  # run_suite() invokes flaky_sentinel.py on every call, including this one —
  # confirm it landed in the scratch file, not production (this is the
  # positive half of the isolation proof; the negative half — real state
  # untouched across the whole suite — is checked once at the very end).
  local scratch_rows
  scratch_rows=$([ -f "$TEST_DIR/flaky-history.jsonl" ] && wc -l < "$TEST_DIR/flaky-history.jsonl" || echo 0)
  if [ "$scratch_rows" -ge 1 ]; then
    pass "item2: flaky_sentinel wrote to the scratch FLAKY_HISTORY_PATH, not production"
  else
    fail "item2: flaky_sentinel wrote to the scratch FLAKY_HISTORY_PATH, not production" "\$TEST_DIR/flaky-history.jsonl has $scratch_rows rows"
  fi
  teardown
}

test_item3_pytest_bound_reports_timeout() {
  setup
  # local: RUN_PR_TESTS_PYTEST_TIMEOUT and FAKE_PYTEST_SLEEP otherwise leak
  # forward as ordinary globals into every test function that runs after
  # this one and doesn't override them itself -- caught when it silently
  # bounded test_d2177_item4/5's pytest arm at 1s instead of the 900s
  # default their comments assume.
  local FAKE_PYTEST_SLEEP RUN_PR_TESTS_PYTEST_TIMEOUT
  GH_FILES="backend/foo.py"; FAKE_PYTEST_SLEEP=3; FAKE_PYTEST_EXIT=0
  RUN_PR_TESTS_PYTEST_TIMEOUT=1

  local out rc
  out=$(run_script 4343); rc=$?
  if [ "$rc" -ne 0 ]; then pass "item3: aggregate exit is non-zero when the bound fires"
  else fail "item3: aggregate exit is non-zero when the bound fires" "got exit 0"; fi

  local timed_out_val
  timed_out_val=$(pytest_entry_field "$out" "timed_out")
  if [ "$timed_out_val" = "True" ]; then pass "item3: pytest manifest entry carries timed_out:true"
  else fail "item3: pytest manifest entry carries timed_out:true" "got: $timed_out_val"; fi
  teardown
}

# PR-b items 7-10: the generic tests/*.sh -> itself routing rule, its bound,
# and the denylist. python3 is still the stub here (setup() puts it first on
# PATH) so these never touch a real pytest run — only the fixture bash
# suites dropped into $TEST_DIR/tests/ actually execute.

test_item7_bash_suite_maps_to_itself_and_runs() {
  setup
  cat > "$TEST_DIR/tests/test_merge_gate.sh" <<'FIXEOF'
#!/usr/bin/env bash
echo "fixture merge gate"
exit 0
FIXEOF
  GH_FILES="tests/test_merge_gate.sh"

  local out
  out=$(run_script 7001)

  local suite
  suite=$(routing_field "$out" "tests/test_merge_gate.sh" "suite")
  if [ "$suite" = "bash tests/test_merge_gate.sh" ]; then
    pass "item7: tests/test_merge_gate.sh maps to itself in routing"
  else
    fail "item7: tests/test_merge_gate.sh maps to itself in routing" "got: $suite"
  fi

  local cmd_exit
  cmd_exit=$(tests_run_field "$out" "bash tests/test_merge_gate.sh" "exit_code")
  if [ "$cmd_exit" = "0" ]; then
    pass "item7: bash tests/test_merge_gate.sh actually appears in tests_run"
  else
    fail "item7: bash tests/test_merge_gate.sh actually appears in tests_run" "got exit_code: $cmd_exit"
  fi
  teardown
}

test_item8_orphan_triage_suites_self_map() {
  setup
  printf '#!/usr/bin/env bash\nexit 0\n' > "$TEST_DIR/tests/test_triage_orphan_diffs.sh"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$TEST_DIR/tests/test_worktree_self_exclusion.sh"
  GH_FILES=$'tests/test_triage_orphan_diffs.sh\ntests/test_worktree_self_exclusion.sh'

  local out s1 s2
  out=$(run_script 7002)
  s1=$(routing_field "$out" "tests/test_triage_orphan_diffs.sh" "suite")
  s2=$(routing_field "$out" "tests/test_worktree_self_exclusion.sh" "suite")

  if [ "$s1" = "bash tests/test_triage_orphan_diffs.sh" ]; then
    pass "item8: test_triage_orphan_diffs.sh maps to itself, not the pytest arm"
  else
    fail "item8: test_triage_orphan_diffs.sh maps to itself, not the pytest arm" "got: $s1"
  fi
  if [ "$s2" = "bash tests/test_worktree_self_exclusion.sh" ]; then
    pass "item8: test_worktree_self_exclusion.sh maps to itself, not the pytest arm"
  else
    fail "item8: test_worktree_self_exclusion.sh maps to itself, not the pytest arm" "got: $s2"
  fi
  teardown
}

test_item9_generic_bash_suite_is_bounded() {
  setup
  printf '#!/usr/bin/env bash\nsleep 30\n' > "$TEST_DIR/tests/test_fixture_hang.sh"
  GH_FILES="tests/test_fixture_hang.sh"
  RUN_PR_TESTS_BASH_TIMEOUT=1

  local out rc
  out=$(run_script 7003); rc=$?

  local timed_out
  timed_out=$(tests_run_field "$out" "bash tests/test_fixture_hang.sh" "timed_out")
  if [ "$timed_out" = "True" ]; then
    pass "item9: a hanging auto-routed suite is reported timed_out:true"
  else
    fail "item9: a hanging auto-routed suite is reported timed_out:true" "got: $timed_out"
  fi
  if [ "$rc" -ne 0 ]; then
    pass "item9: aggregate exit is non-zero when a generic suite is killed by its bound"
  else
    fail "item9: aggregate exit is non-zero when a generic suite is killed by its bound" "got exit 0"
  fi
  teardown
}

test_item10_denylisted_suite_reported_skipped_with_reason() {
  setup
  # Real denylist entry baked into the copied script — no fixture needed,
  # routing only matches on the changed-file string.
  GH_FILES="tests/test_post_merge_hook_unmerged_paths.sh"

  local out skipped reason
  out=$(run_script 7004)
  skipped=$(routing_field "$out" "tests/test_post_merge_hook_unmerged_paths.sh" "skipped")
  reason=$(routing_field "$out" "tests/test_post_merge_hook_unmerged_paths.sh" "reason")

  if [ "$skipped" = "True" ]; then
    pass "item10: a denylisted suite is reported skipped in routing, not silently absent"
  else
    fail "item10: a denylisted suite is reported skipped in routing, not silently absent" "got skipped=$skipped"
  fi
  if [ -n "$reason" ] && [ "$reason" != "__MISSING__" ] && [ "$reason" != "None" ]; then
    pass "item10: the skipped entry carries a non-empty reason"
  else
    fail "item10: the skipped entry carries a non-empty reason" "got reason=$reason"
  fi
  teardown
}

# D#2177 items 1-7: the tests/*.sh -> pytest double-routing fix, and the
# termination trap that keeps a mid-run kill from discarding completed
# results. Named test_d2177_* (not test_item*) to stay distinct from the
# PR-a/PR-b item numbers above, which are a different enumeration.

test_d2177_item1_bash_suite_no_longer_triggers_pytest() {
  setup
  cat > "$TEST_DIR/tests/test_merge_gate.sh" <<'FIXEOF'
#!/usr/bin/env bash
echo "fixture merge gate"
exit 0
FIXEOF
  GH_FILES="tests/test_merge_gate.sh"

  local out suite pytest_count
  out=$(run_script 8001)
  suite=$(routing_field "$out" "tests/test_merge_gate.sh" "suite")
  if [ "$suite" = "bash tests/test_merge_gate.sh" ]; then
    pass "d2177 item1: tests/test_merge_gate.sh still maps to its own bash suite"
  else
    fail "d2177 item1: tests/test_merge_gate.sh still maps to its own bash suite" "got: $suite"
  fi

  pytest_count=$(tests_run_count "$out" "pytest")
  if [ "$pytest_count" = "0" ]; then
    pass "d2177 item1: no tests_run entry mentions pytest"
  else
    fail "d2177 item1: no tests_run entry mentions pytest" "got $pytest_count pytest entries -- on main this is 1 (RUN_PYTHON set by the unnarrowed tests/* arm)"
  fi
  teardown
}

test_d2177_item2_python_routing_preserved() {
  setup
  local f
  for f in tests/test_agent_feed.py tests/backend/test_cors_dynamic.py tests/integration/test_full_stack.py backend/server.py; do
    GH_FILES="$f"
    local out suite pytest_cmd
    out=$(run_script 8002)
    pytest_cmd=$(tests_run_field "$out" "pytest" "command")
    if [ "$pytest_cmd" != "__MISSING__" ]; then
      pass "d2177 item2: $f still produces a tests_run entry mentioning pytest"
    else
      fail "d2177 item2: $f still produces a tests_run entry mentioning pytest" "no pytest entry"
    fi
    suite=$(routing_field "$out" "$f" "suite")
    if [ "$suite" = "pytest" ]; then
      pass "d2177 item2: $f's routing entry still names suite pytest"
    else
      fail "d2177 item2: $f's routing entry still names suite pytest" "got: $suite"
    fi
  done
  teardown
}

test_d2177_item3_denylisted_suite_skipped_and_no_pytest() {
  setup
  GH_FILES="tests/test_post_merge_hook_unmerged_paths.sh"

  local out skipped reason suite pytest_count
  out=$(run_script 8003)
  skipped=$(routing_field "$out" "tests/test_post_merge_hook_unmerged_paths.sh" "skipped")
  reason=$(routing_field "$out" "tests/test_post_merge_hook_unmerged_paths.sh" "reason")
  suite=$(routing_field "$out" "tests/test_post_merge_hook_unmerged_paths.sh" "suite")
  pytest_count=$(tests_run_count "$out" "pytest")

  if [ "$skipped" = "True" ]; then pass "d2177 item3: denylisted suite reported skipped"
  else fail "d2177 item3: denylisted suite reported skipped" "got skipped=$skipped"; fi

  if [ -n "$reason" ] && [ "$reason" != "__MISSING__" ] && [ "$reason" != "None" ]; then
    pass "d2177 item3: skipped entry carries a non-empty reason"
  else
    fail "d2177 item3: skipped entry carries a non-empty reason" "got reason=$reason"
  fi

  if [ "$suite" = "None" ]; then pass "d2177 item3: skipped entry's suite is null"
  else fail "d2177 item3: skipped entry's suite is null" "got: $suite"; fi

  if [ "$pytest_count" = "0" ]; then
    pass "d2177 item3: denylisted suite does not drag in pytest"
  else
    fail "d2177 item3: denylisted suite does not drag in pytest" "got $pytest_count pytest entries"
  fi
  teardown
}

test_d2177_item4_termination_emits_completed_results() {
  setup
  cat > "$TEST_DIR/tests/test_merge_gate.sh" <<'FIXEOF'
#!/usr/bin/env bash
echo "fixture merge gate"
exit 0
FIXEOF
  GH_FILES=$'tests/test_merge_gate.sh\nbackend/server.py'
  FAKE_PYTEST_SLEEP=60

  local out parse cmd_exit
  # Bounded well under FAKE_PYTEST_SLEEP so the pytest arm is still running
  # (and gets killed) when the bound fires; the bash suite above it finishes
  # in well under a second.
  out=$(run_script_bounded 6 8004)

  parse=$(assert_single_json_object "$out" 2>&1)
  if [ "$parse" = "ok" ]; then
    pass "d2177 item4: stdout parses as a single JSON object after a mid-run kill"
  else
    fail "d2177 item4: stdout parses as a single JSON object after a mid-run kill" "$parse"
  fi

  cmd_exit=$(tests_run_field "$out" "bash tests/test_merge_gate.sh" "exit_code")
  if [ "$cmd_exit" = "0" ]; then
    pass "d2177 item4: the completed bash-suite entry survives the kill, with exit_code 0"
  else
    fail "d2177 item4: the completed bash-suite entry survives the kill, with exit_code 0" "got exit_code: $cmd_exit -- on main this whole run produces zero bytes of stdout"
  fi
  teardown
}

test_d2177_item5_partial_flag_distinguishes_killed_from_complete() {
  setup
  cat > "$TEST_DIR/tests/test_merge_gate.sh" <<'FIXEOF'
#!/usr/bin/env bash
exit 0
FIXEOF
  GH_FILES=$'tests/test_merge_gate.sh\nbackend/server.py'
  FAKE_PYTEST_SLEEP=60
  local killed_out killed_partial
  killed_out=$(run_script_bounded 6 8005)
  killed_partial=$(manifest_json "$killed_out" | "$REAL_PYTHON3" -c 'import json,sys; print(json.load(sys.stdin).get("partial"))')
  if [ "$killed_partial" = "True" ]; then
    pass "d2177 item5: a killed run's manifest carries partial:true"
  else
    fail "d2177 item5: a killed run's manifest carries partial:true" "got: $killed_partial"
  fi
  teardown

  setup
  GH_FILES="tests/test_merge_gate2.sh"
  cat > "$TEST_DIR/tests/test_merge_gate2.sh" <<'FIXEOF'
#!/usr/bin/env bash
exit 0
FIXEOF
  local normal_out normal_partial
  normal_out=$(run_script 8006)
  normal_partial=$(manifest_json "$normal_out" | "$REAL_PYTHON3" -c 'import json,sys; print(json.load(sys.stdin).get("partial"))')
  if [ "$normal_partial" != "True" ]; then
    pass "d2177 item5: a normally-completing run's manifest does not carry partial:true"
  else
    fail "d2177 item5: a normally-completing run's manifest does not carry partial:true" "got: $normal_partial"
  fi
  teardown
}

test_d2177_item6_exactly_one_json_object_on_normal_path() {
  setup
  cat > "$TEST_DIR/tests/test_merge_gate.sh" <<'FIXEOF'
#!/usr/bin/env bash
echo "some suite output first"
exit 0
FIXEOF
  GH_FILES="tests/test_merge_gate.sh"

  local out parse
  out=$(run_script 8007)
  parse=$(assert_single_json_object "$out" 2>&1)
  if [ "$parse" = "ok" ]; then
    pass "d2177 item6: normal-path stdout is exactly one JSON object, nothing trailing"
  else
    fail "d2177 item6: normal-path stdout is exactly one JSON object, nothing trailing" "$parse"
  fi
  teardown
}

test_d2177_item7_no_double_counting_orphan_triage() {
  setup
  printf '#!/usr/bin/env bash\nexit 0\n' > "$TEST_DIR/tests/test_triage_orphan_diffs.sh"
  GH_FILES="tests/test_triage_orphan_diffs.sh"

  local out count
  out=$(run_script 8008)
  count=$(tests_run_count "$out" "test_triage_orphan_diffs.sh")
  if [ "$count" = "1" ]; then
    pass "d2177 item7: test_triage_orphan_diffs.sh appears exactly once in tests_run"
  else
    fail "d2177 item7: test_triage_orphan_diffs.sh appears exactly once in tests_run" "got $count entries"
  fi
  teardown
}

# Regression: run_suite's own RUN_SUITE_TIMEOUT_SECONDS bound must still
# reach a suite that forks its own child, run completely UNWRAPPED — no
# outer `timeout` at all. That matters: an outer wrapper's own group-wide
# kill can mask this exact failure (it did, in code review on this PR) by
# cleaning up the grandchild itself regardless of whether the inner bound
# ever reached it. run-pr-tests.sh's own documented usage is unwrapped
# (`bash scripts/run-pr-tests.sh PR`), so that's what this runs.
#
# History: an earlier version of this fix added --foreground to run_suite's
# inner `timeout` call so the termination trap would fire promptly on an
# outer kill. Reviewer-measured cost: --foreground stops `timeout` from
# creating a process group for the suite, so its own kill-after can only
# reach the suite's direct child, not a grandchild it forks — with
# RUN_PR_TESTS_PYTEST_TIMEOUT=2 against a suite forking an 8s child, the
# run took the full 8s instead of bounding at ~2s. Fixed by having
# run_suite background its command and `wait` on the PID (interruptible by
# a trap on its own) instead of leaning on --foreground, so `timeout` can
# go back to plain `--kill-after` and its default whole-group kill.
test_d2177_regression_bound_reaches_forked_grandchild() {
  setup
  local GH_FILES FAKE_PYTEST_FORK_SLEEP RUN_PR_TESTS_PYTEST_TIMEOUT FAKE_PYTEST_SLEEP
  GH_FILES="backend/foo.py"
  FAKE_PYTEST_FORK_SLEEP=8
  RUN_PR_TESTS_PYTEST_TIMEOUT=2
  FAKE_PYTEST_SLEEP=0

  local start elapsed out
  start=$(date +%s)
  out=$(run_script 9001)
  elapsed=$(( $(date +%s) - start ))

  if [ "$elapsed" -le 6 ]; then
    pass "regression: a suite forking a child is still bounded by RUN_PR_TESTS_PYTEST_TIMEOUT (elapsed=${elapsed}s)"
  else
    fail "regression: a suite forking a child is still bounded by RUN_PR_TESTS_PYTEST_TIMEOUT (elapsed=${elapsed}s)" "took ${elapsed}s against a 2s bound and an 8s forked grandchild -- the bound didn't reach it"
  fi

  local timed_out_val
  timed_out_val=$(pytest_entry_field "$out" "timed_out")
  if [ "$timed_out_val" = "True" ]; then
    pass "regression: the bounded pytest entry still reports timed_out:true"
  else
    fail "regression: the bounded pytest entry still reports timed_out:true" "got: $timed_out_val"
  fi
  teardown
}

test_d2177_item10_nongoal_comment_preserved() {
  if grep -q 'not, and is not made to be, an authoritative' "$REPO_ROOT/scripts/run-pr-tests.sh"; then
    pass "d2177 item10: the exit-code-is-not-authoritative comment is still present"
  else
    fail "d2177 item10: the exit-code-is-not-authoritative comment is still present" "comment missing or reworded away"
  fi
}

# Negative half of the isolation proof: the real production file's row count
# must be identical before and after the whole suite runs. A test asserting
# "the override was set" is weaker than showing production state never moved.
REAL_ROWS_BEFORE=$(real_flaky_history_rows)

test_item1_unrouted_files_are_null
test_item2_pytest_state_dir_and_no_timeout
test_item3_pytest_bound_reports_timeout
test_item7_bash_suite_maps_to_itself_and_runs
test_item8_orphan_triage_suites_self_map
test_item9_generic_bash_suite_is_bounded
test_item10_denylisted_suite_reported_skipped_with_reason

test_d2177_item1_bash_suite_no_longer_triggers_pytest
test_d2177_item2_python_routing_preserved
test_d2177_item3_denylisted_suite_skipped_and_no_pytest
test_d2177_item4_termination_emits_completed_results
test_d2177_item5_partial_flag_distinguishes_killed_from_complete
test_d2177_item6_exactly_one_json_object_on_normal_path
test_d2177_item7_no_double_counting_orphan_triage
test_d2177_regression_bound_reaches_forked_grandchild
test_d2177_item10_nongoal_comment_preserved

REAL_ROWS_AFTER=$(real_flaky_history_rows)
if [ "$REAL_ROWS_AFTER" = "$REAL_ROWS_BEFORE" ]; then
  pass "suite: production flaky-history.jsonl row count unchanged ($REAL_ROWS_BEFORE -> $REAL_ROWS_AFTER)"
else
  fail "suite: production flaky-history.jsonl row count unchanged" "$REAL_ROWS_BEFORE -> $REAL_ROWS_AFTER"
fi

echo ""
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
