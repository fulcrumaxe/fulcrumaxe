#!/usr/bin/env bash
# tests/test_engine_emit_callers.sh — the instrumented engine sources, run for real
# under `set -euo pipefail`, with the emitter working and with it forced to fail.
#
# What it proves:
#   * each source writes the closed-shape event it is meant to write;
#   * an emitter that cannot write (ENGINE_EMIT_FORCE_FAIL=1) never changes the
#     caller's own result;
#   * a metrics-file override (tests, replays) writes no engine events at all.
#
# scripts/merge-and-hook.sh is covered in tests/test_merge_and_hook.sh (Test 2b).
# scripts/post-merge-hook.sh and scripts/start-the-day.sh act on the operator's
# checkout when run, so, like tests/test_post_merge_hook_wiring.sh, they are
# checked by reading the shipping script.
#
# Run: bash tests/test_engine_emit_callers.sh

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0; FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — ${2:-}"; FAIL=$((FAIL + 1)); }
eq() { [[ "$2" == "$3" ]] && pass "$1" || fail "$1" "got '$2', expected '$3'"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

# summarize <state-dir> — one "kind field=value ..." line per event, nothing else.
summarize() {
  python3 -I -c "
import json, sys
for l in open(sys.argv[1] + '/engine-events.jsonl'):
    e = json.loads(l)
    print(e['kind'], *(k + '=' + str(e[k]) for k in sorted(e) if k not in ('v','seq','ts','engine_instance_id','kind','head_sha','source_ages_s')))
" "$1" 2>/dev/null
}

echo "gate1_receipt_emit"
S="$T/g1"; SHA="$(printf 'a%.0s' {1..40})"
out=$( set -euo pipefail
  export AUTONOMOUS_TEAM_STATE_DIR="$S"
  source "$REPO_ROOT/scripts/lib/gate1-receipt.sh"
  gate1_receipt_emit 41 "$SHA" 0 true
  gate1_receipt_emit 42 "$SHA" 1 true
  gate1_receipt_emit 43 "$SHA" 0 false
  gate1_receipt_emit 44 "not-a-sha" 0 true
  echo "done" )
eq "returns 0 under errexit" "$out" "done"
eq "pass, fail, error; a bad sha emits nothing" "$(summarize "$S" | tr '\n' ';')" \
   "gate_receipt gate=gate1 pr=41 result=pass;gate_receipt gate=gate1 pr=42 result=fail;gate_receipt gate=gate1 pr=43 result=error;"
out=$( set -euo pipefail
  export AUTONOMOUS_TEAM_STATE_DIR="$T/none/sub" ENGINE_EMIT_FORCE_FAIL=1
  source "$REPO_ROOT/scripts/lib/gate1-receipt.sh"
  gate1_receipt_emit 41 "$SHA" 0 true; echo "rc=$?" )
eq "an emitter that fails does not fail the caller" "$out" "rc=0"

echo "review labels (gh-label.sh)"
mkdir -p "$T/bin"
cat > "$T/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  "pr view"*"headRefOid"*) printf '%s\n' "${STUB_SHA:-}" ;;
  "api -X POST"*) [[ "${STUB_APPLY_FAIL:-0}" == "1" ]] && { echo "boom" >&2; exit 1; }; echo '[]' ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$T/bin/gh"
S="$T/lab"
out=$( set -euo pipefail
  export PATH="$T/bin:$PATH" AUTONOMOUS_TEAM_STATE_DIR="$S" LABEL_REPO=o/r GH_LABEL_FORCE_WORKTREE=0 STUB_SHA="$SHA"
  source "$REPO_ROOT/scripts/lib/gh-label.sh"
  apply_label 7 code-review-passed
  apply_label 7 security-needs-fix
  apply_label 7 acceptance-failed
  apply_label 7 some-other-label
  STUB_APPLY_FAIL=1 apply_label 8 code-review-passed 2>/dev/null || echo "apply failed as it should"
  STUB_SHA="" apply_label 9 acceptance-passed
  echo "done" )
eq "apply_label keeps its own result" "$(echo "$out" | tr '\n' ';')" "apply failed as it should;done;"
eq "gate labels emit; others, failed applies and a missing sha do not" "$(summarize "$S" | tr '\n' ';')" \
   "gate_receipt gate=code_review pr=7 result=pass;gate_receipt gate=security_review pr=7 result=fail;gate_receipt gate=acceptance pr=7 result=fail;"
