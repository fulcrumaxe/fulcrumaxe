#!/usr/bin/env bash
# tests/test_hook_caller_failure_surfacing.sh — D#2111 acceptance criteria 1-7
#
# scripts/subagent-stop-hook.sh and scripts/loop-phased-step5.sh both call a
# callee (post-agent-hook.sh / post-merge-hook.sh) that can abort for any of
# several reasons — hook_event_init's INIT_FAILED is just one of five (site 1)
# or six (site 2). Before D#2111 both callers discarded the specific cause:
# site 1 via `|| true` on a piped exit code, site 2 via `2>/dev/null`.
#
# This test forces a REAL abort at each site — not a mock that always says
# "something failed" — and checks that the caller's own record names the
# actual cause, cause-agnostically. Both call sites are exercised in the one
# way that has never run under any *other* test:
#   site 1: SUBAGENT_STOP_DRY_RUN unset (every other test sets it, which
#           returns before the line under repair — see
#           scripts/tests/test_event_id_containment.py:21-24)
#   site 2: HOOKS_DISABLED unset (test_merge_gate.sh always sets it)
#
# Each site also carries a mutation check: the fixed script is copied and
# surgically reverted (site 1) or narrowed to a INIT_FAILED-only grep (site 2,
# mutation B) to prove the corresponding criterion actually goes red — not
# just "a log line exists somewhere", which would pass against the mutant too.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/blackboard-fixture.sh
source "$SCRIPT_DIR/lib/blackboard-fixture.sh"
# D#2283: redirect AUTONOMOUS_TEAM_STATE_DIR to a scratch dir for the life of
# this suite, so pr_state fixtures and audit.jsonl writes land there instead
# of ~/.autonomous-forever-state — on the clean path and on a crash alike.
# The pre-existing `trap _cleanup EXIT` below stays as-is; the scratch dir's
# removal is folded into _cleanup rather than a second trap registration,
# which would silently replace this one (D#2283 trap-composition hazard).
blackboard_scratch_state_dir || {
  echo "FATAL: could not create scratch state dir" >&2
  exit 1
}
SCRATCH_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
BB_PR_STATE_DIR="$(blackboard_pr_state_dir "$REPO_ROOT")" || {
  echo "FATAL: could not resolve blackboard pr_state dir" >&2
  exit 1
}

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

WORK=$(mktemp -d /tmp/test-hook-caller-failure-surfacing.XXXXXX)
_cleanup() {
  rm -rf "$WORK"
  rm -f "$BB_PR_STATE_DIR/88501.json" \
        "$BB_PR_STATE_DIR/88502.json" \
        "$BB_PR_STATE_DIR/88503.json" \
        "$BB_PR_STATE_DIR/88504.json"
  rm -rf "$SCRATCH_STATE_DIR"
}
trap _cleanup EXIT

# =============================================================================
# Site 1: scripts/subagent-stop-hook.sh (post-agent-hook.sh callee)
# =============================================================================

# Build a transcript carrying a known hook_event_id: one assistant-role
# AGENT_OUTPUT envelope (verdict=fail, no --pr, so post-agent-hook.sh's
# verify_pr_exists() never makes a network call) and one user-role line
# with the hook_event_id= tag (transcript_event_id.py's flat/Shape-B match).
_make_transcript() {
  local out="$1" event_id="$2" disc="$3"
  python3 - "$out" "$event_id" "$disc" <<'PYEOF'
import json, sys
out, event_id, disc = sys.argv[1], sys.argv[2], sys.argv[3]
envelope = (
    "done\n\n<!-- AGENT_OUTPUT -->\n```json\n"
    '{"agent": "executor", "discussion": ' + disc + ', "verdict": "fail"}\n'
    "```\n<!-- /AGENT_OUTPUT -->"
)
lines = [
    {"role": "assistant", "content": envelope},
    {"role": "user", "content": "hook_event_id=" + event_id},
]
with open(out, "w") as f:
    for line in lines:
        f.write(json.dumps(line) + "\n")
PYEOF
}

