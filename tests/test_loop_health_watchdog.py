"""
Tests for scripts/loop-health-watchdog.sh

Tests A, B, C verify the threshold detection, banner write/clear, and idempotency logic.
Tests D, E verify the staleness guard (LOOP_HEALTH_STALE_HOURS).
Uses tmp_path for loop.log isolation; points the script at a temp blackboard via BLACKBOARD_DIR.

Fixtures use PRODUCTION line format: "[HH:MM:SS] SUMMARY {<json>}"
where the JSON has "start"/"end" fields (ISO8601), not a "timestamp" field.
This matches the actual output of the loop runner (confirmed 2026-05-20).
"""

import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = REPO_ROOT / "scripts" / "loop-health-watchdog.sh"

BANNER_KEY = "dashboard/banner/loop-health"
LAST_ALERTED_KEY = "loop_health/last_alerted_count"


def _fresh_ts(minutes_ago: int = 5) -> str:
    """Return an ISO timestamp a few minutes in the past (guaranteed fresh for 6h guard)."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_summary(exit_code: int, start: str | None = None, end: str | None = None) -> str:
    """Generate a SUMMARY line in PRODUCTION format: '[HH:MM:SS] SUMMARY {...}'.

    The JSON uses 'start' and 'end' fields (ISO8601) — same as the real loop runner.
    No 'timestamp' field — that was the old fixture format that caused this bug.
    """
    if start is None:
        start = _fresh_ts()
    if end is None:
        end = start
    hhmm = datetime.now(timezone.utc).strftime("%H:%M:%S")
    duration = 0
    return (
        f"[{hhmm}] SUMMARY "
        f'{{"start": "{start}", "end": "{end}", '
        f'"duration_s": {duration}, "exit_code": {exit_code}, "timed_out": false}}'
    )


def run_watchdog(
    loop_log: Path,
    bb_dir: Path,
    threshold: int = 3,
    stale_hours: float | None = None,
) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(Path.home()),
        "LOOP_LOG_PATH": str(loop_log),
        "BLACKBOARD_DIR": str(bb_dir),
        "LOOP_HEALTH_THRESHOLD": str(threshold),
    }
    if stale_hours is not None:
        env["LOOP_HEALTH_STALE_HOURS"] = str(stale_hours)
    return subprocess.run(
        ["bash", str(WATCHDOG)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_a_three_trailing_failures_sets_banner(tmp_path):
    """Test A: 3 trailing non-zero exits -> banner written with consecutive_failures=3."""
    loop_log = tmp_path / "loop.log"
    bb_dir = tmp_path / "blackboard"
    bb_dir.mkdir()

    # Write 3 consecutive failures with fresh timestamps (within the 6h staleness window)
    loop_log.write_text("\n".join([
        make_summary(1, _fresh_ts(20)),
        make_summary(1, _fresh_ts(10)),
        make_summary(1, _fresh_ts(5)),
    ]) + "\n")

    result = run_watchdog(loop_log, bb_dir, threshold=3)
    assert result.returncode == 0, f"watchdog exited non-zero: {result.stderr}"

    bb = Blackboard(root=bb_dir)
    banner = bb.read(BANNER_KEY)
    assert banner is not None, "Expected banner key to be set"
    assert banner["consecutive_failures"] == 3
    assert banner["severity"] == "warning"
    assert "last 3 fires exited non-zero" in banner["message"]


def test_b_one_success_after_failures_clears_banner(tmp_path):
    """Test B: after 3 failures, one success appended -> banner deleted, last_alerted reset to 0."""
    loop_log = tmp_path / "loop.log"
    bb_dir = tmp_path / "blackboard"
    bb_dir.mkdir()

    # First: 3 failures to set banner (fresh timestamps)
    loop_log.write_text("\n".join([
        make_summary(1, _fresh_ts(30)),
        make_summary(1, _fresh_ts(20)),
        make_summary(1, _fresh_ts(10)),
    ]) + "\n")
    run_watchdog(loop_log, bb_dir, threshold=3)

    bb = Blackboard(root=bb_dir)
    assert bb.read(BANNER_KEY) is not None, "Pre-condition: banner should be set after 3 failures"

    # Append one success
    with loop_log.open("a") as f:
        f.write(make_summary(0, _fresh_ts(5)) + "\n")

    result = run_watchdog(loop_log, bb_dir, threshold=3)
    assert result.returncode == 0, f"watchdog exited non-zero: {result.stderr}"

    banner = bb.read(BANNER_KEY)
    assert banner is None, "Expected banner key to be deleted after success"

    last_alerted = bb.read(LAST_ALERTED_KEY)
    assert last_alerted == 0, f"Expected last_alerted_count to be 0, got {last_alerted}"


def test_c_two_failures_below_threshold_no_banner(tmp_path):
    """Test C: only 2 trailing failures with threshold=3 -> no banner written."""
    loop_log = tmp_path / "loop.log"
    bb_dir = tmp_path / "blackboard"
    bb_dir.mkdir()

    # One success then 2 failures (only 2 trailing non-zero, all fresh)
    loop_log.write_text("\n".join([
        make_summary(0, _fresh_ts(30)),
        make_summary(1, _fresh_ts(20)),
        make_summary(1, _fresh_ts(10)),
    ]) + "\n")

    result = run_watchdog(loop_log, bb_dir, threshold=3)
    assert result.returncode == 0, f"watchdog exited non-zero: {result.stderr}"

    bb = Blackboard(root=bb_dir)
    banner = bb.read(BANNER_KEY)
    assert banner is None, f"Expected no banner for 2 failures < threshold 3, got: {banner}"


def test_missing_log_exits_cleanly(tmp_path):
    """Watchdog exits 0 silently when loop.log does not exist."""
    loop_log = tmp_path / "nonexistent.log"
    bb_dir = tmp_path / "blackboard"
    bb_dir.mkdir()

    result = run_watchdog(loop_log, bb_dir)
    assert result.returncode == 0


def test_empty_log_exits_cleanly(tmp_path):
    """Watchdog exits 0 silently when loop.log exists but has no SUMMARY lines."""
    loop_log = tmp_path / "loop.log"
    loop_log.write_text("some other content\nno summaries here\n")
    bb_dir = tmp_path / "blackboard"
    bb_dir.mkdir()

    result = run_watchdog(loop_log, bb_dir)
    assert result.returncode == 0


def test_d_stale_failures_suppressed(tmp_path):
    """Test D: failures whose first start timestamp is older than stale_hours → no banner.

    The stale threshold is set to 6h via LOOP_HEALTH_STALE_HOURS.  The failure
    timestamps are 24h in the past, so the guard must suppress the alert.
    """
    loop_log = tmp_path / "loop.log"
    bb_dir = tmp_path / "blackboard"
    bb_dir.mkdir()

    # All three failures have start timestamps 24 hours ago
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    loop_log.write_text("\n".join([
        make_summary(1, old_ts),
        make_summary(1, old_ts),
        make_summary(1, old_ts),
    ]) + "\n")

    # Threshold=3, stale_hours=6 → 24h-old failures should be suppressed
    result = run_watchdog(loop_log, bb_dir, threshold=3, stale_hours=6.0)
    assert result.returncode == 0, f"watchdog exited non-zero: {result.stderr}"

    bb = Blackboard(root=bb_dir)
    banner = bb.read(BANNER_KEY)
    assert banner is None, (
        f"Expected NO banner for stale failures (24h old, threshold 6h), got: {banner}"
    )


def test_e_fresh_failures_still_alert(tmp_path):
    """Test E: failures whose first start timestamp is within stale_hours → banner IS written.

    The stale threshold is set to 6h.  The failure timestamps are 1 minute ago,
    so the alert must fire normally.
    """
    loop_log = tmp_path / "loop.log"
    bb_dir = tmp_path / "blackboard"
    bb_dir.mkdir()

    # Three failures 1 minute ago — clearly fresh
    fresh_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    loop_log.write_text("\n".join([
        make_summary(1, fresh_ts),
        make_summary(1, fresh_ts),
        make_summary(1, fresh_ts),
    ]) + "\n")

    result = run_watchdog(loop_log, bb_dir, threshold=3, stale_hours=6.0)
    assert result.returncode == 0, f"watchdog exited non-zero: {result.stderr}"

    bb = Blackboard(root=bb_dir)
    banner = bb.read(BANNER_KEY)
    assert banner is not None, "Expected banner for fresh failures (1min old, threshold 6h)"
    assert banner["consecutive_failures"] == 3
    assert banner["severity"] == "warning"
