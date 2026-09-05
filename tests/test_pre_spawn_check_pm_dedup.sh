#!/usr/bin/env bash
# tests/test_pre_spawn_check_pm_dedup.sh
# Regression test for D#844: PM dedup check must run BEFORE writing spawn_attempt
# to agent-feed.jsonl, so the just-written entry doesn't self-block the call.
#
# Core assertion: a fresh first call is NOT blocked by pm_dedup; an immediate
# second call IS blocked.
#
# HARD RULE: this test MUST NOT invoke `claude`, `claude -p`, `_start_loop_run`,
# or trigger /loop. Block conditions are tested via direct feed manipulation only.
#
# Runs the real scripts/pre-spawn-check.sh against a sandboxed workspace copy
# (tests/lib/pre-spawn-check-fixture.sh -- the same mechanism
# test_pre_spawn_check_block_events.sh already used) instead of the live
# $REPO_ROOT/.autonomous-team/agent-feed.jsonl (D#2267). Before this fix, this
# suite read and rewrote that real file directly -- a read-filter-rewrite of
# the whole file, which could silently drop a real concurrently-running
# agent's own spawn_attempt append landing between this suite's read and its
# write. The sandboxed workspace has no other writer, so that data-loss risk
# and the live-feed collision risk are both gone by construction rather than
# mitigated.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"
source "$SCRIPT_DIR/lib/pre-spawn-check-fixture.sh"

PASS=0
FAIL=0
TEST_DISCUSSION="9999"

ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

echo "=== test_pre_spawn_check_pm_dedup ==="
echo ""

TMPDIR_BASE=$(mktemp -d)
trap 'rm -rf "$TMPDIR_BASE"' EXIT

ws=$(make_workspace)
install_sandbox "$ws"
FEED_FILE="$ws/.autonomous-team/agent-feed.jsonl"

# ── Cleanup helper ─────────────────────────────────────────────────────────────
# Remove any test entries for D#9999 from the workspace feed. The workspace
# has no other writer, so this plain read-filter-rewrite is safe here in a
# way it would not be against the live repo feed.
cleanup_feed() {
  if [[ -f "$FEED_FILE" ]]; then
    python3 - <<PYEOF 2>/dev/null || true
import json, pathlib
feed = pathlib.Path("$FEED_FILE")
if not feed.exists():
    exit(0)
lines = []
for line in feed.read_text().splitlines():
    try:
        d = json.loads(line)
        if str(d.get("discussion", "")) == "$TEST_DISCUSSION" and d.get("role") == "project-manager":
            continue  # drop test entries
    except Exception:
        pass
    lines.append(line)
feed.write_text("\n".join(lines) + ("\n" if lines else ""))
PYEOF
  fi
}

# ── Helper: count recent pm spawn_attempt entries for D#TEST_DISCUSSION ────────
count_recent_pm_entries() {
  python3 - <<PYEOF 2>/dev/null || echo "0"
import json, time, pathlib
feed = pathlib.Path("$FEED_FILE")
cutoff = time.time() - 120
count = 0
if feed.exists():
    for line in feed.read_text().splitlines():
        try:
            d = json.loads(line)
            if (d.get("event_type") == "spawn_attempt"
                    and d.get("role") == "project-manager"
                    and str(d.get("discussion", "")) == "$TEST_DISCUSSION"):
                import datetime
                ts = d.get("ts", "")
                t = datetime.datetime.fromisoformat(ts.rstrip("Z")).timestamp()
                if t >= cutoff:
                    count += 1
        except Exception:
            pass
print(count)
PYEOF
}

# ── AC1: First call — pm_dedup must NOT block ──────────────────────────────────
# What we MUST NOT see is the pm_dedup error in stderr.
echo "--- AC1: first call for D#$TEST_DISCUSSION is not pm_dedup blocked ---"

run_psc_sandboxed "$ws" --role project-manager --discussion "$TEST_DISCUSSION"

if psc_stderr "$ws" | grep -q "pm_dedup"; then
  fail "AC1: first call was blocked by pm_dedup (self-blocking bug still present)"
  echo "  stderr: $(psc_stderr "$ws")"
else
  ok "AC1: first call not blocked by pm_dedup (correct)"
fi

# ── AC2: Write a synthetic spawn_attempt entry for D#TEST_DISCUSSION ──────────
# Simulate what the script writes when it succeeds past dedup. Then verify
# the second call IS blocked.
echo ""
echo "--- AC2: second call (after spawn_attempt written) is blocked ---"