_make_stdin_json() {
  local transcript="$1" session_id="$2"
  python3 - "$transcript" "$session_id" <<'PYEOF'
import json, sys
transcript, session_id = sys.argv[1], sys.argv[2]
print(json.dumps({
    "hook_event_name": "SubagentStop",
    "session_id": session_id,
    "transcript_path": transcript,
    "cwd": "/tmp",
}))
PYEOF
}

# Force post-agent-hook.sh's hook_event_init to hit D#2105's own repro: a
# mode-000 lock file at "<event_id>-pah.lock" under a scratch HOOK_EVENT_DIR.
# post-agent-hook.sh appends "-pah" to the supplied --event-id for its own
# idempotency id (see post-agent-hook.sh's comment on TASK_EVENT_ID).
_run_site1() {
  local caller="$1" event_id="$2" disc="$3" session_id="$4" out_stdout="$5" out_stderr="$6"
  local hed="$WORK/hed-$session_id"
  mkdir -p "$hed"
  local lockfile="$hed/${event_id}-pah.lock"
  touch "$lockfile"
  chmod 000 "$lockfile"

  local transcript="$WORK/transcript-$session_id.jsonl"
  _make_transcript "$transcript" "$event_id" "$disc"
  local stdin_json
  stdin_json=$(_make_stdin_json "$transcript" "$session_id")

  HOOK_EVENT_DIR="$hed" bash "$caller" <<< "$stdin_json" > "$out_stdout" 2> "$out_stderr"
  return $?
}

# Pull the log path this run's diagnostic named, e.g. "... (log: /tmp/foo)".
_extract_log_path() {
  python3 - "$1" <<'PYEOF'
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r"\(log: (\S+)\)", text)
print(m.group(1) if m else "")
PYEOF
}

echo ""
echo "=== Site 1: scripts/subagent-stop-hook.sh, real (non-dry-run) callee abort ==="

EVENT_ID_C1="executor-88601-1755800001"
EVENT_ID_C2="executor-88602-1755800002"

_run_site1 "$REPO_ROOT/scripts/subagent-stop-hook.sh" "$EVENT_ID_C1" 88601 "c1" \
  "$WORK/c1.out" "$WORK/c1.err"
RC_C1=$?

if [ "$RC_C1" -eq 0 ]; then
  _pass "criterion 3: site1 exits 0 despite the forced callee failure"
else
  _fail "criterion 3: site1 exit=$RC_C1 (expected 0)"
fi

if grep -q "post-agent-hook" "$WORK/c1.err" && grep -q "exit 1" "$WORK/c1.err"; then
  _pass "criterion 2: site1 record names the hook and the exit status"
else
  _fail "criterion 2: site1 stderr missing hook name/status -- $(cat "$WORK/c1.err")"
fi

SINK_C1=$(_extract_log_path "$WORK/c1.err")
if [ -n "$SINK_C1" ] && [ -f "$SINK_C1" ] && grep -q "INIT_FAILED: failed to open lock fd" "$SINK_C1"; then
  _pass "criterion 1: site1 per-run record contains the callee's specific cause"
else
  _fail "criterion 1: site1 sink '$SINK_C1' missing the INIT_FAILED cause"
fi

_run_site1 "$REPO_ROOT/scripts/subagent-stop-hook.sh" "$EVENT_ID_C2" 88602 "c2" \
  "$WORK/c2.out" "$WORK/c2.err"
SINK_C2=$(_extract_log_path "$WORK/c2.err")

if [ -n "$SINK_C1" ] && [ -n "$SINK_C2" ] && [ "$SINK_C1" != "$SINK_C2" ] \
   && [ -f "$SINK_C1" ] && [ -f "$SINK_C2" ]; then
  _pass "criterion 4: two runs with different event ids get distinct, both-readable records"
else
  _fail "criterion 4: sinks not distinct/both readable (sink1=$SINK_C1 sink2=$SINK_C2)"
fi

if (cd "$REPO_ROOT" && grep -rn "last-post-agent-hook" --include=*.sh scripts/ >/dev/null 2>&1); then
  _fail "criterion 5: an unread /tmp sink reference is still present in scripts/"
else
  _pass "criterion 5: no lingering reference to the old unread /tmp sink"
fi

echo ""
echo "--- Site 1 mutation: revert to '|| true' on a copy, confirm criteria 1 & 4 go red ---"

