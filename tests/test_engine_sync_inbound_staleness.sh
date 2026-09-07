#!/usr/bin/env bash
# tests/test_engine_sync_inbound_staleness.sh
#
# Acceptance tests for the engine-sync inbound alarm: the staleness check
# itself, its edge-triggered notification, its grace window, and the point
# at which loop-preflight stops the loop over it.
#
# Hermetic: builds its own throwaway "public" bare repo and a "work" clone
# with a remote literally named `code-plane`, so it never touches this
# repo's real `code-plane` remote or its real `refs/synced/code-plane`.
#
# Every assertion here is behavioral (parsed JSON fields, exit codes,
# counted notifications) -- never a grep over source (D#2377).

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STALENESS="$REPO_DIR/scripts/engine-sync/inbound/staleness.sh"
ALARM="$REPO_DIR/scripts/engine-sync/inbound/alarm.sh"
PREFLIGHT="$REPO_DIR/scripts/loop-preflight.sh"

PASS=0
FAIL=0
FAILED_NAMES=()

check() {
  local name="$1" ok="$2"
  if [ "$ok" = "0" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    FAILED_NAMES+=("$name")
    echo "FAIL: $name" >&2
  fi
}

json_field() {
  # json_field <json-line> <key> -- prints the value, or nothing on parse error.
  python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], ""))
except Exception:
    print("")
' "$2" <<<"$1"
}

TMP_ROOT="$(mktemp -d)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

# --- Fixture: a bare "public" repo with 3 commits: seed, +1 (c1), +2 (c2) ---
PUBLIC="$TMP_ROOT/scratch-public"
BUILD="$TMP_ROOT/scratch-build"
git init -q --bare "$PUBLIC"
git init -q "$BUILD"
git -C "$BUILD" config user.email t@example.com
git -C "$BUILD" config user.name t
echo seed >"$BUILD/f.txt"
git -C "$BUILD" add f.txt
git -C "$BUILD" commit -q -m seed
SEED_SHA="$(git -C "$BUILD" rev-parse HEAD)"
echo c1 >>"$BUILD/f.txt"
git -C "$BUILD" commit -q -am c1
echo c2 >>"$BUILD/f.txt"
git -C "$BUILD" commit -q -am c2
TIP_SHA="$(git -C "$BUILD" rev-parse HEAD)"
git -C "$BUILD" push -q "$PUBLIC" HEAD:refs/heads/main

# --- Scratch work repo: a clone with a remote literally named "code-plane" ---
WORK="$TMP_ROOT/scratch-work"
git clone -q "$PUBLIC" "$WORK"
git -C "$WORK" remote rename origin code-plane

run_staleness() {
  ENGINE_SYNC_GIT_DIR="$WORK" bash "$STALENESS"
}

set_marker() {
  git -C "$WORK" update-ref refs/synced/code-plane "$1"
}

# ---------------------------------------------------------------------------
# A1: marker == current tip -> in-sync, behind 0, exit 0
# ---------------------------------------------------------------------------
set_marker "$TIP_SHA"
OUT="$(run_staleness)"
RC=$?
STATUS="$(json_field "$OUT" status)"
BEHIND="$(json_field "$OUT" behind)"
[ "$RC" = "0" ] && [ "$STATUS" = "in-sync" ] && [ "$BEHIND" = "0" ]
check "A1 marker==tip -> in-sync/behind=0/exit0" $?

# ---------------------------------------------------------------------------
# A2: negative -- marker at seed -> stale, behind 2, exit 1 (parsed integer)
# ---------------------------------------------------------------------------
set_marker "$SEED_SHA"
OUT="$(run_staleness)"
RC=$?
STATUS="$(json_field "$OUT" status)"
BEHIND="$(json_field "$OUT" behind)"
[ "$RC" = "1" ] && [ "$STATUS" = "stale" ] && [ "$BEHIND" = "2" ]
check "A2 marker=seed -> stale/behind=2 (int)/exit1" $?

# ---------------------------------------------------------------------------
# A3: negative -- remote unreachable fails closed: exit 2, undecidable, NEVER
# exit 0 / in-sync.
# ---------------------------------------------------------------------------
git -C "$WORK" remote set-url code-plane "$TMP_ROOT/does-not-exist-repo"
OUT="$(run_staleness)"
RC=$?
STATUS="$(json_field "$OUT" status)"
[ "$RC" = "2" ] && [ "$STATUS" = "undecidable" ] && [ "$RC" != "0" ] && [ "$STATUS" != "in-sync" ]
check "A3 unreachable remote -> undecidable/exit2, never in-sync" $?
git -C "$WORK" remote set-url code-plane "$PUBLIC"

