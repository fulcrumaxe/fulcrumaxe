#!/usr/bin/env bash
# scripts/measure-pytest-baseline.sh --arm idle|contended --out DIR
#   [--junit-out PATH] [--cap-seconds N]
#
# One bounded, serialised full-suite pytest run, emitted as a single JSON
# record file under --out. D#2403 PR 2 of 5 — measurement only, fixes no
# test. See scripts/lib/pytest_baseline.py for the record schema.
#
# Owns: arm selection, contention process lifecycle (nproc/2 CPU spinners),
# the three /proc/loadavg samples, the pgrep serialisation guard, the scratch
# AUTONOMOUS_TEAM_STATE_DIR, and the timeout --kill-after=5s wrapper.
#
# Caps: 1800s idle arm, 2700s contended arm (D#2006 — timeout without
# --kill-after left three pytest suites running 5+ hours on this host).
#
# Optional flags (D#1900 PR 2, both additive — omit them and this script
# behaves exactly as it did before they existed):
#   --junit-out PATH    write the run's junit-xml to PATH and keep it, instead
#     of a mktemp file that is deleted once the record has been built. The
#     record only carries the outcomes parsed out of that XML; a caller that
#     has to publish the raw evidence (the portability probe uploads it as a
#     CI artifact) needs the file itself to survive.
#   --cap-seconds N     override the arm's timeout cap for a real measurement
#     on a host whose speed the arm caps were not chosen for. The caps above
#     were picked against a 12-vCPU host; a 2-vCPU GitHub runner is a
#     different regime, not a slower one, and 1800s is a guess there rather
#     than a bound. This is deliberately NOT the PYTEST_BASELINE_TEST_*
#     env overrides below: those stay test-only, and a real run that needed a
#     different cap should not have to borrow the fixture escape hatch.
#
# Test-only overrides (used by tests/test_pytest_baseline.sh against a
# fixture "sleeping stub", never by a real measurement run):
#   PYTEST_BASELINE_TEST_ARGV          replace the real pytest invocation
#   PYTEST_BASELINE_TEST_CAP_SECONDS   replace the arm's timeout cap
#   PYTEST_BASELINE_SKIP_SERIALIZE_GUARD=1   skip the pgrep guard (host may
#     have unrelated pytest runs from other agents; the guard would make the
#     fixture-only contract suite flaky for a reason that has nothing to do
#     with this PR)

set -u
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT" || exit 1

ARM=""
OUT_DIR=""
JUNIT_OUT=""
CAP_SECONDS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --arm) ARM="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --junit-out) JUNIT_OUT="$2"; shift 2 ;;
    --cap-seconds) CAP_SECONDS="$2"; shift 2 ;;
    *) echo "[measure-pytest-baseline] unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ "$ARM" != "idle" ] && [ "$ARM" != "contended" ]; then
  echo "[measure-pytest-baseline] --arm must be 'idle' or 'contended' (got: ${ARM})" >&2
  exit 2
fi
if [ -z "$OUT_DIR" ]; then
  echo "[measure-pytest-baseline] --out DIR is required" >&2
  exit 2
fi
if [ -n "$CAP_SECONDS" ]; then
  case "$CAP_SECONDS" in
    ''|*[!0-9]*)
      echo "[measure-pytest-baseline] --cap-seconds must be a positive integer (got: ${CAP_SECONDS})" >&2
      exit 2 ;;
  esac
  if [ "$CAP_SECONDS" -lt 1 ]; then
    echo "[measure-pytest-baseline] --cap-seconds must be a positive integer (got: ${CAP_SECONDS})" >&2
    exit 2
  fi
fi
mkdir -p "$OUT_DIR"

# ── Serialisation guard ───────────────────────────────────────────────────
# Never start alongside another agent's pytest (item 9). Checked immediately
# before the run starts, per the recorded field's own definition.
OTHER_PYTEST_RUNNING=false
if [ "${PYTEST_BASELINE_SKIP_SERIALIZE_GUARD:-0}" != "1" ]; then
  if pgrep -f 'python3 -m pytest' >/dev/null 2>&1; then
    echo "[measure-pytest-baseline] another 'python3 -m pytest' is already running on this host — refusing to start (runs must be serialised)" >&2
    exit 3
  fi
fi