SCRIPTS_MUT_SITE1="$WORK/scripts-mut-site1"
cp -r "$REPO_ROOT/scripts" "$SCRIPTS_MUT_SITE1"

python3 - "$REPO_ROOT/scripts/subagent-stop-hook.sh" "$SCRIPTS_MUT_SITE1/subagent-stop-hook.sh" "$WORK/last-post-agent-hook.err" <<'PYEOF'
import sys
src_path, dest_path, err_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src_path) as f:
    src = f.read()

# Locate the fix by unique, short anchors rather than reproducing the whole
# block by hand -- less to transcribe correctly, and it survives comment
# rewording. Start anchor is the fix's own explanatory comment; end anchor is
# the literal "Always exit 0" comment that immediately follows the whole
# else-block in the un-mutated file.
start_anchor = "# D#2111: the old fixed-path tee target had exactly one writer and zero"
end_locator = "\n\n# Always exit 0"

assert src.count(start_anchor) == 1, "site1 fix start-anchor not found uniquely -- has the comment changed?"
assert src.count(end_locator) == 1, "site1 end-locator not found uniquely"

start_idx = src.index(start_anchor)
locator_idx = src.index(end_locator, start_idx)
# The outer "fi" that closes the whole else-block is the last "fi\n" line
# before the end locator.
fi_marker = "\nfi\n"
fi_idx = src.rindex(fi_marker, start_idx, locator_idx + 1)
replace_end = fi_idx + len(fi_marker)

old_span = src[start_idx:replace_end]
new_span = (
    "# D#2111 mutation: reverted to the pre-fix idiom on purpose, to prove\n"
    "  # criteria 1 and 4 actually depend on the fix (not on a log line merely\n"
    "  # existing somewhere).\n"
    '  bash "$SCRIPT_DIR/post-agent-hook.sh" "${POST_HOOK_ARGS[@]}"'
    f' 2>&1 | tee {err_path} >/dev/null || true\n'
    "fi\n"
)
mutated = src[:start_idx] + new_span + src[replace_end:]
assert mutated != src, "mutation produced no change"
with open(dest_path, "w") as f:
    f.write(mutated)
PYEOF

# Suite-scoped stand-in for the pre-fix tee target — nothing under scripts/
# or hooks/ hardcodes this name (D#2254), so it's safe to parameterise; the
# mutation above wrote its own copy of the string into $SCRIPTS_MUT_SITE1's
# subagent-stop-hook.sh, using this exact path.
MUT_SINK="$WORK/last-post-agent-hook.err"
rm -f "$MUT_SINK"
_run_site1 "$SCRIPTS_MUT_SITE1/subagent-stop-hook.sh" "$EVENT_ID_C1" 88601 "m1" \
  "$WORK/m1.out" "$WORK/m1.err"
SINK_M1=$(_extract_log_path "$WORK/m1.err")

if [ -z "$SINK_M1" ]; then
  _pass "mutation: criterion 1 goes red (no discoverable per-run record under the reverted idiom)"
else
  _fail "mutation: criterion 1 did NOT go red -- still found a sink at '$SINK_M1'"
fi

_run_site1 "$SCRIPTS_MUT_SITE1/subagent-stop-hook.sh" "$EVENT_ID_C2" 88602 "m2" \
  "$WORK/m2.out" "$WORK/m2.err"

if [ -f "$MUT_SINK" ] \
   && grep -q "${EVENT_ID_C2}-pah.lock" "$MUT_SINK" \
   && ! grep -q "${EVENT_ID_C1}-pah.lock" "$MUT_SINK"; then
  _pass "mutation: criterion 4 goes red (second run clobbers the first at the shared fixed path)"
else
  _fail "mutation: criterion 4 did NOT go red -- expected the shared path to show only the second run's lock path"
fi
rm -f "$MUT_SINK"

# =============================================================================
# Site 2: scripts/loop-phased-step5.sh (post-merge-hook.sh callee)
# =============================================================================
#
# Reuses the merge-gate fixture shape from tests/test_merge_gate.sh
# (_make_config_file / _write_snapshot_spec_ready / _write_pr_state_merging),
# adapted to run against a scratch copy of scripts/ whose post-merge-hook.sh
# is stubbed to a real, non-INIT_FAILED abort (post-merge-hook.sh:183's
# "Unknown argument" path) -- so this test proves the fix is cause-agnostic,
# not keyed to the D#2105 string.

