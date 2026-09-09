#!/usr/bin/env bash
# test_dispatcher_manifest_change.sh — two-tick regression test for D#2495:
# scripts/schedule/dispatcher.sh must re-read scripts/schedule/jobs.yaml
# after the manifest's content changes, even when `stat` is unusable on the
# host running the tick.
#
# Before the fix, dispatcher.sh compared `pc_stat_mtime "$MANIFEST"` against
# a cached value to decide whether to re-parse. On any host where
# pc_stat_mtime fails, the literal sentinel "0" landed on BOTH sides of that
# equality check after the first tick, so every later tick compared "0" !=
# "0", found no difference, and never re-read the manifest again — silently,
# forever. A single dispatcher.sh call can't see this: the first tick always
# behaves correctly (empty cache, so it re-parses regardless of the mtime
# value). Only a *second* tick, after the manifest has actually changed,
# exposes a stuck cache — hence the two-tick shape below.
#
# The fix replaces the mtime comparison with a `cksum` of the manifest's
# bytes, which has no stat-flag dialect to fail on, so the failure mode is
# gone rather than handled. Case 1 below still runs under a shimmed, always-
# failing `stat` to prove that: dispatcher.sh no longer depends on `stat`
# succeeding at all, so a hostile host can't reproduce the old collapse.
#
# Cases:
#   1. Two-tick sequence under a hostile `stat` — the manifest changes
#      between ticks, and tick 2 must see the change.
#   2. Reverse case — an unchanged manifest between two ticks must still
#      SKIP the re-parse on the second tick (a canary byte appended to the
#      jobs cache after tick 1 proves the file was left untouched — content
#      would be identical either way, so this has to be a "was it rewritten
#      at all" check, not a "does it look right" check).
#   3. A genuinely unreadable manifest is a loud, distinct failure (exit
#      non-zero + a manifest_unreadable run-log row), never a sentinel that
#      could later compare equal to itself.
#
# Exit 0 = all tests passed. Exit 1 = failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# DISPATCHER_UNDER_TEST lets a manual mutation-testing pass point this suite
# at a scratch copy of dispatcher.sh (e.g. one with the fix reverted) without
# touching the real file. Defaults to the real script.
DISPATCHER="${DISPATCHER_UNDER_TEST:-$REPO_ROOT/scripts/schedule/dispatcher.sh}"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

echo "=== Dispatcher Manifest-Change Tests (D#2495) ==="
echo ""

# A shimmed `stat` that always fails regardless of flags — simulates "no
# usable stat found on this host" (the condition pc_stat_mtime used to hit).
# Prepended onto PATH only for the dispatcher.sh invocations below, never
# for this test script's own file introspection.
SHIM_DIR=$(mktemp -d)
cat > "$SHIM_DIR/stat" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$SHIM_DIR/stat"

GATE_ON_CONFIG=$(mktemp)
printf '{"gates": {"scheduled_jobs": true}}' > "$GATE_ON_CONFIG"

# _write_manifest PATH TOKEN_CEILING — a single job whose schedule (Feb 29,
# 3am) never matches "now", so a tick completes without actually running or
# forking anything. Same never-due trick tests/test_scheduler_dispatcher.sh
# already uses for its p99 benchmark.
_write_manifest() {
    local path="$1" token_ceiling="$2"
    cat > "$path" <<EOF2
- name: heartbeat
  job: heartbeat
  schedule: "0 3 29 2 *"
  timeout_seconds: 30
  token_ceiling: $token_ceiling
  enabled: true
EOF2
}

# _run_tick STATE_DIR MANIFEST HASH_CACHE JOBS_CACHE HOSTILE(0|1) — prints
# dispatcher.sh's exit code.
_run_tick() {
    local state_dir="$1" manifest="$2" hash_cache="$3" jobs_cache="$4" hostile="$5"
    local path_prefix=""
    [[ "$hostile" -eq 1 ]] && path_prefix="$SHIM_DIR:"
    PATH="${path_prefix}${PATH}" \
        AUTONOMOUS_TEAM_STATE_DIR="$state_dir" \
        AF_CONTROL_PLANE_CONFIG="$GATE_ON_CONFIG" \
        DISPATCHER_MANIFEST="$manifest" \
        DISPATCHER_HASH_CACHE_FILE="$hash_cache" \
        DISPATCHER_JOBS_CACHE_FILE="$jobs_cache" \
        bash "$DISPATCHER" >/dev/null 2>&1
    echo $?
}

# ── Case 1: two-tick sequence, manifest changes between ticks ────────────────
echo "1. Two-tick sequence under a hostile stat -- manifest change is picked up on tick 2"
CASE1_DIR=$(mktemp -d)
MANIFEST_1="$CASE1_DIR/jobs.yaml"
HASH_CACHE_1="$CASE1_DIR/hash-cache.txt"
JOBS_CACHE_1="$CASE1_DIR/jobs-cache.json"
STATE_1="$CASE1_DIR/state"

_write_manifest "$MANIFEST_1" 0

RC1=$(_run_tick "$STATE_1" "$MANIFEST_1" "$HASH_CACHE_1" "$JOBS_CACHE_1" 1)
if [[ "$RC1" -ne 0 ]]; then
    fail "tick 1 exits 0" "exit=$RC1"