# ---------------------------------------------------------------------------
# A4: loop-preflight.sh emits an engine_sync object carrying .status, AND
# still exits 0 while that status is "stale". One stale observation is loud
# and does not stop the loop -- the grace window further down is what
# eventually does, and asserting both halves is what stops a change from
# flipping one without the other.
# ---------------------------------------------------------------------------
set_marker "$SEED_SHA" # stale
PREFLIGHT_STATE_DIR="$(mktemp -d)"
PREFLIGHT_OUT="$(AUTONOMOUS_TEAM_STATE_DIR="$PREFLIGHT_STATE_DIR" ENGINE_SYNC_GIT_DIR="$WORK" ENGINE_SYNC_STATE_DIR="$PREFLIGHT_STATE_DIR" bash "$PREFLIGHT" 2>"$TMP_ROOT/preflight.stderr")"
PREFLIGHT_RC=$?
rm -rf "$PREFLIGHT_STATE_DIR"
ES_STATUS="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get("engine_sync", {}).get("status", ""))
except Exception:
    print("")
' "$PREFLIGHT_OUT" 2>/dev/null)"
[ "$PREFLIGHT_RC" = "0" ] && [ "$ES_STATUS" = "stale" ]
check "A4 loop-preflight: engine_sync.status=stale AND exit0" $?
if [ "$ES_STATUS" != "stale" ]; then
  echo "  (preflight stderr follows for diagnosis)" >&2
  tail -20 "$TMP_ROOT/preflight.stderr" >&2
fi

# ---------------------------------------------------------------------------
# A5: negative -- the alarm is edge-triggered. Two consecutive stale runs
# post exactly one notification; the second posts zero. A third run, after
# a transition back to in-sync and out again, posts one more.
# ---------------------------------------------------------------------------
NOTIFY_STATE_DIR="$TMP_ROOT/notify-state"
mkdir -p "$NOTIFY_STATE_DIR"
NOTIFY_LOG="$TMP_ROOT/notify.log"
NOTIFY_SINK="$TMP_ROOT/fake-notify.sh"
cat >"$NOTIFY_SINK" <<EOF
#!/usr/bin/env bash
echo "notified: \$1" >> "$NOTIFY_LOG"
EOF
chmod +x "$NOTIFY_SINK"

run_alarm() {
  ENGINE_SYNC_GIT_DIR="$WORK" ENGINE_SYNC_STATE_DIR="$NOTIFY_STATE_DIR" \
    ENGINE_SYNC_NOTIFY_CMD="$NOTIFY_SINK" bash "$ALARM" >/dev/null
}

count_notifications() {
  [ -f "$NOTIFY_LOG" ] && wc -l <"$NOTIFY_LOG" || echo 0
}

set_marker "$SEED_SHA" # stale
run_alarm
run_alarm
COUNT="$(count_notifications)"
[ "$COUNT" = "1" ]
check "A5 two consecutive stale runs -> exactly one notification" $?

set_marker "$TIP_SHA" # back to in-sync
run_alarm
COUNT="$(count_notifications)"
[ "$COUNT" = "1" ]
check "A5 recovery to in-sync posts none" $?

set_marker "$SEED_SHA" # stale again
run_alarm
COUNT="$(count_notifications)"
[ "$COUNT" = "2" ]
check "A5 re-entering stale after recovery posts one more" $?

# ---------------------------------------------------------------------------
# Negative -- production staleness and synthetic staleness arrive from
# OPPOSITE directions, and only one of them was ever tested.
#
# Every staleness assertion above moves the MARKER backwards, which points
# it at an object that is already local. The real thing moves the REMOTE
# forwards, at which point the tip's sha is known (ls-remote returns it) but
# its object is not present -- and the check reported `undecidable` for
# exactly as long as that lasted, i.e. from every merge until something else
# happened to fetch. That is the window the alarm exists to cover.
#
# This builds that case honestly: commit to the public repo WITHOUT
# fetching, leave the marker where it is, and assert the check resolves it.
# ---------------------------------------------------------------------------
echo c3 >>"$BUILD/f.txt"
git -C "$BUILD" commit -q -am c3
AHEAD_SHA="$(git -C "$BUILD" rev-parse HEAD)"
git -C "$BUILD" push -q "$PUBLIC" HEAD:refs/heads/main

set_marker "$TIP_SHA"   # the marker is where the last run left it; the REMOTE moved