# Write a fresh spawn_attempt entry timestamped now
python3 - <<PYEOF 2>/dev/null || true
import json, pathlib, datetime
feed = pathlib.Path("$FEED_FILE")
feed.parent.mkdir(parents=True, exist_ok=True)
entry = {
    "event_type": "spawn_attempt",
    "role": "project-manager",
    "discussion": int("$TEST_DISCUSSION"),
    "message": "spawn_attempt: project-manager D#$TEST_DISCUSSION (test entry)",
    "ts": datetime.datetime.utcnow().isoformat() + "Z",
}
with open(feed, "a") as f:
    f.write(json.dumps(entry) + "\n")
PYEOF

# Verify the entry was written
ENTRY_COUNT=$(count_recent_pm_entries)
if [[ "$ENTRY_COUNT" -ge 1 ]]; then
  ok "AC2-setup: synthetic spawn_attempt entry for D#$TEST_DISCUSSION written to feed"
else
  fail "AC2-setup: could not write synthetic entry — test cannot continue"
fi

# Now run the second call — this MUST be blocked by pm_dedup
run_psc_sandboxed "$ws" --role project-manager --discussion "$TEST_DISCUSSION"
AC2_EXIT=$(psc_exit "$ws")

if [[ "$AC2_EXIT" -ne 0 ]] && psc_stderr "$ws" | grep -q "pm_dedup"; then
  ok "AC2: second call blocked by pm_dedup (exit $AC2_EXIT, correct)"
else
  if [[ "$AC2_EXIT" -eq 0 ]]; then
    fail "AC2: second call exited 0 — pm_dedup did not block"
  else
    fail "AC2: exited $AC2_EXIT but pm_dedup message not in stderr (different failure)"
    echo "  stderr: $(psc_stderr "$ws")"
  fi
fi

# ── AC3: executor role is unaffected by pm_dedup ──────────────────────────────
echo ""
echo "--- AC3: executor role not affected by pm_dedup ---"

# The feed now has a spawn_attempt for project-manager D#9999.
# An executor spawn for the same discussion must not be blocked by pm_dedup.
run_psc_sandboxed "$ws" --role executor --discussion "$TEST_DISCUSSION"

if psc_stderr "$ws" | grep -q "pm_dedup"; then
  fail "AC3: executor was blocked by pm_dedup (must never happen)"
else
  ok "AC3: executor not affected by pm_dedup (correct)"
fi

# ── AC4: pm_dedup does NOT fire for project-manager without --discussion ───────
echo ""
echo "--- AC4: pm_dedup skipped when no --discussion argument ---"

run_psc_sandboxed "$ws" --role project-manager

if psc_stderr "$ws" | grep -q "pm_dedup"; then
  fail "AC4: pm_dedup fired without --discussion (should be skipped)"
else
  ok "AC4: pm_dedup correctly skipped when no --discussion provided"
fi

# ── AC5: Z-suffix timestamp from 5h ago does NOT trigger pm_dedup ─────────────
# Regression for D#900: ts.rstrip("Z") discards the Z but leaves the timestamp
# naive, causing fromisoformat() to treat it as local time and potentially
# computing the wrong delta. With the fix (replace("Z", "+00:00")) the timestamp
# is parsed as UTC and correctly falls outside the 120s dedup window.
echo ""
echo "--- AC5: spawn_attempt from 5h ago (Z suffix) does NOT block new spawn ---"

# Clean up any recent entries (from AC2) before writing the 5h-ago entry
cleanup_feed

# Write a synthetic entry timestamped 5 hours ago with Z suffix
python3 - <<PYEOF 2>/dev/null || true
import json, pathlib, datetime
feed = pathlib.Path("$FEED_FILE")
feed.parent.mkdir(parents=True, exist_ok=True)
# 5 hours ago in UTC, formatted with Z suffix (the format the script writes)
five_hours_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)
ts = five_hours_ago.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
entry = {
    "event_type": "spawn_attempt",
    "role": "project-manager",
    "discussion": int("$TEST_DISCUSSION"),
    "message": "spawn_attempt: project-manager D#$TEST_DISCUSSION (5h-ago test entry)",
    "ts": ts,
}
with open(feed, "a") as f:
    f.write(json.dumps(entry) + "\n")
print(f"wrote entry with ts={ts}")
PYEOF

# The pre-spawn-check should NOT block on pm_dedup — entry is 5h old, well past
# the 120s window. (Other checks may fail for unrelated reasons; we only check
# that pm_dedup is NOT the failure cause.)
run_psc_sandboxed "$ws" --role project-manager --discussion "$TEST_DISCUSSION"

if psc_stderr "$ws" | grep -q "pm_dedup"; then
  fail "AC5: 5h-old Z-suffix entry incorrectly triggered pm_dedup (TZ parse bug)"
  echo "  stderr: $(psc_stderr "$ws")"
else
  ok "AC5: 5h-old entry not blocked by pm_dedup (Z-suffix parsed correctly as UTC)"
fi

echo ""
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
