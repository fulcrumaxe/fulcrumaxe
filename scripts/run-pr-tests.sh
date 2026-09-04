#!/usr/bin/env bash
# run-pr-tests.sh PR_NUMBER
#
# Detect which test suites a PR touches and run them.
# Outputs a JSON object to stdout:
#   { "routing": [{file, suite}, ...], "tests_run": [{command, exit_code, duration_seconds}, ...] }
# `routing` has one entry per changed file naming whichever suite claimed it,
# or `null` if none did (also named on stderr) — a reader can tell "was my
# change tested?" from the output alone, instead of trusting a green exit
# (D#2132: a bash-suite change routed to a pytest sweep that never collected
# it produced the same `{"tests_run":[]}`/exit-0 shape as a real pass).
# If this process is terminated (TERM/INT — e.g. an enclosing `timeout`) before
# every routed suite finishes, the same shape is still emitted on stdout for
# whichever suites had already completed, with an added "partial": true field
# (D#2177: a bounded run used to discard everything on a kill, turning a
# partial success into the same zero-bytes silence D#2132 was filed about). A
# normally-completing run never sets that field true.
# Exits 0 if all suites pass (or none were detected), non-zero if any fail —
# but that exit code is not, and is not made to be, an authoritative
# PR-pass/fail signal (the tree-wide pytest baseline has unrelated failures).
#
# Detection rules (by PR diff files):
#   backend/** or tests/**/*.py   → python3 -m pytest (whatever test dirs exist),
#                                    bounded by RUN_PR_TESTS_PYTEST_TIMEOUT (default
#                                    900s) with AUTONOMOUS_TEAM_STATE_DIR exported to
#                                    a scratch dir for the child process
#   tests/*.sh (top-level)         → bash tests/<name>.sh, self-routed (see the
#                                    third case block below) — does NOT also
#                                    trigger pytest (D#2177; it used to, because
#                                    the tests/**/*.py pattern above used to be
#                                    tests/** and matched .sh files too).
#                                    Denylisted files are reported skipped with
#                                    a reason instead of run.
#   tests/lib/**, tests/fixtures/** (non-Python) → unrouted on purpose: no
#                                    suite claims non-Python fixtures/helpers,
#                                    so they show up as suite:null in `routing`
#                                    and are named on stderr rather than
#                                    silently dropped (D#2177 side effect).
#   dashboard/**                  → npm install + npm run test + npm run test:e2e
#   tui/** or root *.spec.ts      → cd tui && npm run test (if script exists)
#   dashboard_tui/**              → tui-tester pre-merge sweep (blocks on error-severity findings)
#
# Usage:
#   bash scripts/run-pr-tests.sh 371
#   TESTS_JSON=$(bash scripts/run-pr-tests.sh 371)
#   EXIT_CODE=$?

set -euo pipefail