# First, prove the old failure mode is real and is what the fetch fixes: with
# fetching suppressed, the remote tip's object is genuinely absent and the
# only honest answer is undecidable -- never in-sync.
OUT="$(ENGINE_SYNC_NO_FETCH=1 ENGINE_SYNC_GIT_DIR="$WORK" bash "$STALENESS")"
RC=$?
STATUS="$(json_field "$OUT" status)"
[ "$RC" = "2" ] && [ "$STATUS" = "undecidable" ]
check "remote ahead, nothing fetched, fetch suppressed -> undecidable (never in-sync)" $?

# Now the real path: the check fetches the one ref it compares against, so a
# remote that has just moved ahead is decidable immediately.
OUT="$(run_staleness)"
RC=$?
STATUS="$(json_field "$OUT" status)"
BEHIND="$(json_field "$OUT" behind)"
[ "$RC" = "1" ] && [ "$STATUS" = "stale" ] && [ "$BEHIND" = "1" ]
check "remote ahead, nothing fetched -> stale/behind=1 (the fetch makes it decidable)" $?

# ---------------------------------------------------------------------------
# The grace window, and the flip that makes it blocking.
#
# Paired with A4 above, which asserted the SAME condition (marker stale)
# still exits 0. Neither is meaningful alone: A4 says one stale observation
# does not stop the loop, and the two below say the Nth one does. A change
# that flips one without the other fails here.
# ---------------------------------------------------------------------------
GRACE_STATE_DIR="$TMP_ROOT/grace-state"
mkdir -p "$GRACE_STATE_DIR"

run_preflight_grace() {
  # run_preflight_grace <threshold> -- returns preflight's exit code, and
  # leaves its stdout in PREFLIGHT_OUT.
  PREFLIGHT_OUT="$(AUTONOMOUS_TEAM_STATE_DIR="$GRACE_STATE_DIR" \
    ENGINE_SYNC_GIT_DIR="$WORK" \
    ENGINE_SYNC_STATE_DIR="$GRACE_STATE_DIR" \
    ENGINE_SYNC_HALT_THRESHOLD="$1" \
    bash "$PREFLIGHT" 2>/dev/null)"
  return $?
}

engine_sync_field() {
  python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    v = d.get("engine_sync", {}).get(sys.argv[2], "")
    print("true" if v is True else ("false" if v is False else v))
except Exception:
    print("")
' "$1" "$2" 2>/dev/null
}

set_marker "$SEED_SHA"   # stale, and it will stay stale

run_preflight_grace 3
RC1=$?
F1="$(engine_sync_field "$PREFLIGHT_OUT" consecutive_failures)"
H1="$(engine_sync_field "$PREFLIGHT_OUT" halt)"
[ "$RC1" = "0" ] && [ "$F1" = "1" ] && [ "$H1" = "false" ]
check "grace window: 1st stale observation -> counted, not halting, preflight exit 0" $?

run_preflight_grace 3
RC2=$?
F2="$(engine_sync_field "$PREFLIGHT_OUT" consecutive_failures)"
[ "$RC2" = "0" ] && [ "$F2" = "2" ]
check "grace window: 2nd stale observation -> still exit 0, counter at 2" $?

run_preflight_grace 3
RC3=$?
F3="$(engine_sync_field "$PREFLIGHT_OUT" consecutive_failures)"
H3="$(engine_sync_field "$PREFLIGHT_OUT" halt)"
[ "$RC3" = "1" ] && [ "$F3" = "3" ] && [ "$H3" = "true" ]
check "grace window: 3rd stale observation -> halt=true, preflight exit 1" $?

# ---------------------------------------------------------------------------
# Negative -- the counter resets on a genuine sync and on nothing else.
#
# A counter that a successful CHECK resets is a counter that only ever reads
# zero. What clears it is the marker moving forward, which is what a sync
# actually does.
# ---------------------------------------------------------------------------
run_alarm_in() {
  # run_alarm_in <state-dir> [threshold] -- stdout is the alarm's JSON.
  ENGINE_SYNC_GIT_DIR="$WORK" ENGINE_SYNC_STATE_DIR="$1" \
    ENGINE_SYNC_HALT_THRESHOLD="${2:-6}" bash "$ALARM" 2>/dev/null
}

RESET_STATE_DIR="$TMP_ROOT/reset-state"
mkdir -p "$RESET_STATE_DIR"

