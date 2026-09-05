"""
Tests for backend/kpi_engine.py — compute_velocity, compute_idle_rate,
compute_pr_cycle_time, compute_estimation_accuracy.

All functions accept lists directly — no file I/O required.
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.kpi_engine import (
    compute_velocity,
    compute_idle_rate,
    compute_pr_cycle_time,
    compute_estimation_accuracy,
    compute_estimation_metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _hours_ago_iso(h: float) -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=h)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# compute_velocity
# ---------------------------------------------------------------------------

def test_velocity_empty_list():
    result = compute_velocity([])
    assert result["total_done"] == 0
    assert result["last_24h"] == 0
    assert result["all_time_per_day"] == 0.0


def test_velocity_no_done_discussions():
    discussions = [
        {"status": "IMPLEMENTING", "closed_at": _hours_ago_iso(2)},
        {"status": "SPEC_READY", "closed_at": None},
    ]
    result = compute_velocity(discussions)
    assert result["total_done"] == 0
    assert result["last_24h"] == 0


def test_velocity_counts_recent_done():
    discussions = [
        {"status": "DONE", "closed_at": _hours_ago_iso(1)},
        {"status": "DONE", "closed_at": _hours_ago_iso(3)},
        {"status": "DONE", "closed_at": _hours_ago_iso(100)},  # older than 24h
    ]
    result = compute_velocity(discussions)
    assert result["total_done"] == 3
    assert result["last_24h"] == 2


def test_velocity_all_time_per_day_positive():
    discussions = [
        {"status": "DONE", "closed_at": _hours_ago_iso(48)},
        {"status": "DONE", "closed_at": _hours_ago_iso(24)},
    ]
    result = compute_velocity(discussions)
    assert result["all_time_per_day"] > 0
    assert result["total_done"] == 2


def test_velocity_done_without_closed_at():
    discussions = [
        {"status": "DONE", "closed_at": None},
        {"status": "DONE", "closed_at": ""},
    ]
    result = compute_velocity(discussions)
    # DONE entries without timestamps still count in total_done
    assert result["total_done"] == 2
    assert result["last_24h"] == 0


# ---------------------------------------------------------------------------
# compute_idle_rate
# ---------------------------------------------------------------------------

def test_idle_rate_empty_metrics():
    result = compute_idle_rate([])
    assert result["total_iterations"] == 0
    assert result["last_24h_pct"] is None
    assert result["all_time_pct"] is None


def test_idle_rate_all_idle():
    metrics = [
        {"idle": True, "timestamp": _hours_ago_iso(1)},
        {"idle": True, "timestamp": _hours_ago_iso(2)},
    ]
    result = compute_idle_rate(metrics)
    assert result["all_time_pct"] == 100.0


def test_idle_rate_none_idle():
    metrics = [
        {"idle": False, "timestamp": _hours_ago_iso(1)},
        {"idle": False, "timestamp": _hours_ago_iso(2)},
    ]
    result = compute_idle_rate(metrics)
    assert result["all_time_pct"] == 0.0


def test_idle_rate_mixed():
    metrics = [
        {"idle": True, "timestamp": _hours_ago_iso(1)},
        {"idle": False, "timestamp": _hours_ago_iso(2)},
        {"idle": True, "timestamp": _hours_ago_iso(3)},
        {"idle": False, "timestamp": _hours_ago_iso(4)},
    ]
    result = compute_idle_rate(metrics)
    assert result["all_time_pct"] == 50.0
    assert result["total_iterations"] == 4


def test_idle_rate_recent_vs_all_time():
    # 2 recent (within 24h), 1 old; recent 1/2 idle, all-time 2/3 not-idle
    metrics = [
        {"idle": False, "timestamp": _hours_ago_iso(200)},  # old
        {"idle": False, "timestamp": _hours_ago_iso(1)},    # recent
        {"idle": True,  "timestamp": _hours_ago_iso(2)},    # recent
    ]
    result = compute_idle_rate(metrics)
    assert result["last_24h_pct"] == 50.0
    assert result["total_iterations"] == 3


# ---------------------------------------------------------------------------
# compute_pr_cycle_time
# ---------------------------------------------------------------------------

def test_pr_cycle_time_no_done():
    discussions = [{"status": "IMPLEMENTING", "created_at": _hours_ago_iso(5), "closed_at": _hours_ago_iso(1)}]
    result = compute_pr_cycle_time(discussions)
    assert result["total_measured"] == 0
    assert result["mean_hours"] is None
    assert result["median_hours"] is None


def test_pr_cycle_time_single_done():
    discussions = [
        {
            "status": "DONE",
            "created_at": _hours_ago_iso(10),
            "closed_at": _hours_ago_iso(4),
        }
    ]
    result = compute_pr_cycle_time(discussions)
    assert result["total_measured"] == 1
    assert result["mean_hours"] is not None
    assert abs(result["mean_hours"] - 6.0) < 0.1


def test_pr_cycle_time_multiple_done():
    discussions = [
        {"status": "DONE", "created_at": _hours_ago_iso(10), "closed_at": _hours_ago_iso(8)},  # 2h
        {"status": "DONE", "created_at": _hours_ago_iso(20), "closed_at": _hours_ago_iso(16)},  # 4h
    ]
    result = compute_pr_cycle_time(discussions)
    assert result["total_measured"] == 2
    assert abs(result["mean_hours"] - 3.0) < 0.1
    assert abs(result["median_hours"] - 3.0) < 0.1


def test_pr_cycle_time_skips_missing_timestamps():
    discussions = [
        {"status": "DONE", "created_at": None, "closed_at": _hours_ago_iso(1)},
        {"status": "DONE", "created_at": _hours_ago_iso(5), "closed_at": None},
        {"status": "DONE", "created_at": _hours_ago_iso(6), "closed_at": _hours_ago_iso(3)},  # valid
    ]
    result = compute_pr_cycle_time(discussions)
    assert result["total_measured"] == 1


# ---------------------------------------------------------------------------
# compute_estimation_accuracy
# ---------------------------------------------------------------------------

def test_estimation_accuracy_no_estimates():
    discussions = [{"status": "DONE"}, {"status": "DONE", "estimated_hours": None}]
    result = compute_estimation_accuracy(discussions)
    assert result["tasks_with_estimates"] == 0
    assert result["mean_absolute_error_hours"] is None
    assert result["within_1_5x_pct"] is None


def test_estimation_accuracy_perfect_estimate():
    discussions = [
        {"estimated_hours": 4, "actual_hours": 4},
    ]
    result = compute_estimation_accuracy(discussions)
    assert result["tasks_with_estimates"] == 1
    assert result["mean_absolute_error_hours"] == 0.0
    assert result["within_1_5x_pct"] == 100.0


def test_estimation_accuracy_within_1_5x_boundary():
    # actual = 1.5 * estimated → within boundary (<=)
    discussions = [
        {"estimated_hours": 4.0, "actual_hours": 6.0},  # 6 <= 4*1.5 → within
        {"estimated_hours": 4.0, "actual_hours": 7.0},  # 7 > 4*1.5 → outside
    ]
    result = compute_estimation_accuracy(discussions)
    assert result["tasks_with_estimates"] == 2
    assert result["within_1_5x_pct"] == 50.0


def test_estimation_accuracy_skips_zero_estimate():
    discussions = [
        {"estimated_hours": 0, "actual_hours": 3},
    ]
    result = compute_estimation_accuracy(discussions)
    assert result["tasks_with_estimates"] == 0


def test_estimation_accuracy_multiple_entries():
    discussions = [
        {"estimated_hours": 2, "actual_hours": 3},   # error=1
        {"estimated_hours": 8, "actual_hours": 4},   # error=4
    ]
    result = compute_estimation_accuracy(discussions)
    assert result["tasks_with_estimates"] == 2
    assert abs(result["mean_absolute_error_hours"] - 2.5) < 0.01


# ---------------------------------------------------------------------------
# Regression tests: completion-block lookup for backfill data (D#616)
# ---------------------------------------------------------------------------

def _completion_discussion(est: float, act: float) -> dict:
    """Simulate a backfill entry: estimated_hours and actual_hours only in
    the completion block — no frontmatter, no top-level fields."""
    return {
        "status": "DONE",
        "completion": {
            "estimated_hours": est,
            "actual_hours": act,
        },
    }


def test_estimation_accuracy_reads_completion_block():
    """compute_estimation_accuracy must use completion.estimated_hours."""
    discussions = [_completion_discussion(4.0, 4.0)]
    result = compute_estimation_accuracy(discussions)
    assert result["tasks_with_estimates"] == 1
    assert result["mean_absolute_error_hours"] == 0.0
    assert result["within_1_5x_pct"] == 100.0


def test_estimation_accuracy_completion_block_error():
    """Error calculation is correct when hours come from completion block."""
    discussions = [
        _completion_discussion(4.0, 6.0),  # error=2, within 1.5x (6 <= 6)
        _completion_discussion(4.0, 7.0),  # error=3, outside 1.5x (7 > 6)
    ]
    result = compute_estimation_accuracy(discussions)
    assert result["tasks_with_estimates"] == 2
    assert abs(result["mean_absolute_error_hours"] - 2.5) < 0.01
    assert result["within_1_5x_pct"] == 50.0


def test_estimation_metrics_reads_completion_estimated_hours():
    """compute_estimation_metrics must count pairs where estimated_hours is
    only in completion block (the backfill format)."""
    discussions = [_completion_discussion(3.0, 3.0) for _ in range(5)]
    result = compute_estimation_metrics(discussions)
    assert result["total_measured"] == 5
    assert result["accuracy"] is not None
    assert result["accuracy"] == pytest.approx(1.0, abs=0.001)


def test_estimation_metrics_31_backfill_pairs():
    """Simulate 31 backfill-style pairs — accuracy must be non-null and
    total_measured must match (exercises the >= min_samples path)."""
    discussions = [_completion_discussion(float(i), float(i) * 1.1) for i in range(1, 32)]
    result = compute_estimation_metrics(discussions)
    assert result["total_measured"] == 31
    assert result["accuracy"] is not None
    assert 0.0 < result["accuracy"] <= 1.0


def test_estimation_metrics_completion_does_not_double_count():
    """A discussion with estimated_hours in both frontmatter and completion
    should only count once, using the frontmatter value (higher priority)."""
    discussion = {
        "status": "DONE",
        "frontmatter": {"estimated_hours": 2.0},
        "completion": {"estimated_hours": 99.0, "actual_hours": 2.0},
    }
    result = compute_estimation_metrics([discussion] * 5)
    assert result["total_measured"] == 5
    # accuracy should be 1.0 (frontmatter est=2.0 vs actual=2.0, not 99 vs 2)
    assert result["accuracy"] == pytest.approx(1.0, abs=0.001)