# JSON-escape a string for embedding as a JSON string literal.
_json_str() {
  printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

PR_NUMBER="${1:-}"
if [ -z "$PR_NUMBER" ]; then
  echo "Usage: $0 PR_NUMBER" >&2
  exit 1
fi

# Determine repo root — support being called from any cwd
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# shellcheck source=scripts/lib/worktree-ground-check.sh
source "$SCRIPT_DIR/lib/worktree-ground-check.sh"
REPO="$(_resolve_repo)"

# Suites denylisted from the generic tests/*.sh auto-run rule below (D#2132
# PR-b, triaged by D#2152). A denylisted file still appears in `routing`,
# marked skipped with this reason — it is never silently dropped.
#
# Grammar (D#2152 Spec item 1, enforced by tests/test_bash_suite_denylist_grammar.sh):
#   <path>:<class>(<argument>) — <one-line evidence>
# <class> is exactly one of:
#   bug(D#N)        — a filed, open [Bug] Discussion tracks a real defect.
#                      Exit condition: the cited Discussion's fix merges.
#   host-env(<dep>) — this host is missing or contends for a dependency the
#                      suite needs (a binary, a port, a green tree-wide
#                      pytest baseline). Exit condition: the named dependency
#                      leaves the suite (installed, freed, or the suite stops
#                      needing it).
#   slow(<measured>s > <bound>s) — the suite legitimately exceeds the bound
#                      RUN_PR_TESTS_BASH_TIMEOUT (RUN_PR_TESTS_BASH_TIMEOUT
#                      default 120s, not 20s) actually applies. Exit
#                      condition: the suite fits inside that bound.
#   flaky(<evidence>) — the suite's outcome is nondeterministic across runs.
#                      Exit condition: the nondeterminism is fixed, or its
#                      cause is found and it is reclassified bug(D#N).
#   obsolete(archive/<dir>) — the suite tests something that no longer
#                      exists; the file itself was git mv'd to that archive
#                      path. Exit condition: the path leaves this array with
#                      the archived file (nothing to remove separately).
#
# Reconciled OFF this list under D#2152 item 8 (each entry read "measured
# timeout >20s on this host", but the runner's real RUN_PR_TESTS_BASH_TIMEOUT
# default is 120s, not 20s): all five completed well inside 120s with 0
# failures, so a slow(...) classification could not be justified.
#   tests/test_dashboard_server_no_llm.sh — rc=0 dur=24s  7 passed, 0 failed
#   tests/test_loop_bootstrap.sh          — rc=0 dur=29s 22 passed, 0 failed
#   tests/test_loop_phased_step5.sh       — rc=0 dur=41s 100 passed, 0 failed
#   tests/test_scheduler_dispatcher.sh    — rc=0 dur=38s 16 passed, 0 failed
#   tests/test_spawn_agent_start_run.sh   — rc=0 dur=23s 25 passed, 0 failed
BASH_SUITE_DENYLIST=(
  "tests/test_post_merge_hook_unmerged_paths.sh:host-env(/usr/bin/git shim) — red on nix, unrelated to routing; shim hardcodes /usr/bin/git, see RUN_POST_MERGE_HOOK comment below (D#1976). LIVE FIXTURE: tests/test_run_pr_tests_routing.sh item10 depends on this path staying denylisted."
  "tests/smoke-3spawn-d984.sh:bug(D#2168) — FileNotFoundError: a3.json never written; SUBAGENT_STOP_DRY_RUN write path swallows its own error (2>/dev/null || true)"
  "tests/test_append_loop_metrics.sh:bug(D#2166) — missing field: ts; appended row carries key 'timestamp', not 'ts'"
  "tests/test_dashboard_lifecycle.sh:host-env(dashboard ports) — reconciled against the real 120s bound (dur=79s, still fails): Vite dev server did not respond on port 5273 within 30s"
  "tests/test_dial_bypass_coverage.sh:bug(D#2167) — FAIL B4: graphql mutation from worktree — expected exit 2 (blocked), got exit 0"
  "tests/test_execve_fence.sh:host-env(pyseccomp) — claude_execve_fence: FATAL: pyseccomp not installed — fence required"
  "tests/test_loop_bootstrap_extended.sh:bug(D#2165) — reconciled against the real 120s bound (dur=29s, still fails): stale assertions against loop-bootstrap/backend-snapshot/, archived by D#1890 on 2026-08-17"
  "tests/test_loop_merge_sha_pin.sh:flaky(inconsistent across repeat runs) — measured fail on this host, inconsistent across repeat runs on this host"
  "tests/test_post_agent_hook_substrate.sh:flaky(timed out some runs, passed at 16s others) — measured right at the old 20s bound; timed out on some runs, passed at 16s on others"
  "tests/test_post_merge_hook_browser_queue.sh:bug(D#2163) — queue file not created; scratch copy omits scripts/lib/ deps post-merge-hook.sh sources unconditionally"
  "tests/test_preflight_full.sh:host-env(pytest baseline) — preflight-full.sh runs pytest; tree-wide baseline is 67 failed / 23 errors on this host even with AUTONOMOUS_TEAM_STATE_DIR exported (D#2132 non-goal)"
  "tests/test_pre_spawn_token_cap.sh:bug(D#2163) — scripts/lib/state-dir.sh not found; make_ws()'s cp -r double-nests scripts/lib into scripts/lib/lib"
  "tests/test_reaper_clean_generated_wiki.sh:bug(D#2161) — missing-dir assertions; corroborated at 23/27 during PR #2147 review against unmodified main"
  "tests/test_run_pr_tests_routing.sh:flaky(d2177 item4/5 TERM-vs-SIGKILL race under load) — run_script_bounded's outer kill-after races _kill_tree plus manifest emission against the enclosing timeout's SIGKILL; measured 3/8 fail at kill-after=3s under load 34, 1/8 still failed at 5s, 0/8 at 10s (load 15-35, 8 trials per setting, interleaved A/B) — default widened to 5s to match this repo's own --kill-after=5s bounded-run convention, but the race narrows rather than closes at any fixed margin, so it is inherent to SIGKILL semantics on a loaded host, not a routing-logic defect"
  "tests/test_spawn_agent_file_scope.sh:bug(D#2163) — scripts/lib/repo-resolve.sh not found; scratch copy omits it (D#2153 shares no files with this suite, see D#2163)"
  "tests/test_spawn_agent_hook_event_id.sh:bug(D#2162) — hook_event_id not last; a prompt_manifest= line is now emitted after it"
  "tests/test_spawn_agent_includes_template_body.sh:bug(D#2164) — executor/impl-coordinator spawn exit code 1; every other tested role passes"
  "tests/test_spawn_enforcement_gates.sh:bug(D#2163) — scripts/lib/repo-resolve.sh not found; scratch copy omits it"
  "tests/test_start_dashboard.sh:host-env(dashboard ports) — ports still bound after stop: backend/api.py:18099 backend/server.py:8765 dashboard/server.py:8420 Vite:5173"
  "tests/test_start_dashboard_sh.sh:host-env(dashboard ports) — reconciled against the real 120s bound (dur=26s, still fails): same ports-bound-after-stop failure as test_start_dashboard.sh"
)
_bash_suite_denylist_reason() {
  local target="$1" entry
  for entry in "${BASH_SUITE_DENYLIST[@]}"; do
    [ "${entry%%:*}" = "$target" ] && { printf '%s' "${entry#*:}"; return 0; }
  done
  return 1
}

# Collect changed files in the PR
CHANGED_FILES=$(gh pr diff "$PR_NUMBER" --repo "$REPO" --name-only 2>/dev/null || true)
if [ -z "$CHANGED_FILES" ]; then
  # Try fetching via REST if diff --name-only fails (e.g. closed PR)
  CHANGED_FILES=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json files --jq '[.files[].path] | .[]' 2>/dev/null || true)
