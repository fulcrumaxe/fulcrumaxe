#!/usr/bin/env bash
# scripts/loop-health-watchdog.sh — detect consecutive /loop iteration failures and alert.
#
# Reads .autonomous-team/loop.log for SUMMARY {...} lines, counts trailing consecutive
# non-zero exit_code entries, and writes a blackboard banner when the threshold is met.
#
# Production line format: "[HH:MM:SS] SUMMARY {...}"
# The grep and Python parser both handle the optional [HH:MM:SS] prefix.
#
# Staleness guard: if the first failure in the trailing run has a timestamp older than
# LOOP_HEALTH_STALE_HOURS (default 6), the alert is suppressed.  This prevents the
# watchdog from re-firing needs-boss alerts when the loop.log has old, unrecovered
# failures that have since been addressed (e.g. a prompt-lane CLI path change in May 2026).
#
# Environment overrides (for testing):
#   LOOP_LOG_PATH            — override path to loop.log (default: .autonomous-team/loop.log)
#   LOOP_HEALTH_THRESHOLD    — override failure threshold (default: config or 3)
#   LOOP_HEALTH_STALE_HOURS  — failures older than this many hours are ignored (default: 6)
#   BLACKBOARD_DIR           — override blackboard root directory
#
# Exit code: always 0 — watchdog must never break /loop step 7.5.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOOP_LOG="${LOOP_LOG_PATH:-$REPO_ROOT/.autonomous-team/loop.log}"
BB_DIR="${BLACKBOARD_DIR:-$REPO_ROOT/.autonomous-team/blackboard}"
BANNER_KEY="dashboard/banner/loop-health"
LAST_ALERTED_KEY="loop_health/last_alerted_count"

# Exit silently if log missing or unreadable
if [[ ! -f "$LOOP_LOG" ]]; then
  exit 0
fi

# Extract SUMMARY lines — production format: "[HH:MM:SS] SUMMARY {...}"
# grep is NOT anchored so it matches with or without the timestamp prefix.
SUMMARIES=$(grep -E 'SUMMARY \{' "$LOOP_LOG" 2>/dev/null || true)
if [[ -z "$SUMMARIES" ]]; then
  exit 0
fi

# Delegate all analysis and blackboard writes to an inline Python script.
# Python handles JSON safely; bash just collects the SUMMARY lines and passes them.
SUMMARY_TMP=$(mktemp)
printf '%s\n' "$SUMMARIES" > "$SUMMARY_TMP"

python3 - "$SUMMARY_TMP" "$BB_DIR" "$REPO_ROOT" <<'PYEOF' 2>/dev/null || true
import sys
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

summary_file = sys.argv[1]
bb_dir = sys.argv[2]
repo_root = sys.argv[3]

sys.path.insert(0, repo_root)
from backend.blackboard import Blackboard

BANNER_KEY = "dashboard/banner/loop-health"
LAST_ALERTED_KEY = "loop_health/last_alerted_count"

# Resolve threshold: env > control_plane > default 3
threshold_env = os.environ.get("LOOP_HEALTH_THRESHOLD", "")
if threshold_env.isdigit():
    threshold = int(threshold_env)
else:
    try:
        result = subprocess.run(
            ["python3", f"{repo_root}/backend/control_plane.py", "get",
             "loop_health.consecutive_failure_threshold"],
            capture_output=True, text=True, timeout=5
        )
        cfg_val = result.stdout.strip().strip('"')
        threshold = int(cfg_val) if cfg_val.isdigit() else 3
    except Exception:
        threshold = 3

# Staleness threshold: failures whose first occurrence is older than this are suppressed.
# Prevents re-firing alerts from ancient loop.log entries that have since been resolved.
stale_hours_env = os.environ.get("LOOP_HEALTH_STALE_HOURS", "")
try:
    stale_hours = float(stale_hours_env) if stale_hours_env else 6.0
except ValueError:
    stale_hours = 6.0

# Parse SUMMARY lines and count trailing consecutive non-zero exits.
# Production line format: "[HH:MM:SS] SUMMARY {<json>}"
# The JSON timestamp is in the "start" field (ISO8601); fallback to "end".
consecutive = 0
last_exit = 0
last_ts = ""

with open(summary_file) as f:
    for raw_line in f:
        line = raw_line.strip()
        # Find the "SUMMARY " marker — may be preceded by "[HH:MM:SS] "
        marker = "SUMMARY "
        idx = line.find(marker)
        if idx == -1:
            continue
        json_str = line[idx + len(marker):]
        try:
            entry = json.loads(json_str)
        except json.JSONDecodeError:
            continue
        exit_code = entry.get("exit_code", 0)
        # Use "start" field (production format); fall back to "end" then "timestamp"
        ts = entry.get("start") or entry.get("end") or entry.get("timestamp") or ""
        if exit_code != 0:
            consecutive += 1
            last_exit = exit_code
            if not last_ts and ts:
                last_ts = ts
        else:
            consecutive = 0
            last_exit = 0
            last_ts = ""

bb = Blackboard(root=bb_dir)
now_dt = datetime.now(timezone.utc)
now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

# Staleness guard: if the first failure in the trailing run is older than stale_hours,
# skip alerting.  The log entry exists but the failure is no longer actionable.
if consecutive >= threshold and last_ts:
    try:
        failure_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        age_hours = (now_dt - failure_dt).total_seconds() / 3600
        if age_hours > stale_hours:
            # Stale — clear any existing banner and bail out without alerting.
            existing = bb.read(BANNER_KEY)
            if existing is not None:
                bb.delete(BANNER_KEY)
                bb.write(LAST_ALERTED_KEY, 0, updated_by="loop-health-watchdog")
            sys.exit(0)
    except (ValueError, OverflowError):
        pass  # unparseable timestamp → treat as fresh, proceed normally

if consecutive >= threshold:
    # Write banner
    banner = {
        "severity": "warning",
        "message": f"Scheduled loop failing: last {consecutive} fires exited non-zero",
        "consecutive_failures": consecutive,
        "last_exit_code": last_exit,
        "last_summary_at": last_ts or now,
        "updated_at": now,
    }
    bb.write(BANNER_KEY, banner, updated_by="loop-health-watchdog")

    # Idempotent team-log: only post when count increases
    last_alerted = bb.read(LAST_ALERTED_KEY) or 0
    if isinstance(last_alerted, str):
        try:
            last_alerted = int(last_alerted)
        except ValueError:
            last_alerted = 0

    if consecutive > last_alerted:
        hhmm = datetime.now().strftime("%H:%M")
        # Include path to the most recent failing loop-run JSON for direct inspection
        try:
            sys.path.insert(0, repo_root)
            from backend.loop_runs import latest_failing_run_path
            run_path = latest_failing_run_path() or ""
        except Exception:
            run_path = ""
        run_hint = f" — see {run_path}" if run_path else ""
        log_msg = (
            f"[{hhmm}] loop-health-watchdog: {consecutive} consecutive non-zero "
            f"loop iterations (last exit={last_exit}){run_hint}"
        )
        subprocess.run(
            ["bash", f"{repo_root}/scripts/rotate-team-log.sh", "comment", log_msg],
            capture_output=True, timeout=30
        )
        bb.write(LAST_ALERTED_KEY, consecutive, updated_by="loop-health-watchdog")
else:
    # Below threshold — clear banner if it exists
    existing = bb.read(BANNER_KEY)
    if existing is not None:
        bb.delete(BANNER_KEY)
        bb.write(LAST_ALERTED_KEY, 0, updated_by="loop-health-watchdog")

PYEOF

rm -f "$SUMMARY_TMP"
exit 0