# ── Contention lifecycle (arm=contended only) ─────────────────────────────
# CPU-only, nproc/2 spinners — half the box, deliberately (saturating it
# produces numbers indistinguishable from the defect being measured).
CONTENTION_METHOD=""
CONTENTION_PIDS=()
NPROC=$(nproc)
if [ "$ARM" = "contended" ]; then
  N=$(( NPROC / 2 ))
  [ "$N" -lt 1 ] && N=1
  for _ in $(seq 1 "$N"); do
    ( while :; do :; done ) &
    CONTENTION_PIDS+=("$!")
  done
  CONTENTION_METHOD="${N} CPU-bound busy-loop spinners ('while :; do :; done'), started before the run and reaped on EXIT"
fi

_reap_contention() {
  if [ "${#CONTENTION_PIDS[@]}" -gt 0 ]; then
    kill -KILL "${CONTENTION_PIDS[@]}" 2>/dev/null || true
  fi
}
trap _reap_contention EXIT

# ── Cap selection ──────────────────────────────────────────────────────────
if [ "$ARM" = "idle" ]; then
  CAP=1800
else
  CAP=2700
fi
if [ -n "$CAP_SECONDS" ]; then
  CAP="$CAP_SECONDS"
fi
if [ -n "${PYTEST_BASELINE_TEST_CAP_SECONDS:-}" ]; then
  CAP="$PYTEST_BASELINE_TEST_CAP_SECONDS"
fi

# ── Load sampling (start / mid / end) ─────────────────────────────────────
_read_loadavg() {
  read -r one five _ < /proc/loadavg
  printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$one" "$five"
}

SAMPLES_FILE=$(mktemp)
( while :; do _read_loadavg >> "$SAMPLES_FILE"; sleep 5; done ) &
SAMPLER_PID=$!

_kill_sampler() {
  kill -KILL "$SAMPLER_PID" 2>/dev/null || true
  wait "$SAMPLER_PID" 2>/dev/null || true
}
trap '_kill_sampler; _reap_contention' EXIT

START_SAMPLE=$(_read_loadavg)

# ── Scratch state dir (never let pytest touch production state) ──────────
STATE_DIR=$(mktemp -d)

# ── Checkout cleanliness ───────────────────────────────────────────────────
CHECKOUT_SHA=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
DIRTY_PATHS_RAW=$(git status --porcelain 2>/dev/null || true)
if [ -z "$DIRTY_PATHS_RAW" ]; then
  CHECKOUT_CLEAN=true
else
  CHECKOUT_CLEAN=false
fi

HOST=$(hostname)
if [ -n "$JUNIT_OUT" ]; then
  mkdir -p "$(dirname "$JUNIT_OUT")"
  JUNIT_XML="$JUNIT_OUT"
else
  JUNIT_XML=$(mktemp --suffix=.xml)
fi

# path_scope.argv — the actual argv, not a label for it. archive/ is excluded
# via explicit --ignore; --continue-on-collection-errors is deliberately NOT
# used (it would convert "the suite did not run" into "the suite ran with
# more errors", erasing the distinction this measurement exists to preserve).
# tests/test_next_work.py is ignored because pytest-timeout is not installed
# on this host (verified: ModuleNotFoundError), so there is no per-test bound
# available for it — without the ignore every run dies on the outer timeout
# and produces no record at all.
PATH_SCOPE_ARGV="python3 -m pytest tests/ backend/tests/ --ignore=archive/ --ignore=tests/test_next_work.py -q --tb=no -p no:cacheprovider"

T0=$(date +%s)
if [ -n "${PYTEST_BASELINE_TEST_ARGV:-}" ]; then
  # shellcheck disable=SC2086
  timeout --kill-after=5s "$CAP" env AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR" bash -c "$PYTEST_BASELINE_TEST_ARGV"
  EXIT_CODE=$?
else
  timeout --kill-after=5s "$CAP" env AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR" \
    python3 -m pytest tests/ backend/tests/ \
    --ignore=archive/ --ignore=tests/test_next_work.py \
    -q --tb=no -p no:cacheprovider --junit-xml="$JUNIT_XML"
  EXIT_CODE=$?
fi
T1=$(date +%s)
DURATION=$(( T1 - T0 ))

END_SAMPLE=$(_read_loadavg)
_kill_sampler
_reap_contention
trap - EXIT

if [ "$EXIT_CODE" -eq 124 ]; then
  COMPLETE=false
  TIMED_OUT=true
else
  COMPLETE=true
  TIMED_OUT=false
fi