fi

if [ -z "$CHANGED_FILES" ]; then
  echo '{"routing":[],"tests_run":[]}'
  exit 0
fi

# Detect which suites to run
RUN_PYTHON=false
RUN_DASHBOARD=false
RUN_TUI=false
RUN_TS_BACKEND=false
RUN_ORPHAN_TRIAGE=false
RUN_TUI_ANTI_PATTERNS=false
RUN_POST_MERGE_HOOK=false

# Parallel to the loop below: which suite (if any) claimed ROUTING_FILES[i].
# Manifest field only (D#2132) — not a routing-logic change. Unmatched
# files stay "" here and are reported as `null` further down.
ROUTING_FILES=()
ROUTING_SUITES=()
# Parallel skip reason, set only for a denylisted tests/*.sh file (D#2132 PR-b).
ROUTING_REASONS=()
RUN_GENERIC_BASH=false
GENERIC_BASH_FILES=()

while IFS= read -r f; do
  [ -n "$f" ] || continue
  suite=""
  case "$f" in
    backend/*|tests/*.py|*_test.py|test_*.py)
      # tests/*.py spans nested dirs too (case globbing lets * cross a slash),
      # so this still catches tests/backend/*.py, tests/integration/*.py,
      # tests/orchestrator/*.py, tests/corpus_drift/*.py etc. Narrowed from
      # tests/* (D#2177): that used to also match tests/*.sh, which is what
      # sent bash suites into the 900s pytest arm on top of their own bash
      # run below — nothing ever cleared RUN_PYTHON for them. Non-Python
      # content under tests/lib/ and tests/fixtures/ falls out of this arm
      # on purpose; it has no suite of its own and is reported suite:null.
      RUN_PYTHON=true; suite="pytest" ;;
    dashboard/*)
      RUN_DASHBOARD=true; suite="dashboard" ;;
    ts-backend/*)
      RUN_TS_BACKEND=true; suite="ts-backend" ;;
    tui/*)
      RUN_TUI=true; suite="tui" ;;
    *.spec.ts|*.test.ts)
      # Only route root-level spec/test files to TUI; ts-backend's are already caught above
      if [[ "$f" != ts-backend/* ]]; then
        RUN_TUI=true
        suite="tui"
      fi
      ;;
    scripts/triage-orphan-diffs.sh|scripts/reap-worktrees.sh|scripts/lib/orphan-triage.sh|scripts/lib/worktree-registry.sh|tests/test_triage_orphan_diffs.sh)
      RUN_ORPHAN_TRIAGE=true; suite="orphan-triage" ;;
    dashboard_tui/*)
      RUN_TUI_ANTI_PATTERNS=true; suite="tui-anti-patterns" ;;
  esac

  # A second case on purpose. The one above stops at its first matching arm, so
  # anything under tests/ is claimed by the `backend/*|tests/*` arm and can never
  # reach a later one — a suite keyed only there would look registered and never
  # run. Keeping this separate also means touching a test file still triggers
  # pytest, as it did before.
  case "$f" in
    scripts/post-merge-hook.sh|scripts/lib/auto-pull-step.sh|scripts/lib/auto-pull-recover.sh|tests/test_post_merge_hook_pull.sh|tests/test_post_merge_hook_wiring.sh|tests/test_no_heredoc_hook_copies.sh|tests/test_auto_pull_recover.sh|tests/test_post_merge_hook_unmerged_paths.sh)
      RUN_POST_MERGE_HOOK=true
      [ -z "$suite" ] && suite="post-merge-hook"
      ;;
  esac

  # A third case, same reasoning as the second: a suite keyed only under
  # tests/ can't be reached by an earlier arm in the first case block. Any
  # directly-changed top-level tests/*.sh file maps to itself, overriding
  # whatever suite label the arms above gave it — this is what makes
  # tests/test_merge_gate.sh (and the two RUN_ORPHAN_TRIAGE arm's shadowed
  # entries) show up as the bash suite that actually tests them instead of
  # a pytest sweep that never collects them (D#2132 PR-b). A denylisted file
  # is reported skipped with a reason instead of queued to run.
  reason=""
  case "$f" in
    tests/*.sh)
      if [[ "${f#tests/}" != */* ]]; then
        if reason="$(_bash_suite_denylist_reason "$f")"; then
          suite=""
        else
          suite="bash $f"
          RUN_GENERIC_BASH=true
          GENERIC_BASH_FILES+=("$f")
        fi
      fi
      ;;
  esac

  ROUTING_FILES+=("$f")
  ROUTING_SUITES+=("$suite")
  ROUTING_REASONS+=("$reason")
