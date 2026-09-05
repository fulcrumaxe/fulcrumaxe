#!/usr/bin/env bash
# tests/test_worktree_cap_alarm.sh
# End-to-end regression test for the worktree-cap guard fixed alongside
# D#2059's PR: the folded rotate-team-log.sh/emit_spawn_block continuation
# (scripts/pre-spawn-check.sh:286-289, 331-334, 359-362, 741-744), the
# stripped emit_spawn_block body (217-232), and the switch from the empty
# worktree registry to a disk-based count (scripts/lib/worktree-registry.sh
# count-disk).
#
# Every check below also runs its own mutation against a scratch copy of the
# production script and asserts the check goes red -- a test that has not
# been observed failing has not been shown to test anything.
#
# HARD RULE: UNDER NO CIRCUMSTANCES may this test invoke `claude`, `claude -p`,
# `_start_loop_run`, or trigger /loop. Block conditions are simulated via
# mock scripts and a sandboxed copy of pre-spawn-check.sh, never the real one
# against real Discussion state.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"

PASS=0
FAIL=0

ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

TMPDIR_BASE=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_BASE"; }
trap cleanup EXIT

make_workspace() {
  local ws
  ws=$(mktemp -d "$TMPDIR_BASE/ws-XXXXXX")
  mkdir -p "$ws/.autonomous-team" "$ws/backend" "$ws/scripts/lib"
  echo "$ws"
}

# ── Sandbox installer (same technique as test_pre_spawn_check_block_events.sh) ─
# install_sandbox copies the PRODUCTION script by default; install_sandbox_from
# copies an arbitrary path instead, so mutation runs never touch the real file.
install_sandbox_from() {
  local ws="$1" psc_src="$2"
  mkdir -p "$ws/.claude/worktrees" "$ws/scripts/lib" "$ws/backend" "$ws/.autonomous-team"
  cp "$psc_src" "$ws/scripts/pre-spawn-check.sh"
  cp "$SCRIPTS_DIR/agent-feed-append.sh" "$ws/scripts/agent-feed-append.sh"
  cp "$SCRIPTS_DIR/lib/state-dir.sh" "$ws/scripts/lib/state-dir.sh"
  cp "$SCRIPTS_DIR/lib/hook-event.sh" "$ws/scripts/lib/hook-event.sh"
  cp "$SCRIPTS_DIR/lib/worktree-registry.sh" "$ws/scripts/lib/worktree-registry.sh"
  cp "$REPO_ROOT/backend/agent_feed.py" "$ws/backend/agent_feed.py"

  cat > "$ws/scripts/rotate-team-log.sh" << 'STUBEOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "${ROTATE_LOG_CAPTURE:-/dev/null}"
exit 0
STUBEOF
  chmod +x "$ws/scripts/rotate-team-log.sh"

  cat > "$ws/scripts/lib/external_intake_gate.py" << 'STUBEOF'
#!/usr/bin/env python3
import json
print(json.dumps({"blocked": False, "reason": "test_stub_always_allow"}))
STUBEOF
  chmod +x "$ws/scripts/lib/external_intake_gate.py"
}

install_sandbox() { install_sandbox_from "$1" "$SCRIPTS_DIR/pre-spawn-check.sh"; }

# Runs the sandboxed pre-spawn-check.sh with --isolation worktree, --no-register
# and a unique --event-id. $2 is the WORKTREE_CAP value.
run_psc_worktree() {
  local ws="$1" cap="$2"
  local rc=0
  ROTATE_LOG_CAPTURE="$ws/rotate-team-log.captured" WORKTREE_CAP="$cap" \
    bash "$ws/scripts/pre-spawn-check.sh" --role executor --discussion 999 \
      --isolation worktree --no-register --event-id "evt-$$-$RANDOM" \
    > "$ws/psc.stdout" 2> "$ws/psc.stderr" || rc=$?
  echo "$rc" > "$ws/psc.exit"
}

psc_exit() { cat "$1/psc.exit"; }
psc_log()  { cat "$1/rotate-team-log.captured" 2>/dev/null || true; }