_make_config_file() {
  local tmpfile
  tmpfile=$(mktemp --suffix='.json')
  cat > "$tmpfile" <<'JSON'
{
  "gates": {
    "auto_merge": true,
    "security_review": false,
    "budget_check": false,
    "idea_generation": true,
    "stall_detection": false,
    "wiki_sync": false,
    "human_verification": false,
    "self_observe_executor": false,
    "self_observe_impl_coord": false,
    "docs_writer": false,
    "incident_commander": false,
    "release_manager": false,
    "runbook_writer": false,
    "phased_orchestration": true,
    "phased_code_review": true
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
  python3 - "$path" "$disc_num" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
path, disc_num = sys.argv[1], int(sys.argv[2])
snap = {
    "discussions": [
        {"number": disc_num, "title": f"Test feature {disc_num}",
         "body": "<!-- STATUS:SPEC_READY --> some spec content"}
    ],
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
}
json.dump(snap, open(path, "w"))
PYEOF
}

_write_pr_state_merging() {
  local pr_num="$1" disc_num="$2" needs_sec="${3:-True}"
  local bb_dir="$BB_PR_STATE_DIR"
  mkdir -p "$bb_dir"
  python3 - "$bb_dir" "$pr_num" "$disc_num" "$needs_sec" <<'PYEOF'
import json, sys
bb_dir, pr_num, disc_num, needs_sec = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "True"
entry = {
    "value": {
        "pr": pr_num, "discussion": disc_num, "phase": "merging",
        "spawned_phases": [], "completed_phases": [],
        "needs_security_review": needs_sec, "fix_cycle_count": 0,
        "respawn_count": 0, "last_envelope": {}, "blocked_reason": None,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    },
    "version": 1, "updated_at": "2026-01-01T00:00:00+00:00", "updated_by": "test",
}
json.dump(entry, open(f"{bb_dir}/{pr_num}.json", "w"), indent=2)
PYEOF
}

_remove_pr_state_entry() {
  rm -f "$BB_PR_STATE_DIR/$1.json"
}

_stub_post_merge_hook() {
  # A real, non-INIT_FAILED abort: post-merge-hook.sh:~183's own
  # "Unknown argument" path, for an argument no caller in this codebase
  # actually passes -- the point is the message text, not the trigger.
  cat > "$1" <<'STUB'
#!/usr/bin/env bash
echo "Unknown argument: --bogus" >&2
echo "Usage: $0 --pr <N> [--discussion <N>]" >&2
exit 1
STUB
  chmod +x "$1"
}

_run_site2() {
  # args: script_path pr disc hooks_disabled out_file
  local script_path="$1" pr="$2" disc="$3" hooks_disabled="$4" out_file="$5"
  local cfg snap
  cfg=$(_make_config_file)
  snap=$(mktemp --suffix='.json')
  _write_snapshot_spec_ready "$snap" "$disc"
  _write_pr_state_merging "$pr" "$disc" "True"

  local -a env_args=(
    AF_CONTROL_PLANE_CONFIG="$cfg" SNAPSHOT_PATH="$snap"
    SPAWN_AGENT=echo GH_MERGE=echo
    AUTONOMOUS_TEAM_REPO=autonomous-agent-7/fulcrumaxe
    DASHBOARD_TOUCHED=no SECURITY_TRIGGER_RESULT=no
    DISCUSSING_MOCK='[]'
    "HAS_LABEL_${pr}_code_review_passed=yes"
    "HAS_LABEL_${pr}_security_review_passed=yes"
    "HAS_LABEL_${pr}_acceptance_passed=yes"
    REPO_ROOT="$REPO_ROOT"
  )
  if [ "$hooks_disabled" = "1" ]; then
    env_args+=(HOOKS_DISABLED=1)
  fi

  env "${env_args[@]}" bash "$script_path" > "$out_file" 2>&1
  local rc=$?
  rm -f "$cfg" "$snap"
  _remove_pr_state_entry "$pr"
  return "$rc"
}

echo ""
echo "=== Site 2: scripts/loop-phased-step5.sh, real (HOOKS_DISABLED unset) merge with a stubbed abort ==="

SCRIPTS_COPY="$WORK/scripts-copy"
cp -r "$REPO_ROOT/scripts" "$SCRIPTS_COPY"
_stub_post_merge_hook "$SCRIPTS_COPY/post-merge-hook.sh"

_run_site2 "$SCRIPTS_COPY/loop-phased-step5.sh" 88501 88601 "0" "$WORK/site2-real.out"
RC_REAL=$?

if grep -q "Unknown argument: --bogus" "$WORK/site2-real.out"; then
  _pass "criterion 6: site2 log line contains the stubbed callee's real stderr text, cause-agnostically"
else
  _fail "criterion 6: site2 log missing the stubbed cause -- $(cat "$WORK/site2-real.out")"
fi

if grep -q "merged successfully" "$WORK/site2-real.out"; then
  _pass "criterion 7 (part 1): merging phase continued past the failed hook"
else
  _fail "criterion 7 (part 1): 'merged successfully' missing -- phase did not continue"
fi

_run_site2 "$SCRIPTS_COPY/loop-phased-step5.sh" 88502 88602 "1" "$WORK/site2-hooksdisabled.out"
RC_HOOKSDISABLED=$?

if [ "$RC_REAL" -eq "$RC_HOOKSDISABLED" ]; then
  _pass "criterion 7 (part 2): script exit status unchanged vs. a HOOKS_DISABLED=1 run ($RC_REAL)"
else
  _fail "criterion 7 (part 2): exit status differs (real=$RC_REAL, HOOKS_DISABLED=1 is $RC_HOOKSDISABLED)"
fi

echo ""
echo "--- Site 2 mutation A: restore 2>/dev/null on a copy, confirm criterion 6 goes red ---"

SCRIPTS_MUTA="$WORK/scripts-mutA"
cp -r "$SCRIPTS_COPY" "$SCRIPTS_MUTA"

python3 - "$SCRIPTS_MUTA/loop-phased-step5.sh" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    src = f.read()

start_anchor = "# D#2111: `2>/dev/null` used to discard whatever post-merge-hook.sh"
end_locator = "merge failed (rc=$MERGE_RC)"
assert src.count(start_anchor) == 1, "site2 fix start-anchor not found uniquely"
assert src.count(end_locator) == 1, "site2 end-locator not found uniquely"

start_idx = src.index(start_anchor)
locator_idx = src.index(end_locator, start_idx)
fi_marker = "\n            fi\n"
fi_idx = src.rindex(fi_marker, start_idx, locator_idx + 1)
replace_end = fi_idx + len(fi_marker)

new_span = (
    "# D#2111 mutation A: reverted to 2>/dev/null on purpose, to prove\n"
    "              # criterion 6 actually depends on capturing stderr.\n"
    '              bash "$SCRIPT_DIR/post-merge-hook.sh" \\\n'
    '                --pr "$PR_NUM" \\\n'
    '                --discussion "$DISC_NUM" \\\n'
    '                --event-id "$MERGE_EVENT_ID" 2>/dev/null || \\\n'
    '                _log "D#$DISC_NUM PR#$PR_NUM: WARNING -- post-merge-hook failed (non-fatal)"\n'
    "            fi\n"
)
mutated = src[:start_idx] + new_span + src[replace_end:]
assert mutated != src, "mutation A produced no change"
with open(path, "w") as f:
    f.write(mutated)
PYEOF

_run_site2 "$SCRIPTS_MUTA/loop-phased-step5.sh" 88503 88603 "0" "$WORK/site2-mutA.out"

if ! grep -q "Unknown argument: --bogus" "$WORK/site2-mutA.out"; then
  _pass "mutation A: criterion 6 goes red (2>/dev/null hides the stubbed cause again)"
else
  _fail "mutation A: criterion 6 did NOT go red -- cause text still present"
fi

echo ""
echo "--- Site 2 mutation B: grep for INIT_FAILED only, confirm criterion 6 goes red while criterion 1 stays green ---"

SCRIPTS_MUTB="$WORK/scripts-mutB"
cp -r "$SCRIPTS_COPY" "$SCRIPTS_MUTB"

python3 - "$SCRIPTS_MUTB/loop-phased-step5.sh" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    src = f.read()

start_anchor = "# D#2111: `2>/dev/null` used to discard whatever post-merge-hook.sh"
end_locator = "merge failed (rc=$MERGE_RC)"
assert src.count(start_anchor) == 1, "site2 fix start-anchor not found uniquely"
assert src.count(end_locator) == 1, "site2 end-locator not found uniquely"

start_idx = src.index(start_anchor)
locator_idx = src.index(end_locator, start_idx)
fi_marker = "\n            fi\n"
fi_idx = src.rindex(fi_marker, start_idx, locator_idx + 1)
replace_end = fi_idx + len(fi_marker)

# This is the anti-pattern the Spec explicitly forbids: only surface the
# cause when it happens to match the D#2105 string. Any other real abort
# (like the stubbed "Unknown argument" here) falls back to the old generic
# warning -- exactly as narrow as the thing D#2111 exists to fix.
new_span = (
    "# D#2111 mutation B: the anti-pattern the Spec forbids -- keyed to\n"
    "              # INIT_FAILED specifically instead of being cause-agnostic.\n"
    '              PMH_ERR_FILE=$(mktemp /tmp/post-merge-hook-err.XXXXXX)\n'
    '              bash "$SCRIPT_DIR/post-merge-hook.sh" \\\n'
    '                --pr "$PR_NUM" \\\n'
    '                --discussion "$DISC_NUM" \\\n'
    '                --event-id "$MERGE_EVENT_ID" 2>"$PMH_ERR_FILE"\n'
    '              PMH_RC=$?\n'
    '              if [ "$PMH_RC" -ne 0 ]; then\n'
    '                if grep -q INIT_FAILED "$PMH_ERR_FILE" 2>/dev/null; then\n'
    '                  PMH_LAST_LINE=$(tail -1 "$PMH_ERR_FILE" 2>/dev/null)\n'
    '                  _log "D#$DISC_NUM PR#$PR_NUM: WARNING -- post-merge-hook failed (exit $PMH_RC, non-fatal): ${PMH_LAST_LINE}"\n'
    "                else\n"
    '                  _log "D#$DISC_NUM PR#$PR_NUM: WARNING -- post-merge-hook failed (non-fatal)"\n'
    "                fi\n"
    "              fi\n"
    '              rm -f "$PMH_ERR_FILE"\n'
    "            fi\n"
)
mutated = src[:start_idx] + new_span + src[replace_end:]
assert mutated != src, "mutation B produced no change"
with open(path, "w") as f:
    f.write(mutated)
PYEOF

_run_site2 "$SCRIPTS_MUTB/loop-phased-step5.sh" 88504 88604 "0" "$WORK/site2-mutB.out"

if ! grep -q "Unknown argument: --bogus" "$WORK/site2-mutB.out"; then
  _pass "mutation B: criterion 6 goes red (INIT_FAILED-keyed grep misses a real, different cause)"
else
  _fail "mutation B: criterion 6 did NOT go red -- an INIT_FAILED-only grep should have missed this cause"
fi

# Criterion 1 must stay green under mutation B -- it's a site-1 check and
# mutation B only touches site 2's script, so re-run the (already-verified)
# site-1 assertion once more here as the explicit isolation proof the Spec
# asks for.
_run_site1 "$REPO_ROOT/scripts/subagent-stop-hook.sh" "$EVENT_ID_C1" 88601 "c1b" \
  "$WORK/c1b.out" "$WORK/c1b.err"
SINK_C1B=$(_extract_log_path "$WORK/c1b.err")
if [ -n "$SINK_C1B" ] && [ -f "$SINK_C1B" ] && grep -q "INIT_FAILED: failed to open lock fd" "$SINK_C1B"; then
  _pass "mutation B isolation: criterion 1 (site 1, untouched by mutation B) stays green"
else
  _fail "mutation B isolation: criterion 1 unexpectedly broke -- sink '$SINK_C1B'"
fi

echo ""
echo "=== test_hook_caller_failure_surfacing.sh: $PASS passed, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