done <<< "$CHANGED_FILES"

# Assemble the routing manifest + the "nothing claims this" stderr line.
# A denylisted file has an empty suite too, but it's not unrouted — it was
# claimed and deliberately skipped, which ROUTING_REASONS records.
UNROUTED_FILES=()
for _i in "${!ROUTING_FILES[@]}"; do
  if [ -z "${ROUTING_SUITES[$_i]}" ] && [ -z "${ROUTING_REASONS[$_i]}" ]; then
    UNROUTED_FILES+=("${ROUTING_FILES[$_i]}")
  fi
done
if [ ${#UNROUTED_FILES[@]} -gt 0 ]; then
  echo "[run-pr-tests] no suite claims: ${UNROUTED_FILES[*]}" >&2
fi

ROUTING_JSON=$(python3 -c '
import json, sys
# Always split (never guard on "argv is falsy -> []"): by this point in the
# script CHANGED_FILES is non-empty, so there is always >=1 file, and a
# single file with an all-empty field (an unrouted or non-skipped entry)
# makes argv[N] == "" too — the old falsy guard silently produced [] there,
# zip()-truncated every other array to empty, and the whole manifest came
# back "routing":[] (D#2132 PR-b, caught by the routing self-test).
files = sys.argv[1].split("\x1e")
suites = sys.argv[2].split("\x1e")
reasons = sys.argv[3].split("\x1e")
out = []
for f, s, r in zip(files, suites, reasons):
    entry = {"file": f, "suite": (s or None)}
    if r:
        entry["skipped"] = True
        entry["reason"] = r
    out.append(entry)
print(json.dumps(out))
' "$(IFS=$'\x1e'; echo "${ROUTING_FILES[*]:-}")" "$(IFS=$'\x1e'; echo "${ROUTING_SUITES[*]:-}")" "$(IFS=$'\x1e'; echo "${ROUTING_REASONS[*]:-}")")

# Accumulate results as JSON array entries
RESULTS=()
AGGREGATE_EXIT=0
# Dedup by display command — the generic tests/*.sh rule and the legacy
# RUN_ORPHAN_TRIAGE/RUN_POST_MERGE_HOOK registries below can both claim the
# same bash suite for the same PR; running it twice would double-count it
# in tests_run (D#2132 PR-b).
declare -A _RAN_COMMANDS=()

# Termination trap (D#2177 fix b). An enclosing `timeout` — the shape that
# produced the reviewer-measured "exit=124, 0 bytes" case — sends TERM to
# this script's whole process group when it isn't invoked with --foreground.
# Without this trap, that kill discards RESULTS/ROUTING_JSON (both
# main-shell variables — run_suite appends here directly, not in a
# subshell) along with the process, so a partial success reads as zero
# suites ran: the same shape of silence D#2132 was filed about, reproduced
# at the process-termination layer instead of the routing layer.
#
# An earlier version of this fix added --foreground to run_suite's inner
# `timeout` call so the trap would fire promptly instead of being deferred
# until run_suite's foreground command completed. That broke the bound
# itself: --foreground stops `timeout` from creating a process group for
# the command it monitors, so when that command forks its own child
# (pytest workers, a backgrounded step, etc.) `timeout` can only kill the
# direct child — the grandchild survives, and because run_suite piped
# suite output live into `sed`, that surviving grandchild kept the pipe's
# write end open and the whole script blocked on `sed`'s read() long past
# the suite's own bound (reviewer-measured: RUN_PR_TESTS_PYTEST_TIMEOUT=2
# against a suite forking a 30s child ran the full 30s). Both problems
# traced back to the same thing — a live pipe whose completion depends on
# every writer closing it, including ones outside our control — not to
# --foreground specifically.
#
# Fixed by having run_suite background its command and `wait` on that PID
# instead of running a synchronous foreground pipe: `wait PID` is the one
# bash construct the manual documents as interruptible by a pending trap,
# so the trap fires the instant a signal arrives regardless of what the
# suite's process tree is still doing. Suite output goes to a temp file
# instead of a live pipe, so noticing "the direct child exited" no longer
# depends on a pipe actually draining. `timeout` no longer needs
# --foreground — back to plain `timeout --kill-after`, so it goes back to
# creating its own process group for the suite and can kill that whole
# group (grandchildren included) when its own bound fires, exactly as it
# does on unmodified main. `_CURRENT_SUITE_PID` below is best-effort
# cleanup, not what makes either fix work: on a kill, the trap also reaches
# into that suite's process tree by PID so it doesn't linger as an orphan
# after this script exits.
_MANIFEST_EMITTED=false
_CURRENT_SUITE_PID=""

# Sends $2 (default TERM) to every descendant of PID $1, deepest first,
# then to $1 itself. Walks the tree by PID rather than assuming any
# particular process-group nesting, so it doesn't matter how many levels
# `timeout` and the suite itself created.
_kill_tree() {
  local pid="$1" sig="${2:-TERM}" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    _kill_tree "$child" "$sig"
  done
  kill "-$sig" "$pid" 2>/dev/null || true
}

_emit_partial_manifest() {
  set +e  # must not itself fail and swallow the emission
  [ "$_MANIFEST_EMITTED" = "true" ] && exit 124
  _MANIFEST_EMITTED=true
  [ -n "$_CURRENT_SUITE_PID" ] && _kill_tree "$_CURRENT_SUITE_PID" TERM
  local _joined=""
  if [ ${#RESULTS[@]} -gt 0 ]; then
    _joined=$(IFS=,; echo "${RESULTS[*]}")
  fi
  printf '{"routing":%s,"tests_run":[%s],"partial":true}\n' "$ROUTING_JSON" "$_joined"
  exit 124
}
trap _emit_partial_manifest TERM INT

run_suite() {
  local label="$1"; shift
  local cmd_display="$1"; shift
  local cwd="$1"; shift
  [ -n "${_RAN_COMMANDS[$cmd_display]:-}" ] && return 0
  _RAN_COMMANDS["$cmd_display"]=1
  # remaining args are the command to execute
  local T0
  T0=$(date +%s)
  local exit_code=0

  # Ground state before the command runs — a run whose cwd vanishes mid-run
  # (D#1809: a corrupted reviewer run once read as 489 ordinary test
  # failures on a clean branch) must not be able to pass through as either
  # an ordinary pass or an ordinary set of failures.
  local ground_before=1
  wt_ground_intact "$cwd" && ground_before=0

  # Optional per-call bound via RUN_SUITE_TIMEOUT_SECONDS (prefix-assigned by
  # the caller, so scoped to this invocation only). When set, wraps the
  # command in `timeout` and adds a `timed_out` field — a caller-side kill
  # must not look like an ordinary pass or failure either (D#2132).
  local timeout_seconds="${RUN_SUITE_TIMEOUT_SECONDS:-}"
  local outfile
  outfile=$(mktemp)
  # Backgrounded + waited rather than run as a synchronous foreground pipe,
  # and written to a temp file rather than piped live to `sed` (D#2177 —
  # see the trap's comment above for why both of those matter).
  if [ -n "$timeout_seconds" ]; then
    (cd "$cwd" && timeout --kill-after=5s "$timeout_seconds" "$@") >"$outfile" 2>&1 &
  else
    (cd "$cwd" && "$@") >"$outfile" 2>&1 &
  fi
  local suite_pid=$!
  _CURRENT_SUITE_PID="$suite_pid"
  wait "$suite_pid" || exit_code=$?
  _CURRENT_SUITE_PID=""
  sed 's/^/    /' "$outfile" 2>/dev/null
  rm -f "$outfile"

  local ground_after=1
  wt_ground_intact "$cwd" && ground_after=0

  local T1
  T1=$(date +%s)
  local dur=$((T1 - T0))

  # Escape the command string for JSON
  local json_cmd
  json_cmd=$(_json_str "$cmd_display")

  local timed_out_field=""
  if [ -n "$timeout_seconds" ]; then
    if [ "$exit_code" -eq 124 ]; then
      timed_out_field=',"timed_out":true'
    else
      timed_out_field=',"timed_out":false'
    fi
  fi

  if [ "$ground_before" -eq 0 ] && [ "$ground_after" -ne 0 ]; then
    RESULTS+=("{\"command\":$json_cmd,\"exit_code\":$exit_code,\"duration_seconds\":$dur,\"ground_lost\":true${timed_out_field}}")
    AGGREGATE_EXIT=1
  else
    RESULTS+=("{\"command\":$json_cmd,\"exit_code\":$exit_code,\"duration_seconds\":$dur${timed_out_field}}")
  fi
  # Best-effort flaky-sentinel record — never blocks the test run. Both
  # streams discarded: `record` prints the appended row to stdout, which
  # otherwise corrupts this script's single-JSON-object manifest whenever
  # any suite actually runs (D#2132).
  python3 backend/flaky_sentinel.py record --test-id "$cmd_display" --exit-code "$exit_code" \
    >/dev/null 2>&1 || true
  if [ "$exit_code" -ne 0 ]; then
    AGGREGATE_EXIT=$exit_code
  fi
}

# Generic tests/*.sh suites (D#2132 PR-b). Bounded per-suite — an auto-routed
# suite that hangs must be reported timed_out:true, not left to run forever.
if [ "$RUN_GENERIC_BASH" = "true" ]; then
  for gf in "${GENERIC_BASH_FILES[@]}"; do
    RUN_SUITE_TIMEOUT_SECONDS="${RUN_PR_TESTS_BASH_TIMEOUT:-120}" \
      run_suite "$gf" "bash $gf" "$REPO_ROOT" bash "$gf"
  done
fi

# Python tests
if [ "$RUN_PYTHON" = "true" ]; then
  TEST_DIRS=()
  [ -d "$REPO_ROOT/tests" ] && TEST_DIRS+=("tests/")
  [ -d "$REPO_ROOT/backend/tests" ] && TEST_DIRS+=("backend/tests/")

  if [ ${#TEST_DIRS[@]} -gt 0 ]; then
    # No -x. A runner that stops at the first failure, in a repo whose baseline
    # is not green, can only ever report the baseline — it aborts inside the
    # pre-existing failures and never reaches the PR's own. That makes it
    # structurally incapable of doing the one job it has. Measured at the time
    # this changed: the suite aborted around 4% in, so ~96% of it never ran on
    # any PR. Exit-code aggregation in run_suite is unchanged, so a failure
    # still fails the run — it just says which failures. D#1997 item 10.
    PYTEST_ARGS="${TEST_DIRS[*]}"
    # Bounded (D#2132: this arm ran unbounded before, 506-514s tree-wide with
    # no timeout at all) and with AUTONOMOUS_TEAM_STATE_DIR exported to a
    # scratch dir — backend/state_paths.py deliberately raises under pytest
    # when that var is unset (see CLAUDE.md's AUTONOMOUS_TEAM_STATE_DIR note).
    PYTEST_TIMEOUT="${RUN_PR_TESTS_PYTEST_TIMEOUT:-900}"
    PYTEST_STATE_DIR=$(mktemp -d)
    RUN_SUITE_TIMEOUT_SECONDS="$PYTEST_TIMEOUT" \
      run_suite "pytest" "python3 -m pytest $PYTEST_ARGS -q" "$REPO_ROOT" \
      env AUTONOMOUS_TEAM_STATE_DIR="$PYTEST_STATE_DIR" python3 -m pytest ${TEST_DIRS[@]} -q
  fi
fi

# ts-backend tests (bun test — bounded, never watch mode)
if [ "$RUN_TS_BACKEND" = "true" ] && [ -f "$REPO_ROOT/ts-backend/package.json" ]; then
  # bun is often installed at ~/.bun/bin which isn't on non-interactive CI PATH
  command -v bun >/dev/null 2>&1 || export PATH="$HOME/.bun/bin:$PATH"
  if ! command -v bun >/dev/null 2>&1; then
    echo "[run-pr-tests] ts-backend: bun not found (checked PATH + ~/.bun/bin) — skipping ts-backend tests" >&2
  else
    run_suite "ts-backend:test" "cd ts-backend && bun run test" "$REPO_ROOT/ts-backend" \
      timeout --kill-after=5s 180 bun run test
    run_suite "ts-backend:parity" "bash scripts/ts-backend-parity-check.sh" "$REPO_ROOT" \
      bash scripts/ts-backend-parity-check.sh
  fi
fi

# Dashboard tests (npm test + test:e2e)
if [ "$RUN_DASHBOARD" = "true" ] && [ -f "$REPO_ROOT/dashboard/package.json" ]; then
  # Install deps silently
  (cd "$REPO_ROOT/dashboard" && npm install --silent 2>&1 | tail -3) || true

  # npm run test (unit/vitest) — memory-guardrailed
  # 1. flock /tmp/vitest-host.lock so only one vitest runs per host at a time
  #    (vitest is jsdom + workers; N parallel agents = OOM, observed 2026-05-12)
  # 2. NODE_OPTIONS caps V8 heap at 1.5GB per process (config already pins single fork)
  if (cd "$REPO_ROOT/dashboard" && node -e "const p=require('./package.json');process.exit(p.scripts&&p.scripts.test?0:1)" 2>/dev/null); then
    run_suite "dashboard:test" "flock /tmp/vitest-host.lock npm run test" "$REPO_ROOT/dashboard" \
      env NODE_OPTIONS="--max-old-space-size=1536" \
      flock -w 600 /tmp/vitest-host.lock npm run test -- --reporter=verbose 2>&1 || true
  fi

  # Puppeteer test:e2e disabled 2026-05-11 — Chrome DevTools MCP is the canonical browser test path.
  # The old e2e/run.mjs spawned a fresh headless Chrome per call; with parallel executors this
  # produced 77+ chrome processes. Tracked in D#578 for full archival of dashboard/e2e/.
  echo "[run-pr-tests] dashboard:test:e2e skipped — puppeteer e2e disabled; use MCP scenarios" >&2
fi

# TUI tests
if [ "$RUN_TUI" = "true" ] && [ -f "$REPO_ROOT/tui/package.json" ]; then
  if (cd "$REPO_ROOT/tui" && node -e "const p=require('./package.json');process.exit(p.scripts&&p.scripts.test?0:1)" 2>/dev/null); then
    (cd "$REPO_ROOT/tui" && npm install --silent 2>&1 | tail -3) || true
    run_suite "tui:test" "cd tui && npm run test" "$REPO_ROOT/tui" npm run test
  fi
fi

# Orphan-diff triage tests. When the changed file is the test itself, the
# generic tests/*.sh rule above already ran it (bounded) and _RAN_COMMANDS
# dedup makes these no-ops; they still cover the scripts/*-only-changed case.
if [ "${RUN_ORPHAN_TRIAGE:-false}" = "true" ] && [ -f "$REPO_ROOT/tests/test_triage_orphan_diffs.sh" ]; then
  RUN_SUITE_TIMEOUT_SECONDS="${RUN_PR_TESTS_BASH_TIMEOUT:-120}" \
    run_suite "triage-orphan-diffs" "bash tests/test_triage_orphan_diffs.sh" "$REPO_ROOT" bash tests/test_triage_orphan_diffs.sh
fi

# Reaper self-exclusion guard — proves the reaper can't delete the worktree
# it's running in (D#1864). D#2159: this suite drove its scenarios through
# the parent_pid/last_heartbeat orphan-detection block, which was archived
# to archive/worktree-registry-heartbeat-half-2026-08-24/ (dead in
# production — nothing calls `worktree_registry register`). The suite went
# with it; see that folder's README for what still covers self-exclusion
# (test_reaper_git_tracked_removal.sh, test_reap_spawn_budget.sh,
# test_sweep_stale_worktrees.sh) and what a replacement would need to cover.
# The file-existence guard below is what makes this a clean no-op rather
# than a silent rot: the suite is gone with a paper trail, not quietly
# skipped in an untracked way.
if [ "${RUN_ORPHAN_TRIAGE:-false}" = "true" ] && [ -f "$REPO_ROOT/tests/test_worktree_self_exclusion.sh" ]; then
  RUN_SUITE_TIMEOUT_SECONDS="${RUN_PR_TESTS_BASH_TIMEOUT:-120}" \
    run_suite "worktree-self-exclusion" "bash tests/test_worktree_self_exclusion.sh" "$REPO_ROOT" bash tests/test_worktree_self_exclusion.sh
fi

# post-merge-hook auto_pull suites. This registry is the only thing that runs
# them when only a script source file changed: preflight-fast.sh runs pytest
# only and never tests/*.sh, and CI is off repo-wide (D#1937). When the
# changed file is one of the tests themselves, the generic tests/*.sh rule
# above already ran it (bounded) and _RAN_COMMANDS dedup makes the matching
# call here a no-op.
# tests/test_post_merge_hook_unmerged_paths.sh is a trigger above but is not run
# here on purpose: it is red on nix for an unrelated reason (its shim hardcodes
# /usr/bin/git) and would fail every PR that touched the hook. D#1976 fixes it,
# and adds it here.
if [ "${RUN_POST_MERGE_HOOK:-false}" = "true" ] && [ -f "$REPO_ROOT/tests/test_post_merge_hook_pull.sh" ]; then
  RUN_SUITE_TIMEOUT_SECONDS="${RUN_PR_TESTS_BASH_TIMEOUT:-120}" \
    run_suite "post-merge-hook:pull" "bash tests/test_post_merge_hook_pull.sh" \
    "$REPO_ROOT" bash tests/test_post_merge_hook_pull.sh
  RUN_SUITE_TIMEOUT_SECONDS="${RUN_PR_TESTS_BASH_TIMEOUT:-120}" \
    run_suite "post-merge-hook:wiring" "bash tests/test_post_merge_hook_wiring.sh" \
    "$REPO_ROOT" bash tests/test_post_merge_hook_wiring.sh
  RUN_SUITE_TIMEOUT_SECONDS="${RUN_PR_TESTS_BASH_TIMEOUT:-120}" \
    run_suite "post-merge-hook:no-heredoc-copies" "bash tests/test_no_heredoc_hook_copies.sh" \
    "$REPO_ROOT" bash tests/test_no_heredoc_hook_copies.sh
  RUN_SUITE_TIMEOUT_SECONDS="${RUN_PR_TESTS_BASH_TIMEOUT:-120}" \
    run_suite "auto-pull-recover" "bash tests/test_auto_pull_recover.sh" \
    "$REPO_ROOT" bash tests/test_auto_pull_recover.sh
fi

# TUI anti-pattern pre-merge gate — blocks on error-severity findings
if [ "$RUN_TUI_ANTI_PATTERNS" = "true" ]; then
  echo "[run-pr-tests] dashboard_tui/** touched — running tui-tester pre-merge sweep" >&2
  run_suite "tui-anti-patterns" \
    "bash scripts/hooks/pre-merge.d/tui-tester-sweep.sh --pr $PR_NUMBER" \
    "$REPO_ROOT" \
    bash scripts/hooks/pre-merge.d/tui-tester-sweep.sh --pr "$PR_NUMBER"
fi

# Build JSON output. Disarm the termination trap first — a normal completion
# must never also emit the partial-marked manifest below it (item 6: exactly
# one JSON object on stdout on this path).
trap - TERM INT
_MANIFEST_EMITTED=true
if [ ${#RESULTS[@]} -eq 0 ]; then
  echo "{\"routing\":$ROUTING_JSON,\"tests_run\":[]}"
else
  JOINED=$(IFS=,; echo "${RESULTS[*]}")
  echo "{\"routing\":$ROUTING_JSON,\"tests_run\":[$JOINED]}"
fi

exit $AGGREGATE_EXIT