# Pick the sample closest to the midpoint of the elapsed window from the
# periodic sampler's log; fall back to the start sample if the run finished
# before the sampler ever fired (nothing better exists to report).
MID_SAMPLE=$(python3 - "$SAMPLES_FILE" "$T0" "$T1" "$START_SAMPLE" <<'PYEOF'
import sys

samples_file, t0, t1, start_sample = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
mid_target = (t0 + t1) / 2.0
best = None
best_dist = None
try:
    with open(samples_file) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) != 3:
                continue
            ts_str = parts[0]
            import datetime
            try:
                ts = datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
            epoch = ts.timestamp()
            dist = abs(epoch - mid_target)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = line.strip()
except OSError:
    pass
print(best if best else start_sample)
PYEOF
)
rm -f "$SAMPLES_FILE"

_sample_to_json() {
  # "<iso> <one_min> <five_min>" -> {"at":..., "one_min":..., "five_min":...}
  python3 -c "
import json, sys
parts = sys.argv[1].split()
print(json.dumps({'at': parts[0], 'one_min': float(parts[1]), 'five_min': float(parts[2])}))
" "$1"
}

START_JSON=$(_sample_to_json "$START_SAMPLE")
MID_JSON=$(_sample_to_json "$MID_SAMPLE")
END_JSON=$(_sample_to_json "$END_SAMPLE")

CONTEXT_FILE=$(mktemp --suffix=.json)
# Build the context via a dedicated python helper that takes everything as
# argv, avoiding any shell-quoting-into-python-source hazard.
python3 - \
  "$CONTEXT_FILE" \
  "$HOST" \
  "$PATH_SCOPE_ARGV" \
  "$CHECKOUT_SHA" \
  "$CHECKOUT_CLEAN" \
  "$DIRTY_PATHS_RAW" \
  "$START_JSON" \
  "$MID_JSON" \
  "$END_JSON" \
  "$NPROC" \
  "$ARM" \
  "$CONTENTION_METHOD" \
  "$OTHER_PYTEST_RUNNING" \
  "$STATE_DIR" \
  "$DURATION" \
  "$COMPLETE" \
  "$EXIT_CODE" \
  "$TIMED_OUT" <<'PYEOF'
import json
import sys

(context_file, host, argv, sha, clean, dirty_raw, start_json, mid_json, end_json,
 nproc, arm, contention_method, other_pytest_running, state_dir, duration,
 complete, exit_code, timed_out) = sys.argv[1:19]

dirty_paths = [l[3:].strip() for l in dirty_raw.splitlines() if l.strip()]

context = {
    "host": host,
    "path_scope": {"argv": argv},
    "checkout": {
        "sha": sha,
        "clean": clean == "true",
        "dirty_paths": dirty_paths,
    },
    "load": {
        "samples": [json.loads(start_json), json.loads(mid_json), json.loads(end_json)],
        "nproc": int(nproc),
        "arm": arm,
        "contention_method": contention_method,
    },
    "other_pytest_running": other_pytest_running == "true",
    "state_dir": state_dir,
    "duration_seconds": int(duration),
    "complete": complete == "true",
    "exit_code": int(exit_code),
    "timed_out": timed_out == "true",
}

with open(context_file, "w") as fh:
    json.dump(context, fh, indent=2)
PYEOF

RECORD_FILE="${OUT_DIR}/run-${ARM}-$(date -u +%Y%m%dT%H%M%SZ).json"
if [ -f "$JUNIT_XML" ] && [ "$COMPLETE" = "true" ]; then
  JUNIT_ARG="$JUNIT_XML"
else
  JUNIT_ARG="/nonexistent-junit-xml-not-produced"
fi

python3 "$REPO_ROOT/scripts/lib/pytest_baseline.py" record \
  --junit-xml "$JUNIT_ARG" \
  --context "$CONTEXT_FILE" > "$RECORD_FILE"
RC=$?

rm -f "$CONTEXT_FILE"
# Keep the junit-xml only when the caller asked for it by path; the mktemp
# spelling is scratch and is still cleaned up.
if [ -z "$JUNIT_OUT" ]; then
  rm -f "$JUNIT_XML"
fi

if [ "$RC" -ne 0 ]; then
  echo "[measure-pytest-baseline] record generation failed (rc=$RC)" >&2
  rm -f "$RECORD_FILE"
  exit 1
fi

echo "[measure-pytest-baseline] wrote $RECORD_FILE (arm=$ARM complete=$COMPLETE duration=${DURATION}s exit_code=$EXIT_CODE)" >&2
exit 0