count_blocked() {
  local feed="$1" reason="$2"
  python3 -c "
import json
count = 0
try:
    with open('$feed') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get('event_type') == 'spawn_blocked' and d.get('reason') == '$reason':
                    count += 1
            except Exception:
                pass
except FileNotFoundError:
    pass
print(count)
" 2>/dev/null || echo "0"
}

make_n_worktree_dirs() {
  local ws="$1" n="$2"
  for i in $(seq 1 "$n"); do
    mkdir -p "$ws/.claude/worktrees/agent-$i"
  done
}

echo "=== test_worktree_cap_alarm ==="
echo ""

# ═════════════════════════════════════════════════════════════════════════
# 1. bash -n sanity (Spec item 1)
# ═════════════════════════════════════════════════════════════════════════
echo "--- Item 1: bash -n ---"
WCA_SYNTAX_ERR="$(mktemp /tmp/test_worktree_cap_alarm.XXXXXX)"
if bash -n "$SCRIPTS_DIR/pre-spawn-check.sh" 2>"$WCA_SYNTAX_ERR"; then
  ok "Item1: pre-spawn-check.sh passes bash -n"
else
  fail "Item1: bash -n failed: $(cat "$WCA_SYNTAX_ERR")"
fi
rm -f "$WCA_SYNTAX_ERR"
echo ""

# ═════════════════════════════════════════════════════════════════════════
# 2. Line-pair scan for the folded continuation (Spec item 2)
# A plain grep can't see this defect -- it spans two lines: a line ending in
# a trailing backslash, immediately followed by a line starting with
# "emit_spawn_block". That pattern turns emit_spawn_block and its arguments
# into arguments of the *previous* line's command instead of a function call.
# ═════════════════════════════════════════════════════════════════════════
echo "--- Item 2: folded-continuation line-pair scan ---"

scan_folded_continuation() {
  python3 - "$1" <<'PYEOF'
import sys
ls = open(sys.argv[1]).read().split('\n')
BS = chr(92)   # spelled this way so no shell quoting layer can eat it
bad = [i + 1 for i in range(len(ls) - 1)
       if ls[i].rstrip().endswith(BS)
       and ls[i + 1].strip().startswith('emit_spawn_block')]
print(len(bad), bad)
PYEOF
}

SCAN_RESULT=$(scan_folded_continuation "$SCRIPTS_DIR/pre-spawn-check.sh")
if [[ "$SCAN_RESULT" == "0 []" ]]; then
  ok "Item2: no folded emit_spawn_block continuations remain ($SCAN_RESULT)"
else
  fail "Item2: found folded continuations: $SCAN_RESULT"
fi

# Sanity check on the checker itself against unmodified HEAD bac3c3d1 -- the
# Spec states this must print "4 [286, 331, 359, 741]"; if it doesn't, the
# checker is not measuring what we think it is.
HEAD_COPY="$TMPDIR_BASE/head-pre-spawn-check.sh"
if git -C "$REPO_ROOT" show bac3c3d1:scripts/pre-spawn-check.sh > "$HEAD_COPY" 2>/dev/null; then
  HEAD_SCAN=$(scan_folded_continuation "$HEAD_COPY")
  if [[ "$HEAD_SCAN" == "4 [286, 331, 359, 741]" ]]; then
    ok "Item2-baseline: scanner reports 4 [286, 331, 359, 741] against HEAD bac3c3d1 (matches Spec measurement)"
  else
    fail "Item2-baseline: scanner reported '$HEAD_SCAN' against HEAD bac3c3d1, expected '4 [286, 331, 359, 741]'"
  fi
else
  echo "  [SKIP] Item2-baseline: bac3c3d1 not reachable in this checkout (shallow clone?)"
fi

