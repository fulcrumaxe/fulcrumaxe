"""Tests for backend/stats/anomaly_detector.py

Covers:
  AC1 – detect() is a pure function returning list[Anomaly].
  AC6 – flags the actual orphan_worktree_rate 2,016,000% swing from history.
  AC7 – first iteration (no prior value) → no anomaly.
  Edge cases: value transitions through 0, stable values, ratio exactly at threshold.
"""

from __future__ import annotations

import pytest

from backend.stats.anomaly_detector import Anomaly, detect


# ── Helpers ───────────────────────────────────────────────────────────────────


def _row(metric: str, value: float, ts: str = "2026-05-18T10:00:00Z", project_tag: str = "") -> dict:
    return {"metric": metric, "value": value, "ts": ts, "project_tag": project_tag}


# ── AC1: stable values → no anomaly ──────────────────────────────────────────


def test_stable_values_no_flag():
    """Values within threshold: no anomaly returned."""
    prev = _row("agents_spawned", 5.0, "2026-05-18T09:00:00Z")
    curr = _row("agents_spawned", 6.0, "2026-05-18T10:00:00Z")
    result = detect(prev, curr)
    assert result == []


def test_value_doubled_below_default_threshold():
    """A 2x swing is well below the 10x default — should not flag."""
    prev = _row("agents_spawned", 10.0)
    curr = _row("agents_spawned", 20.0)
    result = detect(prev, curr)
    assert result == []


def test_exactly_at_threshold_no_flag():
    """Ratio == threshold is NOT a flag (strictly greater-than)."""
    prev = _row("orphan_worktree_rate", 1.0)
    curr = _row("orphan_worktree_rate", 10.0)
    result = detect(prev, curr)
    assert result == []


# ── AC6: the historic orphan_worktree_rate 2,016,000% swing ──────────────────


def test_orphan_worktree_rate_historic_swing():
    """Reproduces the 2026-05-18 orphan_worktree_rate bug: ~20,160x swing.

    The tile showed 2,016,000% (= 20,160x). Typical value is 0.001 (0.1%).
    A malformed value of ~20.16 should trigger the 10x threshold.
    """
    typical = 0.001          # 0.1% — normal
    malformed = 20.16        # 2016000% rendered as a fraction
    prev = _row("orphan_worktree_rate", typical, "2026-05-18T09:00:00Z")
    curr = _row("orphan_worktree_rate", malformed, "2026-05-18T10:00:00Z")
    result = detect(prev, curr)
    assert len(result) == 1
    a = result[0]
    assert a.metric == "orphan_worktree_rate"
    assert a.ratio > 10.0
    assert a.threshold == 10.0


def test_large_swing_flagged():
    """Any metric with a >10x swing is flagged by default."""
    prev = _row("some_metric", 1.0)
    curr = _row("some_metric", 100.0)
    result = detect(prev, curr)
    assert len(result) == 1
    assert result[0].ratio == pytest.approx(100.0)


def test_drop_also_flagged():
    """Detect drops (large decreases) — ratio is always current/prev or prev/current, whichever > 1."""
    prev = _row("some_metric", 100.0)
    curr = _row("some_metric", 1.0)
    result = detect(prev, curr)
    assert len(result) == 1
    assert result[0].ratio == pytest.approx(100.0)


# ── AC7: first iteration (no prior value) → skip ─────────────────────────────


def test_no_prior_value_returns_empty():
    """When there is no previous row, detect() is never called with a pair
    (the I/O layer skips it). But if called with mismatched metrics it returns [].
    """
    prev = _row("different_metric", 5.0)
    curr = _row("some_metric", 50.0)
    result = detect(prev, curr)
    assert result == []


# ── Zero / NaN guards ─────────────────────────────────────────────────────────


def test_prev_zero_skipped():
    """prev == 0 → skip (avoid division by zero and false zero-start positives)."""
    prev = _row("some_metric", 0.0)
    curr = _row("some_metric", 999.0)
    result = detect(prev, curr)
    assert result == []


def test_curr_zero_skipped():
    """curr == 0 → skip."""
    prev = _row("some_metric", 10.0)
    curr = _row("some_metric", 0.0)
    result = detect(prev, curr)
    assert result == []


def test_both_zero_skipped():
    prev = _row("some_metric", 0.0)
    curr = _row("some_metric", 0.0)
    assert detect(prev, curr) == []


def test_none_value_skipped():
    prev = {"metric": "x", "value": None, "ts": "2026-05-18T09:00:00Z", "project_tag": ""}
    curr = _row("x", 50.0)
    assert detect(prev, curr) == []


# ── Config override ───────────────────────────────────────────────────────────


def test_custom_threshold_respected():
    """Config dict overrides the default threshold for a metric."""
    prev = _row("my_metric", 1.0)
    curr = _row("my_metric", 4.0)
    # Without override: 4x < 10x default → no flag
    assert detect(prev, curr) == []
    # With override: 4x > 2x custom → flag
    result = detect(prev, curr, config={"my_metric": 2.0})
    assert len(result) == 1
    assert result[0].threshold == 2.0


def test_cost_metric_tighter_threshold():
    """Cost metrics have a 5x threshold — a 7x swing should flag."""
    prev = _row("total_cost_usd", 1.0)
    curr = _row("total_cost_usd", 7.0)
    result = detect(prev, curr)
    assert len(result) == 1
    assert result[0].threshold == 5.0


def test_duration_metric_looser_threshold():
    """Duration metrics have a 20x threshold — a 15x swing should NOT flag."""
    prev = _row("duration_s", 10.0)
    curr = _row("duration_s", 150.0)  # 15x
    result = detect(prev, curr)
    assert result == []


# ── Anomaly.format_log_line ───────────────────────────────────────────────────


def test_format_log_line():
    a = Anomaly(
        metric="orphan_worktree_rate",
        project_tag="",
        prev_value=0.001,
        current_value=20.16,
        ratio=20160.0,
        threshold=10.0,
        ts="2026-05-18T10:00:00Z",
    )
    line = a.format_log_line()
    assert "orphan_worktree_rate" in line
    assert "20160" in line
    assert "→" in line