set_marker "$SEED_SHA"
run_alarm_in "$RESET_STATE_DIR" >/dev/null
run_alarm_in "$RESET_STATE_DIR" >/dev/null
OUT="$(run_alarm_in "$RESET_STATE_DIR")"
[ "$(json_field "$OUT" consecutive_failures)" = "3" ]
check "counter: three consecutive stale observations count to 3" $?

# A genuine sync -- the marker moves FORWARD to the tip.
set_marker "$AHEAD_SHA"
OUT="$(run_alarm_in "$RESET_STATE_DIR")"
[ "$(json_field "$OUT" status)" = "in-sync" ] && [ "$(json_field "$OUT" consecutive_failures)" = "0" ]
check "counter: a genuine sync (marker advanced) resets it to 0" $?

# Now the negative half, in its own state dir so the count starts clean.
# Go stale, count up, then move the marker BACKWARDS -- from the tip to an
# ancestor of it. The marker changed, so a naive "did the sha change?" test
# would clear the count here; only an ancestry check can tell a rewind from
# a sync, and a rewind is not a sync.
REWIND_STATE_DIR="$TMP_ROOT/rewind-state"
mkdir -p "$REWIND_STATE_DIR"
set_marker "$TIP_SHA"   # stale by 1 -- the remote is at AHEAD_SHA
run_alarm_in "$REWIND_STATE_DIR" >/dev/null
run_alarm_in "$REWIND_STATE_DIR" >/dev/null
OUT="$(run_alarm_in "$REWIND_STATE_DIR")"
BEFORE_REWIND="$(json_field "$OUT" consecutive_failures)"
set_marker "$SEED_SHA"   # backwards: SEED is an ancestor of TIP, not a descendant
OUT="$(run_alarm_in "$REWIND_STATE_DIR")"
AFTER_REWIND="$(json_field "$OUT" consecutive_failures)"
[ "$BEFORE_REWIND" = "3" ] && [ "$AFTER_REWIND" = "4" ]
check "counter: a marker moved backwards is not a sync and does not reset it" $?

# ---------------------------------------------------------------------------
# Negative -- `undecidable` counts toward the halt as well.
#
# "I cannot tell whether we are stale" is worse than "we are stale", and it
# is reachable in ordinary operation (an uninitialized marker), so it cannot
# be treated as an error state that never happens.
# ---------------------------------------------------------------------------
UNDEC_STATE_DIR="$TMP_ROOT/undec-state"
mkdir -p "$UNDEC_STATE_DIR"
clear_marker() {
  git -C "$WORK" update-ref -d refs/synced/code-plane
}
clear_marker
OUT="$(run_alarm_in "$UNDEC_STATE_DIR")"
S1="$(json_field "$OUT" status)"
UC1="$(json_field "$OUT" consecutive_failures)"
OUT="$(run_alarm_in "$UNDEC_STATE_DIR" 2)"
UC2="$(json_field "$OUT" consecutive_failures)"
UH2="$(json_field "$OUT" halt)"
[ "$S1" = "undecidable" ] && [ "$UC1" = "1" ] && [ "$UC2" = "2" ] && [ "$UH2" = "True" ]
check "undecidable counts toward the halt threshold too" $?
set_marker "$TIP_SHA"

# ---------------------------------------------------------------------------
# A6: the nine gpg-dependent test_fetch.py failures are now shutil.which
# ("gpg")-gated skips, not xfail/deletion/a passing stub. 0 failed overall.
# ---------------------------------------------------------------------------
# B: withheld_debt (D#2445) -- it used to only ever count paths
# apply_inbound.py explicitly WITHHELD, so a run that applied paths onto an
# open, unmerged engine-sync/inbound-* branch reported withheld_debt: 0
# while the engine was missing every one of them. It is now the sum of
# withheld (read from apply_inbound.py's own state file) and
# applied-but-unmerged (computed fresh by unmerged.py against the engine's
# open sync branches) -- see alarm.sh's own header for the split.
#
# A self-contained fixture, separate from A1-A6's WORK/PUBLIC above (which
# by this point in the file has been mutated through many marker moves and
# extra commits) -- an "engine" repo with its own `main`, plus a second bare
# repo standing in for its `origin` remote, matching the two-repo shape
# apply_inbound.py's --engine-remote actually pushes sync branches into.
# ---------------------------------------------------------------------------
B_ROOT="$TMP_ROOT/debt"
mkdir -p "$B_ROOT"
B_ENGINE_ORIGIN="$B_ROOT/engine-origin.git"
B_ENGINE="$B_ROOT/engine"
git init -q --bare "$B_ENGINE_ORIGIN"
git -C "$B_ENGINE_ORIGIN" symbolic-ref HEAD refs/heads/main
git init -q -b main "$B_ENGINE"
git -C "$B_ENGINE" config user.email t@example.com
git -C "$B_ENGINE" config user.name t
echo seed >"$B_ENGINE/f.txt"
git -C "$B_ENGINE" add f.txt
git -C "$B_ENGINE" commit -q -m seed
B_TIP_SHA="$(git -C "$B_ENGINE" rev-parse HEAD)"
git -C "$B_ENGINE" remote add origin "$B_ENGINE_ORIGIN"
git -C "$B_ENGINE" push -q origin main
# staleness.sh's own remote (default "code-plane") -- same bare repo, a
# different remote name, so B3's "status" can genuinely be exercised
# (in-sync: this test's whole point is that in-sync alongside a real gap is
# exactly the misreading D#2445 is about) rather than left at whatever an
# unconfigured remote defaults to.
git -C "$B_ENGINE" remote add code-plane "$B_ENGINE_ORIGIN"
git -C "$B_ENGINE" update-ref refs/synced/code-plane "$B_TIP_SHA"