# Mutation: restore the trailing backslash on one of the four call sites and
# confirm the scanner goes from 0 to 1.
MUT2_COPY="$TMPDIR_BASE/mut2-pre-spawn-check.sh"
cp "$SCRIPTS_DIR/pre-spawn-check.sh" "$MUT2_COPY"
python3 - "$MUT2_COPY" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read()
old = '''      emit_spawn_block "worktree_cap_reached" "worktree cap (${WORKTREE_CAP_VAL}) reached" "{\\"active_worktrees\\":${ACTIVE_WORKTREES},\\"cap\\":${WORKTREE_CAP_VAL}}"
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \\
        "[$(date +%H:%M)] team-lead: WARNING — worktree cap (${WORKTREE_CAP_VAL}) reached, deferring spawn for ${ROLE}" \\
        2>/dev/null || true'''
new = '''      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \\
      emit_spawn_block "worktree_cap_reached" "worktree cap (${WORKTREE_CAP_VAL}) reached" "{\\"active_worktrees\\":${ACTIVE_WORKTREES},\\"cap\\":${WORKTREE_CAP_VAL}}"
        "[$(date +%H:%M)] team-lead: WARNING — worktree cap (${WORKTREE_CAP_VAL}) reached, deferring spawn for ${ROLE}" \\
        2>/dev/null || true'''
assert old in text, "fixture pattern not found -- has the worktree-cap block changed shape?"
open(path, 'w').write(text.replace(old, new))
PYEOF
MUT2_SCAN=$(scan_folded_continuation "$MUT2_COPY")
if [[ "$MUT2_SCAN" == 1\ * ]]; then
  ok "Item2-mutation: re-folding one call site makes the scanner report 1 match ($MUT2_SCAN)"
else
  fail "Item2-mutation: expected '1 [<line>]' after re-folding, got '$MUT2_SCAN'"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# 3. Disk-based count (Spec item 4)
# ═════════════════════════════════════════════════════════════════════════
echo "--- Item 3 (Spec item 4): disk-based worktree count ---"

ws_count=$(make_workspace)
echo "[]" > "$ws_count/.autonomous-team/worktrees.json"
make_n_worktree_dirs "$ws_count" 3
cp "$SCRIPTS_DIR/lib/worktree-registry.sh" "$ws_count/scripts/lib/worktree-registry.sh"

DISK_COUNT=$(bash "$ws_count/scripts/lib/worktree-registry.sh" count-disk)
if [[ "$DISK_COUNT" == "3" ]]; then
  ok "Item3: count-disk returns 3 for 3 worktree dirs with an empty registry"
else
  fail "Item3: count-disk expected 3, got '$DISK_COUNT'"
fi

# Comparison proof (the mutation the Spec describes for this item): on the
# exact same fixture, the OLD source of truth -- the registry -- still
# returns 0, because nothing in this repo ever populates it. This is why
# disk, not the registry, must be authoritative.
REGISTRY_COUNT=$(bash "$ws_count/scripts/lib/worktree-registry.sh" count-active)
if [[ "$REGISTRY_COUNT" == "0" ]]; then
  ok "Item3-mutation: the same fixture's count-active (the old source) still reads 0 -- proves disk, not the registry, is what makes this correct"
else
  fail "Item3-mutation: expected count-active to read 0 on this fixture (demonstrating the old bug), got '$REGISTRY_COUNT'"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# 4. Over-cap: warned, logged, and allowed through (Spec item 5, AMENDED)
#
# D#2059's Spec amendment supersedes the original item 5: the process must
# now exit 0 (not 1) when over cap. Enforcement is deferred until D#2001
# (a working reaper) and D#2097 (what the threshold should even count) both
# land -- until then this is a warning, not a block. The conjunction still
# matters: exit-0 alone would also be satisfied by a script that never
# entered the cap branch at all, so the feed row and log text are required
# together with the exit code, not instead of it.
# ═════════════════════════════════════════════════════════════════════════
echo "--- Item 4 (Spec item 5, amended): over-cap is warned, logged, and allowed through ---"

ws5=$(make_workspace)
install_sandbox "$ws5"
make_n_worktree_dirs "$ws5" 3
FEED5="$ws5/.autonomous-team/agent-feed.jsonl"

run_psc_worktree "$ws5" 2
EXIT5=$(psc_exit "$ws5")

if [[ "$EXIT5" -eq 0 ]]; then
  ok "Item4a: process exits 0 when 3 worktrees exceed cap=2 (enforcement deferred, D#2059 amendment)"
