#!/usr/bin/env bash
# tests/test_coldstart_state_containment.sh — a coldstart run from a test must
# not leave a directory behind under the operator's home (D#2317 PR-c).
#
# Covers:
#   1. COLDSTART_STATE_ROOT redirects the state dir, and nothing lands in $HOME.
#   2. With the variable unset, the default is genuinely unchanged.
#   3. tests/test_loop_metrics_path.sh — the suite that produced 44 of the 75
#      dead fixtures — leaves the home directory listing byte-identical.
#   4. The redirect, not the trap, is what closes the hole: same listing after
#      a SIGKILL mid-run.
#   5. scripts/sweep-stale-state-dirs.sh never selects by name pattern, and
#      --apply moves rather than deletes.
#
# Every check builds its own fixture HOME. Nothing here reads or writes the
# real one.

set -uo pipefail

PASS=0
FAIL=0
_pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
_fail() { echo "FAIL: $1 — $2"; FAIL=$((FAIL + 1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WORK="$(mktemp -d /tmp/test-coldstart-containment-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# A throwaway repo for coldstart to wire up.
make_repo() {
  local d="$WORK/$1"
  mkdir -p "$d"
  git -C "$d" init -q
  git -C "$d" remote add origin "https://github.com/test/$1.git"
  printf '%s' "$d"
}

# ── 1. COLDSTART_STATE_ROOT redirects, $HOME stays clean ─────────────────────
H1="$WORK/home1"; R1="$WORK/root1"
mkdir -p "$H1" "$R1"
REPO1="$(make_repo repo1)"
HOME="$H1" COLDSTART_STATE_ROOT="$R1" \
  bash "$REPO_ROOT/scripts/coldstart-project.sh" "$REPO1" redirected \
  > "$WORK/cs1.log" 2>&1
CS1_RC=$?

if [[ $CS1_RC -ne 0 ]]; then
  _fail "coldstart with override exits 0" "exit=$CS1_RC (see $WORK/cs1.log)"
elif [[ -d "$R1/.redirected-state" ]]; then
  _pass "COLDSTART_STATE_ROOT places the state dir under the override root"
else
  _fail "COLDSTART_STATE_ROOT redirect" "no $R1/.redirected-state"
fi

shopt -s nullglob dotglob
H1_LEFTOVERS=( "$H1"/.*-state )
shopt -u nullglob dotglob
if [[ ${#H1_LEFTOVERS[@]} -eq 0 ]]; then
  _pass "nothing created under \$HOME when the override is set"
else
  _fail "override leaves \$HOME clean" "found: ${H1_LEFTOVERS[*]}"
fi

# ── 2. Default (variable unset) unchanged ────────────────────────────────────
H2="$WORK/home2"
mkdir -p "$H2"
REPO2="$(make_repo repo2)"
env -u COLDSTART_STATE_ROOT HOME="$H2" \
  bash "$REPO_ROOT/scripts/coldstart-project.sh" "$REPO2" defaulted \
  > "$WORK/cs2.log" 2>&1
CS2_RC=$?

if [[ $CS2_RC -eq 0 && -d "$H2/.defaulted-state" ]]; then
  _pass "with the variable unset the state dir still lands in \$HOME"
else
  _fail "default unchanged" "exit=$CS2_RC, no $H2/.defaulted-state (see $WORK/cs2.log)"
fi

# ── 3. The leaking suite no longer enlarges the home directory ───────────────
# Run the real tests/test_loop_metrics_path.sh against a fixture HOME and
# compare the listing either side. On origin/main this suite added exactly one
# .test-proj-<pid>-state per run.
H3="$WORK/home3"
mkdir -p "$H3"
list_state_dirs() {
  shopt -s nullglob dotglob
  local d found=()
  for d in "$1"/.*-state; do found+=("$(basename "$d")"); done
  shopt -u nullglob dotglob
  printf '%s\n' "${found[@]+"${found[@]}"}" | sort
}

BEFORE3="$(list_state_dirs "$H3")"
HOME="$H3" bash "$REPO_ROOT/tests/test_loop_metrics_path.sh" > "$WORK/metrics.log" 2>&1
METRICS_RC=$?
AFTER3="$(list_state_dirs "$H3")"

if [[ $METRICS_RC -ne 0 ]]; then
  _fail "test_loop_metrics_path.sh still passes" "exit=$METRICS_RC (see $WORK/metrics.log)"
else
  _pass "test_loop_metrics_path.sh still passes"
fi
if [[ "$BEFORE3" == "$AFTER3" ]]; then
  _pass "test_loop_metrics_path.sh leaves the home listing identical"
else
  _fail "no new state dirs from the suite" "before=[$BEFORE3] after=[$AFTER3]"
fi

# ── 4. Containment survives SIGKILL ──────────────────────────────────────────
# A trap does not fire on SIGKILL. What has to hold is that the coldstart never
# wrote to $HOME in the first place, so a killed run leaks into scratch.
H4="$WORK/home4"; R4="$WORK/root4"
mkdir -p "$H4" "$R4"
REPO4="$(make_repo repo4)"
BEFORE4="$(list_state_dirs "$H4")"

cat > "$WORK/killable.sh" <<'INNER'
#!/usr/bin/env bash
COLDSTART_STATE_ROOT="$2"
export COLDSTART_STATE_ROOT
trap 'echo "trap fired (it will not, under SIGKILL)"' EXIT
bash "$1/scripts/coldstart-project.sh" "$3" killed > /dev/null 2>&1
sleep 30
INNER
HOME="$H4" bash "$WORK/killable.sh" "$REPO_ROOT" "$R4" "$REPO4" &
KILL_PID=$!
# Wait for the coldstart to have actually created its state dir, then SIGKILL.
for _ in $(seq 1 100); do
  [[ -d "$R4/.killed-state" ]] && break
  sleep 0.2
done
{
  kill -9 "$KILL_PID"
  pkill -9 -P "$KILL_PID"
  wait "$KILL_PID"
} 2>/dev/null
AFTER4="$(list_state_dirs "$H4")"

if [[ ! -d "$R4/.killed-state" ]]; then
  _fail "SIGKILL fixture reached the coldstart" "no $R4/.killed-state — the run never got far enough to prove anything"
elif [[ "$BEFORE4" == "$AFTER4" ]]; then
  _pass "a SIGKILLed coldstart leaves the home listing identical (the trap never ran)"
else
  _fail "SIGKILL containment" "before=[$BEFORE4] after=[$AFTER4]"
fi

# ── 5. sweep-stale-state-dirs.sh ─────────────────────────────────────────────
SWEEP="$REPO_ROOT/scripts/sweep-stale-state-dirs.sh"
SR="$WORK/sweeproot"
mkdir -p "$SR"

# (a) small, old, nothing using it -> candidate
mkdir -p "$SR/.stale-fixture-state"
echo x > "$SR/.stale-fixture-state/audit.jsonl"
touch -d '400 days ago' "$SR/.stale-fixture-state/audit.jsonl" "$SR/.stale-fixture-state"

# (b) big, recent, live process inside it -> must NOT be a candidate, even
#     though its name matches the same glob. This is the ~/.projectb-state
#     near-miss from D#2317 reproduced in a fixture.
mkdir -p "$SR/.busy-fixture-state"
head -c 2000000 /dev/urandom > "$SR/.busy-fixture-state/blob.bin" 2>/dev/null
( cd "$SR/.busy-fixture-state" && exec sleep 45 ) &
BUSY_PID=$!
sleep 0.5

# (c) old and idle, but advertises a dashboard -> must NOT be a candidate
mkdir -p "$SR/.advertised-fixture-state"
echo '{}' > "$SR/.advertised-fixture-state/dashboard-runtime.json"
touch -d '400 days ago' "$SR/.advertised-fixture-state/dashboard-runtime.json" "$SR/.advertised-fixture-state"

DRY="$(bash "$SWEEP" --root "$SR" --older-than-days 30 2>&1)"
DRY_RC=$?

if [[ $DRY_RC -eq 0 ]]; then
  _pass "sweep dry run exits 0"
else
  _fail "sweep dry run exits 0" "exit=$DRY_RC"
fi
if grep -q "^CANDIDATE .*\.stale-fixture-state$" <<<"$DRY"; then
  _pass "sweep nominates the small, old, unused dir"
else
  _fail "sweep nominates the stale dir" "not listed as CANDIDATE:
$DRY"
fi
if grep -q "^KEEP .*\.busy-fixture-state.*live process" <<<"$DRY"; then
  _pass "sweep keeps a name-matching dir that has a live process (the projectb near-miss)"
else
  _fail "sweep keeps the busy dir" "expected a KEEP line with 'live process':
$DRY"
fi
if grep -q "^KEEP .*\.advertised-fixture-state.*dashboard-runtime" <<<"$DRY"; then
  _pass "sweep keeps a dir advertising a dashboard runtime"
else
  _fail "sweep keeps the advertised dir" "expected a KEEP line:
$DRY"
fi
if grep -qE '^(CANDIDATE|KEEP) +[0-9]' <<<"$DRY"; then
  _pass "sweep prints a size and a timestamp for every directory it names"
else
  _fail "sweep prints size/mtime" "no sized rows in output:
$DRY"
fi
if [[ -d "$SR/.stale-fixture-state" ]]; then
  _pass "sweep dry run moved nothing"
else
  _fail "sweep dry run is read-only" "the candidate is gone after a dry run"
fi

# The real --apply run, not a preview of it (D#2149): assert on the actual
# before/after filesystem state, and that the candidate was MOVED, not removed.
APPLY_BEFORE="$(list_state_dirs "$SR")"
QDIR="$WORK/quarantine"
APPLY_OUT="$(bash "$SWEEP" --root "$SR" --older-than-days 30 --quarantine "$QDIR" --apply 2>&1)"
APPLY_RC=$?
APPLY_AFTER="$(list_state_dirs "$SR")"

if [[ $APPLY_RC -eq 0 ]]; then
  _pass "sweep --apply exits 0"
else
  _fail "sweep --apply exits 0" "exit=$APPLY_RC:
$APPLY_OUT"
fi
if [[ ! -d "$SR/.stale-fixture-state" && -d "$QDIR/.stale-fixture-state" ]]; then
  _pass "sweep --apply moved the candidate to quarantine rather than deleting it"
else
  _fail "sweep --apply moves, never deletes" "before=[$APPLY_BEFORE] after=[$APPLY_AFTER]; quarantine=$(ls -A "$QDIR" 2>&1)"
fi
if [[ -d "$SR/.busy-fixture-state" && -d "$SR/.advertised-fixture-state" ]]; then
  _pass "sweep --apply left every non-candidate in place"
else
  _fail "sweep --apply spares non-candidates" "after=[$APPLY_AFTER]"
fi

kill -9 "$BUSY_PID" 2>/dev/null
wait "$BUSY_PID" 2>/dev/null

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -gt 0 ]] && exit 1
exit 0