# push_sync_branch <label> <path> <content> -- builds a commit on top of
# B_TIP_SHA carrying one path via a private temporary index (never a
# checkout of B_ENGINE's own working tree), and pushes it to the engine
# origin as engine-sync/inbound-<sha>, exactly the shape
# apply_inbound.py's build_branch_commits produces.
push_sync_branch() {
  local path="$1" content="$2" index_file sha
  index_file="$B_ROOT/index-$RANDOM"
  GIT_INDEX_FILE="$index_file" git -C "$B_ENGINE" read-tree "$B_TIP_SHA"
  local blob
  blob="$(printf '%s\n' "$content" | GIT_INDEX_FILE="$index_file" git -C "$B_ENGINE" hash-object -w --stdin)"
  GIT_INDEX_FILE="$index_file" git -C "$B_ENGINE" update-index --add --cacheinfo "100644,$blob,$path"
  local tree
  tree="$(GIT_INDEX_FILE="$index_file" git -C "$B_ENGINE" write-tree)"
  sha="$(git -C "$B_ENGINE" commit-tree "$tree" -p "$B_TIP_SHA" -m "sync $path")"
  git -C "$B_ENGINE" push -q origin "$sha:refs/heads/engine-sync/inbound-$sha"
  rm -f "$index_file"
}

write_apply_state() {
  # write_apply_state <state-dir> <pending-json> -- pending-json is a raw
  # JSON object literal, e.g. '{}' or '{"a/b.py": {}}'.
  mkdir -p "$1"
  printf '{"consecutive_failures": 0, "pending": %s}\n' "$2" >"$1/engine-sync-inbound-apply.json"
}

run_alarm_debt() {
  # run_alarm_debt <state-dir> -- stdout is the alarm's JSON.
  ENGINE_SYNC_GIT_DIR="$B_ENGINE" ENGINE_SYNC_STATE_DIR="$1" \
    ENGINE_SYNC_ENGINE_REMOTE=origin ENGINE_SYNC_LOCAL_REF=main \
    bash "$ALARM" 2>/dev/null
}

# B1: nothing owed either way -> 0, not null, and no error field.
B1_STATE="$B_ROOT/state-b1"
write_apply_state "$B1_STATE" '{}'
OUT="$(run_alarm_debt "$B1_STATE")"
[ "$(json_field "$OUT" withheld_debt)" = "0" ] && [ "$(json_field "$OUT" withheld_debt_error)" = "" ]
check "B1 nothing owed -> withheld_debt=0, no error" $?

# B2: withheld-only (apply_inbound.py's own state file), no open branches --
# the pre-D#2445 half, still correct. Driven from the fixture's own size,
# never a hardcoded literal.
B2_STATE="$B_ROOT/state-b2"
write_apply_state "$B2_STATE" '{"scripts/guard.sh": {}, "tests/out.txt": {}}'
EXPECTED_B2=2
OUT="$(run_alarm_debt "$B2_STATE")"
[ "$(json_field "$OUT" withheld_debt)" = "$EXPECTED_B2" ]
check "B2 withheld-only -> withheld_debt == pending count ($EXPECTED_B2)" $?