else
  fail "Item4a: expected exit 0 (enforcement is deferred), got $EXIT5"
fi

CAP_COUNT5=$(count_blocked "$FEED5" "worktree_cap_reached")
if [[ "$CAP_COUNT5" -ge 1 ]]; then
  ok "Item4b: spawn_blocked row with reason=worktree_cap_reached appears in the workspace feed"
else
  fail "Item4b: no spawn_blocked row found in the workspace feed"
fi

LAST5=$(python3 -c "
import json
events = []
try:
    with open('$FEED5') as f:
        for line in f:
            d = json.loads(line)
            if d.get('event_type') == 'spawn_blocked':
                events.append(d)
except FileNotFoundError:
    pass
print(json.dumps(events[-1]) if events else '{}')
")
CAP5=$(python3 -c "import json; print(json.loads('$LAST5').get('details',{}).get('cap',''))" 2>/dev/null || echo "")
if [[ "$CAP5" == "2" ]]; then
  ok "Item4b-details: details.cap==2"
else
  fail "Item4b-details: details.cap expected 2, got '$CAP5'"
fi

LOG5=$(psc_log "$ws5")
if echo "$LOG5" | grep -q "worktree cap (2) reached" && echo "$LOG5" | grep -q "deferring spawn for"; then
  ok "Item4c: rotate-team-log received 'worktree cap (2) reached' and 'deferring spawn for'"
else
  fail "Item4c: rotate-team-log text missing expected substrings: $LOG5"
fi
if echo "$LOG5" | grep -q "emit_spawn_block"; then
  fail "Item4c: rotate-team-log capture contains the literal string 'emit_spawn_block'"
else
  ok "Item4c: rotate-team-log capture does not contain the literal string 'emit_spawn_block'"
fi

# ── Mutation A: restore the trailing backslash at the worktree-cap call site.
# Both the feed assertion and the log-text assertion must fail.
echo ""
echo "  -- Mutation A: re-fold the worktree_cap_reached call site --"
ws5a=$(make_workspace)
MUTA_SRC="$TMPDIR_BASE/muta-pre-spawn-check.sh"
cp "$SCRIPTS_DIR/pre-spawn-check.sh" "$MUTA_SRC"
python3 - "$MUTA_SRC" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read()
old = '''      emit_spawn_block "worktree_cap_reached" "worktree cap (${WORKTREE_CAP_VAL}) reached" "{\\"active_worktrees\\":${ACTIVE_WORKTREES},\\"cap\\":${WORKTREE_CAP_VAL}}"
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \\
        "[$(date +%H:%M)] team-lead: WARNING — worktree cap (${WORKTREE_CAP_VAL}) reached, deferring spawn for ${ROLE}" \\
        2>/dev/null || true'''
new = '''      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \\
      emit_spawn_block "worktree_cap_reached" "worktree cap (${WORKTREE_CAP_VAL}) reached" "{\\"active_worktrees\\":${ACTIVE_WORKTREES},\\"cap\\":${WORKTREE_CAP_VAL}}"
        "[$(date +%H:%M)] team-lead: WARNING — worktree cap (${WORKTREE_CAP_VAL}) reached, deferring spawn for ${ROLE}" \\
        2>/dev/null || true'''
assert old in text
open(path, 'w').write(text.replace(old, new))
PYEOF
install_sandbox_from "$ws5a" "$MUTA_SRC"
make_n_worktree_dirs "$ws5a" 3
FEED5A="$ws5a/.autonomous-team/agent-feed.jsonl"
run_psc_worktree "$ws5a" 2
CAP_COUNT5A=$(count_blocked "$FEED5A" "worktree_cap_reached")
LOG5A=$(psc_log "$ws5a")
MUTA_FEED_OK="false"; [[ "$CAP_COUNT5A" -ge 1 ]] && MUTA_FEED_OK="true"
MUTA_LOG_OK="false"
if echo "$LOG5A" | grep -q "worktree cap (2) reached" && echo "$LOG5A" | grep -q "deferring spawn for" && ! echo "$LOG5A" | grep -q "emit_spawn_block"; then
  MUTA_LOG_OK="true"
fi
if [[ "$MUTA_FEED_OK" == "false" && "$MUTA_LOG_OK" == "false" ]]; then
  ok "Item4-mutationA: re-folding the call site breaks BOTH the feed assertion and the log-text assertion, as required"
else
  fail "Item4-mutationA: expected both assertions to fail after re-folding (feed_ok=$MUTA_FEED_OK log_ok=$MUTA_LOG_OK)"
fi

# ── Mutation B: blank the emit_spawn_block body back to the shipped bug.
# Only the feed assertion must fail; the log-text call is untouched.
echo ""
echo "  -- Mutation B: blank the emit_spawn_block body --"
ws5b=$(make_workspace)
MUTB_SRC="$TMPDIR_BASE/mutb-pre-spawn-check.sh"
cp "$SCRIPTS_DIR/pre-spawn-check.sh" "$MUTB_SRC"
python3 - "$MUTB_SRC" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read()
old = '''emit_spawn_block() {
  local reason="$1"
  local message="$2"
  local details_json="$3"
  [[ -z "$details_json" ]] && details_json="{}"
  [[ "$DRY_RUN" == "1" ]] && return 0
  local disc_args=()
  [[ -n "$DISCUSSION" ]] && disc_args=(--discussion "$DISCUSSION")
  bash "$SCRIPT_DIR/agent-feed-append.sh" \\
    --role "$ROLE" \\
    --event-type "spawn_blocked" \\
    --reason "$reason" \\
    --message "blocked $ROLE: $message" \\
    "${disc_args[@]}" \\
    --details "$details_json" \\
    2>/dev/null || true
}'''
new = '''emit_spawn_block() {
  local reason=""
  local message=""
  local details_json="\\{}"
  [[ "" == "1" ]] && return 0
  local disc_args=()
  [[ -n "" ]] && disc_args=(--discussion "")
  bash "\\/agent-feed-append.sh"     --role ""     --event-type "spawn_blocked"     --reason ""     --message "blocked \\: "     ""     --details ""     2>/dev/null || true
}'''
assert old in text
open(path, 'w').write(text.replace(old, new))
PYEOF
install_sandbox_from "$ws5b" "$MUTB_SRC"
make_n_worktree_dirs "$ws5b" 3
FEED5B="$ws5b/.autonomous-team/agent-feed.jsonl"
run_psc_worktree "$ws5b" 2
CAP_COUNT5B=$(count_blocked "$FEED5B" "worktree_cap_reached")
LOG5B=$(psc_log "$ws5b")
if [[ "$CAP_COUNT5B" -eq 0 ]]; then
  ok "Item4-mutationB: blanking emit_spawn_block's body breaks the feed assertion (no row appended)"
else
  fail "Item4-mutationB: expected no spawn_blocked row after blanking emit_spawn_block, found $CAP_COUNT5B"
fi
if echo "$LOG5B" | grep -q "worktree cap (2) reached" && echo "$LOG5B" | grep -q "deferring spawn for"; then
  ok "Item4-mutationB: the log-text assertion still passes (rotate-team-log call is a separate, unaffected statement)"
else
  fail "Item4-mutationB: log-text assertion unexpectedly broke too: $LOG5B"
fi

# ── Mutation C: the tautology guard the amendment requires. Force the
# comparison to a threshold that can never be met (-ge 999999). The process
# still exits 0 -- so a bare "does not exit 1" criterion would still pass --
# but the feed row and log text are both absent because the cap branch is
# never entered. The conjunctive criterion (exit-0 AND feed AND log) must
# fail here, or it is asserting nothing.
echo ""
echo "  -- Mutation C: tautology guard, cap forced unreachable (-ge 999999) --"
ws5c=$(make_workspace)
MUTC5_SRC="$TMPDIR_BASE/mutc5-pre-spawn-check.sh"
cp "$SCRIPTS_DIR/pre-spawn-check.sh" "$MUTC5_SRC"
python3 - "$MUTC5_SRC" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read()
old = 'if [[ "$ACTIVE_WORKTREES" -ge "$WORKTREE_CAP_VAL" ]] 2>/dev/null; then'
new = 'if [[ "$ACTIVE_WORKTREES" -ge 999999 ]] 2>/dev/null; then'
assert text.count(old) == 1, text.count(old)
open(path, 'w').write(text.replace(old, new))
PYEOF
install_sandbox_from "$ws5c" "$MUTC5_SRC"
make_n_worktree_dirs "$ws5c" 3
FEED5C="$ws5c/.autonomous-team/agent-feed.jsonl"
run_psc_worktree "$ws5c" 2
EXIT5C=$(psc_exit "$ws5c")
CAP_COUNT5C=$(count_blocked "$FEED5C" "worktree_cap_reached")
LOG5C=$(psc_log "$ws5c")
MUTC5_EXIT_ALONE_WOULD_PASS="false"; [[ "$EXIT5C" -eq 0 ]] && MUTC5_EXIT_ALONE_WOULD_PASS="true"
MUTC5_CONJUNCTION_HOLDS="true"
[[ "$EXIT5C" -eq 0 ]] || MUTC5_CONJUNCTION_HOLDS="false"
[[ "$CAP_COUNT5C" -ge 1 ]] || MUTC5_CONJUNCTION_HOLDS="false"
if echo "$LOG5C" | grep -q "worktree cap (2) reached" && echo "$LOG5C" | grep -q "deferring spawn for"; then
  :
else
  MUTC5_CONJUNCTION_HOLDS="false"
fi
if [[ "$MUTC5_EXIT_ALONE_WOULD_PASS" == "true" && "$MUTC5_CONJUNCTION_HOLDS" == "false" ]]; then
  ok "Item4-mutationC: a bare exit-0 check would have passed (exit=$EXIT5C), but the conjunctive criterion correctly fails (feed rows=$CAP_COUNT5C, log missing the cap text) -- the tautology guard holds"
else
  fail "Item4-mutationC: expected exit-0-alone=true and conjunction=false, got exit_alone=$MUTC5_EXIT_ALONE_WOULD_PASS conjunction=$MUTC5_CONJUNCTION_HOLDS"
fi

# ── Mutation D: re-insert `exit 1` after the warning. The exit-0 assertion
# must fail -- this is the direct check that the amendment's deferral (not
# the earlier block-and-report behavior) is what actually ships.
echo ""
echo "  -- Mutation D: re-insert exit 1 after the warning --"
ws5d=$(make_workspace)
MUTD5_SRC="$TMPDIR_BASE/mutd5-pre-spawn-check.sh"
cp "$SCRIPTS_DIR/pre-spawn-check.sh" "$MUTD5_SRC"
python3 - "$MUTD5_SRC" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read()
old = '''echo "WARNING: worktree cap ($WORKTREE_CAP_VAL) reached — $ACTIVE_WORKTREES active. Allowing spawn of $ROLE; enforcement deferred (see D#2059)." >&2
    fi'''
new = '''echo "WARNING: worktree cap ($WORKTREE_CAP_VAL) reached — $ACTIVE_WORKTREES active. Allowing spawn of $ROLE; enforcement deferred (see D#2059)." >&2
      exit 1
    fi'''
assert text.count(old) == 1, text.count(old)
open(path, 'w').write(text.replace(old, new))
PYEOF
install_sandbox_from "$ws5d" "$MUTD5_SRC"
make_n_worktree_dirs "$ws5d" 3
run_psc_worktree "$ws5d" 2
EXIT5D=$(psc_exit "$ws5d")
if [[ "$EXIT5D" -ne 0 ]]; then
  ok "Item4-mutationD: re-inserting exit 1 breaks the exit-0 assertion (exit=$EXIT5D, expected 0)"
else
  fail "Item4-mutationD: expected exit 1 after re-inserting the old block, got 0"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# 5. Under-cap: not blocked, and silent (Spec item 6)
# ═════════════════════════════════════════════════════════════════════════
echo "--- Item 5 (Spec item 6): under-cap is silent ---"

ws6=$(make_workspace)
install_sandbox "$ws6"
make_n_worktree_dirs "$ws6" 3
FEED6="$ws6/.autonomous-team/agent-feed.jsonl"

run_psc_worktree "$ws6" 8
EXIT6=$(psc_exit "$ws6")

if [[ "$EXIT6" -eq 0 ]]; then
  ok "Item5a: process exits 0 when 3 worktrees are under cap=8"
else
  fail "Item5a: expected exit 0, got $EXIT6"
fi

CAP_COUNT6=$(count_blocked "$FEED6" "worktree_cap_reached")
if [[ "$CAP_COUNT6" -eq 0 ]]; then
  ok "Item5b: no spawn_blocked row added under cap"
else
  fail "Item5b: expected no spawn_blocked row, found $CAP_COUNT6"
fi

LOG6=$(psc_log "$ws6")
if echo "$LOG6" | grep -q "deferring spawn for"; then
  fail "Item5c: rotate-team-log was invoked with a cap warning under cap"
else
  ok "Item5c: rotate-team-log was not invoked with a cap warning"
fi

# Mutation: change the cap comparison to "-ge 0" (always true).
#
# Re-pointed by the D#2059 Spec amendment: since enforcement no longer
# `exit 1`s, forcing the comparison to always-true no longer changes the
# exit code -- the mutant still exits 0, same as the correct behavior, so
# an exit-code-only assertion here would silently stop detecting anything
# (exactly the "check whose green result means less than its name implies"
# shape the whole Discussion is about, and it would have been introduced
# BY this fix). The criterion is re-pointed at the observable that still
# exists: under this mutation, the previously-silent under-cap workspace
# must now show a spawn_blocked row and a cap warning in the log.
echo ""
echo "  -- Mutation: cap comparison forced to always-true (-ge 0) --"
ws6m=$(make_workspace)
MUT_UNDERCAP_SRC="$TMPDIR_BASE/mut-undercap-pre-spawn-check.sh"
cp "$SCRIPTS_DIR/pre-spawn-check.sh" "$MUT_UNDERCAP_SRC"
python3 - "$MUT_UNDERCAP_SRC" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read()
old = 'if [[ "$ACTIVE_WORKTREES" -ge "$WORKTREE_CAP_VAL" ]] 2>/dev/null; then'
new = 'if [[ "$ACTIVE_WORKTREES" -ge 0 ]] 2>/dev/null; then'
assert text.count(old) == 1, text.count(old)
open(path, 'w').write(text.replace(old, new))
PYEOF
install_sandbox_from "$ws6m" "$MUT_UNDERCAP_SRC"
make_n_worktree_dirs "$ws6m" 3
FEED6M="$ws6m/.autonomous-team/agent-feed.jsonl"
run_psc_worktree "$ws6m" 8
EXIT6M=$(psc_exit "$ws6m")
CAP_COUNT6M=$(count_blocked "$FEED6M" "worktree_cap_reached")
LOG6M=$(psc_log "$ws6m")
MUT_UNDERCAP_STILL_SILENT="true"
[[ "$CAP_COUNT6M" -eq 0 ]] || MUT_UNDERCAP_STILL_SILENT="false"
echo "$LOG6M" | grep -q "deferring spawn for" && MUT_UNDERCAP_STILL_SILENT="false"
if [[ "$EXIT6M" -eq 0 && "$MUT_UNDERCAP_STILL_SILENT" == "false" ]]; then
  ok "Item5-mutation: forcing the comparison to always-true still exits 0 (exit=$EXIT6M, same as correct behavior) but now emits a spawn_blocked row and a cap warning -- the silent-guard criterion correctly fails on the feed/log observable"
elif [[ "$EXIT6M" -ne 0 ]]; then
  fail "Item5-mutation: expected exit 0 (enforcement is deferred) even under this mutation, got $EXIT6M"
else
  fail "Item5-mutation: expected a spawn_blocked row and cap warning under this mutation (feed rows=$CAP_COUNT6M), but the under-cap workspace stayed silent -- the mutation went undetected"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════════
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