out=$( set -euo pipefail
  export PATH="$T/bin:$PATH" AUTONOMOUS_TEAM_STATE_DIR="$T/none/sub" LABEL_REPO=o/r GH_LABEL_FORCE_WORKTREE=0 STUB_SHA="$SHA" ENGINE_EMIT_FORCE_FAIL=1
  source "$REPO_ROOT/scripts/lib/gh-label.sh"
  apply_label 7 code-review-passed; echo "rc=$?" )
eq "a failing emitter does not fail apply_label" "$out" "rc=0"

echo "append-loop-metrics.sh"
mk_tree() {
  local d="$1"
  mkdir -p "$d/scripts/lib" "$d/schemas" "$d/.autonomous-team"
  cp "$REPO_ROOT/scripts/append-loop-metrics.sh" "$REPO_ROOT/scripts/engine-emit.sh" \
     "$REPO_ROOT/scripts/engine-team-log-poll.py" "$REPO_ROOT/scripts/engine-plan-snapshot.py" "$d/scripts/"
  cp "$REPO_ROOT/scripts/lib/engine_emit.py" "$REPO_ROOT/scripts/lib/state-dir.sh" "$d/scripts/lib/"
  cp "$REPO_ROOT/schemas/engine-events.v1.json" "$d/schemas/"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$d/scripts/rotate-team-log.sh"
}
run_metrics() { # <tree> <state> [extra env...]
  local d="$1" s="$2"; shift 2
  env -u METRICS_FILE -u AF_MCP_TEST_ORIGIN PATH="$T/bin:$PATH" AUTONOMOUS_TEAM_STATE_DIR="$s" "$@" \
    bash "$d/scripts/append-loop-metrics.sh" --duration-seconds 33 --agents-spawned 0 --prs-merged 0 \
      --discussions-scanned 0 --prs-scanned 0 </dev/null >/dev/null 2>&1
}
mk_tree "$T/m"
run_metrics "$T/m" "$T/m-state" AF_METRICS_ORIGIN=cron; RC=$?
eq "exit 0 with the emitter working" "$RC" "0"
eq "loop_tick then heartbeat; the loop's age is reported" "$(summarize "$T/m-state" | tr '\n' ';')" \
   "loop_tick duration_s=33 origin=cron;engine_heartbeat origin=cron;"
eq "the heartbeat carries the loop source age" "$(python3 -I -c "
import json,sys
e=[json.loads(l) for l in open(sys.argv[1])][-1]
print(sorted(e['source_ages_s']))" "$T/m-state/engine-events.jsonl")" "['loop']"
run_metrics "$T/m" "$T/m-state2" AF_METRICS_ORIGIN=cron ENGINE_EMIT_FORCE_FAIL=1; RC=$?
eq "exit 0 with the emitter forced to fail" "$RC" "0"
[[ ! -e "$T/m-state2/engine-events.jsonl" ]] && pass "failing emitter wrote nothing" || fail "failing emitter wrote nothing"
run_metrics "$T/m" "$T/m-state3" AF_METRICS_ORIGIN=cron METRICS_FILE="$T/override.jsonl"; RC=$?
eq "exit 0 with a metrics-file override" "$RC" "0"
[[ ! -e "$T/m-state3/engine-events.jsonl" ]] && pass "an override writes no engine events" || fail "an override writes no engine events"
run_metrics "$T/m" "$T/m-state4" AF_METRICS_ORIGIN=test; RC=$?
[[ ! -e "$T/m-state4/engine-events.jsonl" ]] && pass "test origin writes no engine events" || fail "test origin writes no engine events"

echo "post-merge-hook.sh and start-the-day.sh wiring"
grep -q 'engine-emit.sh" merged --pr "\$PR"' "$REPO_ROOT/scripts/post-merge-hook.sh" \
  && pass "post-merge-hook emits merged" || fail "post-merge-hook emits merged"
awk '/engine-emit.sh" merged/,/hook_event_mark_step "agent_feed"/' "$REPO_ROOT/scripts/post-merge-hook.sh" | grep -q '|| true' \
  && pass "the merged emit cannot fail the hook" || fail "the merged emit cannot fail the hook"
grep -q 'engine-plan-snapshot.py" --root "\$REPO_ROOT" --plan-date "\$TODAY" >/dev/null 2>&1 || true' "$REPO_ROOT/scripts/start-the-day.sh" \
  && pass "start-the-day sends the plan snapshot and cannot fail on it" || fail "start-the-day sends the plan snapshot"
for f in post-merge-hook.sh start-the-day.sh merge-and-hook.sh gate1-invoke.sh loop-phased-step5.sh append-loop-metrics.sh lib/gh-label.sh lib/gate1-receipt.sh engine-emit.sh; do
  bash -n "$REPO_ROOT/scripts/$f" && pass "$f parses" || fail "$f parses"
done

echo; echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
