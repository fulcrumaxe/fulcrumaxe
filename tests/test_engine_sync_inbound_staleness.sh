#!/usr/bin/env bash
# tests/test_engine_sync_inbound_staleness.sh
#
# A1-A6 acceptance tests for D#2439 slice A (engine-sync inbound alarm).
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
# still exits 0 while that status is "stale" -- the alarm is loud but
# non-blocking in this slice (the blocking flip is slice C's C9).
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
# A6: the nine gpg-dependent test_fetch.py failures are now shutil.which
# ("gpg")-gated skips, not xfail/deletion/a passing stub. 0 failed overall.
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