elif [[ ! -f "$JOBS_CACHE_1" ]]; then
    fail "tick 1 populates the jobs cache" "no cache file written"
else
    TICK1_TOKEN_CEILING=$(python3 -c "import json; print(json.load(open('$JOBS_CACHE_1'))[0]['token_ceiling'])" 2>/dev/null || echo "ERR")
    if [[ "$TICK1_TOKEN_CEILING" == "0" ]]; then
        pass "tick 1 (cold cache) reads the manifest"
    else
        fail "tick 1 (cold cache) reads the manifest" "expected token_ceiling=0, got '$TICK1_TOKEN_CEILING'"
    fi
fi

# Change the manifest -- the edit a real operator would make between two
# dispatcher ticks a minute apart.
_write_manifest "$MANIFEST_1" 7

RC2=$(_run_tick "$STATE_1" "$MANIFEST_1" "$HASH_CACHE_1" "$JOBS_CACHE_1" 1)
if [[ "$RC2" -ne 0 ]]; then
    fail "tick 2 exits 0" "exit=$RC2"
else
    TICK2_TOKEN_CEILING=$(python3 -c "import json; print(json.load(open('$JOBS_CACHE_1'))[0]['token_ceiling'])" 2>/dev/null || echo "ERR")
    if [[ "$TICK2_TOKEN_CEILING" == "7" ]]; then
        pass "tick 2 re-reads the manifest after it changed (under a hostile stat)"
    else
        fail "tick 2 re-reads the manifest after it changed (under a hostile stat)" \
            "expected token_ceiling=7 after the edit, got '$TICK2_TOKEN_CEILING' -- the manifest change was not picked up"
    fi
fi

rm -rf "$CASE1_DIR"

# ── Case 2: reverse -- unchanged manifest must SKIP the re-parse ─────────────
echo "2. Reverse case -- unchanged manifest is NOT re-read on the second tick"
CASE2_DIR=$(mktemp -d)
MANIFEST_2="$CASE2_DIR/jobs.yaml"
HASH_CACHE_2="$CASE2_DIR/hash-cache.txt"
JOBS_CACHE_2="$CASE2_DIR/jobs-cache.json"
STATE_2="$CASE2_DIR/state"

_write_manifest "$MANIFEST_2" 3

RC3=$(_run_tick "$STATE_2" "$MANIFEST_2" "$HASH_CACHE_2" "$JOBS_CACHE_2" 0)
if [[ "$RC3" -ne 0 || ! -f "$JOBS_CACHE_2" ]]; then
    fail "reverse case: warm-up tick populates the cache" "exit=$RC3"
else
    # Canary appended after the warm-up tick. A re-parse overwrites the
    # cache file (python's `> "$CACHED_JOBS_FILE"` truncates it); a skipped
    # re-parse leaves it byte-for-byte untouched, canary included. This has
    # to be a content check, not a timing/mtime check -- two ticks that land
    # in the same wall-clock second would make mtime unreliable here, and
    # the JSON content would be identical either way since the manifest
    # didn't change.
    CANARY="CANARY-D2495-$$-$RANDOM"
    echo "# $CANARY" >> "$JOBS_CACHE_2"

    RC4=$(_run_tick "$STATE_2" "$MANIFEST_2" "$HASH_CACHE_2" "$JOBS_CACHE_2" 0)
    if [[ "$RC4" -ne 0 ]]; then
        fail "reverse case: second tick exits 0" "exit=$RC4"
    elif grep -q "$CANARY" "$JOBS_CACHE_2"; then
        pass "unchanged manifest skips the re-parse (jobs cache left untouched)"
    else
        fail "unchanged manifest skips the re-parse (jobs cache left untouched)" \
            "canary is gone -- the cache was rewritten even though the manifest did not change"
    fi
fi

rm -rf "$CASE2_DIR"

# ── Case 3: manifest genuinely unreadable -- loud failure, not a sentinel ────
echo "3. Unreadable manifest -- dispatcher exits non-zero, no silent sentinel"
CASE3_DIR=$(mktemp -d)
MISSING_MANIFEST="$CASE3_DIR/does-not-exist.yaml"
HASH_CACHE_3="$CASE3_DIR/hash-cache.txt"
JOBS_CACHE_3="$CASE3_DIR/jobs-cache.json"
STATE_3="$CASE3_DIR/state"

RC5=$(_run_tick "$STATE_3" "$MISSING_MANIFEST" "$HASH_CACHE_3" "$JOBS_CACHE_3" 0)
if [[ "$RC5" -ne 0 ]]; then
    pass "unreadable manifest exits non-zero"
else
    fail "unreadable manifest exits non-zero" "expected non-zero exit, got 0"
fi

RUN_LOG_3="$STATE_3/scheduled-jobs/runs.jsonl"
if [[ -f "$RUN_LOG_3" ]] && grep -q '"note":"manifest_unreadable"' "$RUN_LOG_3"; then
    pass "unreadable manifest writes a manifest_unreadable run-log row"
else
    fail "unreadable manifest writes a manifest_unreadable run-log row" "row not found in $RUN_LOG_3"
fi

rm -rf "$CASE3_DIR"
rm -f "$GATE_ON_CONFIG"
rm -rf "$SHIM_DIR"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -eq 0 ]]; then
    exit 0
else
    exit 1
fi
