"""
Tests for backend/loop_metrics_counters.py

HARD RULE: MUST NOT invoke `claude`, `claude -p`, `_start_loop_run`, or trigger
`/loop`. Test by mocking agent_feed.filter and subprocess.run. Discussion #439.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from backend.loop_metrics_counters import compute_counters

START = "2026-05-10T10:00:00Z"
END   = "2026-05-10T11:00:00Z"


def _feed_event(event_type: str, ts: str) -> dict:
    return {"event_type": event_type, "ts": ts, "role": "executor", "message": "test"}


def _gh_result(prs: list[dict], rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], rc, json.dumps(prs), "")


def _pr(merged_at: str) -> dict:
    return {"number": 1, "mergedAt": merged_at}


# ─── AC4 case 1: N spawn entries in window → agents_spawned == N ───────────
def test_agents_spawned_counts_spawn_entries():
    entries = [
        _feed_event("spawn",         "2026-05-10T10:05:00Z"),
        _feed_event("spawn_attempt", "2026-05-10T10:30:00Z"),
        _feed_event("spawn",         "2026-05-10T10:59:59Z"),
    ]
    with patch("backend.loop_metrics_counters._agent_feed.filter", return_value=iter(entries)), \
         patch("backend.loop_metrics_counters.subprocess.run", return_value=_gh_result([])):
        result = compute_counters(START, END)
    assert result["agents_spawned"] == 3


# ─── AC4 case 2: M PRs with mergedAt in window → prs_merged == M ──────────
def test_prs_merged_counts_from_gh():
    prs = [_pr("2026-05-10T10:05:00Z"), _pr("2026-05-10T10:45:00Z")]
    with patch("backend.loop_metrics_counters._agent_feed.filter", return_value=iter([])), \
         patch("backend.loop_metrics_counters.subprocess.run", return_value=_gh_result(prs)):
        result = compute_counters(START, END)
    assert result["prs_merged"] == 2


# ─── AC4 case 3: empty window → all four counters zero, no exception ──────
def test_empty_window_returns_zeros():
    with patch("backend.loop_metrics_counters._agent_feed.filter", return_value=iter([])), \
         patch("backend.loop_metrics_counters.subprocess.run", return_value=_gh_result([])), \
         patch("backend.loop_metrics_counters._load_snapshot", return_value={}):
        result = compute_counters(START, END)
    assert result["agents_spawned"] == 0
    assert result["prs_merged"] == 0
    assert result["discussions_scanned"] == 0
    assert result["prs_scanned"] == 0


# ─── AC4 case 4: malformed timestamps in source data → skipped silently ────
def test_malformed_timestamps_skipped():
    entries = [
        _feed_event("spawn", "NOT_A_TIMESTAMP"),
        _feed_event("spawn", ""),
        _feed_event("spawn", "2026-05-10T10:30:00Z"),  # only this one is valid
    ]
    prs = [_pr("INVALID"), _pr("2026-05-10T10:20:00Z")]  # only 2nd is valid
    with patch("backend.loop_metrics_counters._agent_feed.filter", return_value=iter(entries)), \
         patch("backend.loop_metrics_counters.subprocess.run", return_value=_gh_result(prs)):
        result = compute_counters(START, END)
    assert result["agents_spawned"] == 1
    assert result["prs_merged"] == 1


# ─── AC4 case 5: gh command fails → prs_merged = 0, no exception ──────────
def test_gh_failure_returns_zero_no_exception():
    failed = subprocess.CompletedProcess(["gh"], 1, "", "auth error")
    with patch("backend.loop_metrics_counters._agent_feed.filter", return_value=iter([])), \
         patch("backend.loop_metrics_counters.subprocess.run", return_value=failed):
        result = compute_counters(START, END)
    assert result["prs_merged"] == 0


# ─── Bonus: agent_feed.filter raises → agents_spawned = 0 ─────────────────
def test_feed_exception_returns_zero():
    with patch("backend.loop_metrics_counters._agent_feed.filter",
               side_effect=Exception("I/O error")), \
         patch("backend.loop_metrics_counters.subprocess.run",
               return_value=_gh_result([])):
        result = compute_counters(START, END)
    assert result["agents_spawned"] == 0


# ─── Bonus: invalid ISO strings for window → graceful zero ────────────────
def test_invalid_iso_strings_return_zeros():
    zero = {"agents_spawned": 0, "prs_merged": 0, "discussions_scanned": 0, "prs_scanned": 0}
    assert compute_counters("bad", END) == zero
    assert compute_counters(START, "bad") == zero


# ─── New: 2 spawns over 7m → spawned=2 duration=420 NOT spawned=0 dur=300 ─
def test_real_spawn_count_and_duration():
    """When a loop iteration runs with 2 spawns over 7m, the recorded row shows
    spawned=2 and duration=420 — not the hardcoded defaults of spawned=0 / 300s.
    """
    iter_start = "2026-05-10T12:00:00Z"
    iter_end   = "2026-05-10T12:07:00Z"  # exactly 7 minutes = 420 seconds
    entries = [
        _feed_event("spawn",         "2026-05-10T12:01:00Z"),
        _feed_event("spawn_attempt", "2026-05-10T12:04:00Z"),
    ]
    with patch("backend.loop_metrics_counters._agent_feed.filter", return_value=iter(entries)), \
         patch("backend.loop_metrics_counters.subprocess.run", return_value=_gh_result([])):
        result = compute_counters(iter_start, iter_end)

    assert result["agents_spawned"] == 2, (
        f"expected agents_spawned=2 but got {result['agents_spawned']} "
        "(audit_trail has no spawn actions; must read agent_feed.jsonl)"
    )

    # Duration is computed by append-loop-metrics.sh from iter_start/iter_end;
    # we verify the counters module returns correct spawn count so the caller
    # can write duration=420 instead of the hardcoded 300 default.
    from backend.loop_metrics_counters import _parse_iso
    start_dt = _parse_iso(iter_start)
    end_dt   = _parse_iso(iter_end)
    assert start_dt is not None and end_dt is not None
    duration_s = int((end_dt - start_dt).total_seconds())
    assert duration_s == 420, (
        f"expected duration=420 but computed {duration_s}s "
        "(end_ts - start_ts should equal 7 * 60)"
    )