# B3: THE D#2445 REGRESSION ITSELF -- applied-but-unmerged only. pending is
# empty (exactly what a real apply run leaves when nothing was withheld),
# but an engine-sync/inbound-* branch nobody has merged carries one path
# main does not have. This is "a run that applies paths into a sync PR that
# is not merged" from the Discussion body, constructed directly.
push_sync_branch "backend/new_from_sync.py" "brand new synced content"
B3_STATE="$B_ROOT/state-b3"
write_apply_state "$B3_STATE" '{}'
GROUND_TRUTH_B3="$(git -C "$B_ENGINE" ls-remote --heads origin 'engine-sync/inbound-*' | wc -l | tr -d ' ')"
OUT="$(run_alarm_debt "$B3_STATE")"
[ "$(json_field "$OUT" status)" = "in-sync" ] && [ "$(json_field "$OUT" withheld_debt)" = "1" ] && [ "$GROUND_TRUTH_B3" = "1" ]
check "B3 applied-but-unmerged path, pending empty -> withheld_debt=1 (not 0)" $?

# B4: both halves nonzero at once -- summed, neither shadows the other.
B4_STATE="$B_ROOT/state-b4"
write_apply_state "$B4_STATE" '{"scripts/guard.sh": {}}'
OUT="$(run_alarm_debt "$B4_STATE")"
[ "$(json_field "$OUT" withheld_debt)" = "2" ]
check "B4 withheld(1) + applied-but-unmerged(1) -> withheld_debt=2 (summed)" $?

# B5/B6: state-file-absent vs state-file-corrupt are now different errors,
# not the same None (D#2445 item 7).
B5_STATE="$B_ROOT/state-b5"
mkdir -p "$B5_STATE" # no engine-sync-inbound-apply.json written at all
OUT="$(run_alarm_debt "$B5_STATE")"
ERR_ABSENT="$(json_field "$OUT" withheld_debt_error)"
[ "$(json_field "$OUT" withheld_debt)" = "None" ] && [ "$ERR_ABSENT" = "state-file-absent" ]
check "B5 apply state file never written -> withheld_debt=null, error=state-file-absent" $?

B6_STATE="$B_ROOT/state-b6"
mkdir -p "$B6_STATE"
printf 'not json{{{' >"$B6_STATE/engine-sync-inbound-apply.json"
OUT="$(run_alarm_debt "$B6_STATE")"
ERR_CORRUPT="$(json_field "$OUT" withheld_debt_error)"
[ "$(json_field "$OUT" withheld_debt)" = "None" ] && [ "$ERR_CORRUPT" = "state-file-corrupt" ]
check "B6 apply state file is not valid JSON -> withheld_debt=null, error=state-file-corrupt" $?

[ "$ERR_ABSENT" != "$ERR_CORRUPT" ]
check "B5 vs B6: absent and corrupt are DIFFERENT errors, not the same null" $?

# B7: negative -- local_ref moving forward on work unrelated to the sync
# branch (a real content change, not an empty commit -- an empty commit
# would not exercise a tip-to-tip diff either way) must not inflate the
# count. This is the tree-diff-trap-shaped failure unmerged.py's own
# docstring exists to avoid: a naive `git diff` of the two tips would
# report f.txt's own later, unrelated change as "missing" alongside the
# genuinely-missing path.
echo "unrelated engine-side change" >>"$B_ENGINE/f.txt"
git -C "$B_ENGINE" commit -q -am "unrelated later engine work"
B7_STATE="$B_ROOT/state-b7"
write_apply_state "$B7_STATE" '{}'
OUT="$(run_alarm_debt "$B7_STATE")"
[ "$(json_field "$OUT" withheld_debt)" = "1" ]
check "B7 local_ref moved forward on unrelated work -> count unchanged, still 1" $?

# ---------------------------------------------------------------------------
A6_STATE_DIR="$(mktemp -d)"
A6_OUT="$(AUTONOMOUS_TEAM_STATE_DIR="$A6_STATE_DIR" timeout --kill-after=5s 120s python3 -m pytest "$REPO_DIR/scripts/engine-sync/tests" -q 2>&1)"
A6_RC=$?
rm -rf "$A6_STATE_DIR"
[ "$A6_RC" = "0" ]
check "A6 python3 -m pytest scripts/engine-sync/tests -q => 0 failed" $?
if [ "$A6_RC" != "0" ]; then
  echo "  (pytest tail follows for diagnosis)" >&2
  echo "$A6_OUT" | tail -30 >&2
fi

# ---------------------------------------------------------------------------
echo
echo "PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf 'FAILED: %s\n' "${FAILED_NAMES[@]}" >&2
  exit 1
fi
exit 0
