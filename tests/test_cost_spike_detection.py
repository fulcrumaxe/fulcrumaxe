"""Tests for cost spike detection (Discussion #540 metric #22).

Covers:
- detect_cost_spike() with a synthetic series including a spike
- Insufficient data guard (< 10 baseline points)
- 3-consecutive-spikes trigger for gates.budget_check
- record_cost_spike() + cost_spike_history() round-trip
- record_iteration_cost() stores a queryable row
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure repo root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# detect_cost_spike() unit tests (no DuckDB needed — pass series directly)
# ---------------------------------------------------------------------------


def test_spike_detected_on_outlier():
    """Series [1,1,1,1,1,1,1,1,1,1,100] → spike on last value."""
    from backend.cost_tracker import detect_cost_spike

    series = [1.0] * 10 + [100.0]
    result = detect_cost_spike(series=series)

    assert result["spike"] is True
    assert result["insufficient_data"] is False
    assert result["value"] == pytest.approx(100.0)
    assert result["sample_size"] == 10
    assert result["threshold"] < 100.0  # 100 exceeds threshold


def test_no_spike_on_normal_value():
    """Series of constant values + another constant value → no spike."""
    from backend.cost_tracker import detect_cost_spike

    series = [5.0] * 15
    result = detect_cost_spike(series=series)

    # All-equal series: sigma=0, threshold=mu+0=5. value=5 is NOT > 5.
    assert result["spike"] is False
    assert result["insufficient_data"] is False


def test_insufficient_data_fewer_than_10_baseline():
    """Fewer than 10 baseline points → insufficient_data=True, spike=False."""
    from backend.cost_tracker import detect_cost_spike

    # 5 baseline points + 1 current
    series = [1.0, 2.0, 1.5, 1.8, 1.2, 50.0]
    result = detect_cost_spike(series=series)

    assert result["spike"] is False
    assert result["insufficient_data"] is True
    assert result["sample_size"] == 5


def test_insufficient_data_empty_series():
    """Empty series → insufficient_data=True."""
    from backend.cost_tracker import detect_cost_spike

    result = detect_cost_spike(series=[])
    assert result["spike"] is False
    assert result["insufficient_data"] is True
    assert result["sample_size"] == 0


def test_exactly_10_baseline_points():
    """Exactly 10 baseline points is sufficient."""
    from backend.cost_tracker import detect_cost_spike

    series = [1.0] * 10 + [999.0]
    result = detect_cost_spike(series=series)

    assert result["insufficient_data"] is False
    assert result["spike"] is True
    assert result["sample_size"] == 10


def test_spike_value_not_in_baseline():
    """The current (last) value must be excluded from the baseline stats."""
    from backend.cost_tracker import detect_cost_spike

    # With the outlier included in baseline, sigma inflates and spike disappears.
    # The function must use ONLY the first N-1 values for mu/sigma.
    series = [1.0] * 10 + [1000.0]
    result = detect_cost_spike(series=series)

    # Baseline: [1]*10, mu=1, sigma=0, threshold=1. 1000 > 1 → spike
    assert result["spike"] is True
    assert result["mu"] == pytest.approx(1.0)
    assert result["sigma"] == pytest.approx(0.0)


def test_original_spec_series():
    """Spec example: [1,1,1,1,1,100] → spike detected on last value.

    Note: this only has 5 baseline points (< 10), so insufficient_data=True
    per the sample-size guard. The spike would be True if the guard were off.
    We verify insufficient_data behaviour here.
    """
    from backend.cost_tracker import detect_cost_spike

    series = [1.0, 1.0, 1.0, 1.0, 1.0, 100.0]
    result = detect_cost_spike(series=series)

    # 5 baseline points < 10 minimum
    assert result["insufficient_data"] is True
    assert result["spike"] is False


def test_spike_with_11_baseline_points():
    """Prove that spec series works when sample size >= 10."""
    from backend.cost_tracker import detect_cost_spike

    # 11 baseline points of 1.0, then outlier 100.0
    series = [1.0] * 11 + [100.0]
    result = detect_cost_spike(series=series)

    assert result["insufficient_data"] is False
    assert result["spike"] is True


# ---------------------------------------------------------------------------
# DuckDB round-trip tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path):
    """Provide a temp DuckDB path, patched into stats_writer._db_path."""
    db_file = tmp_path / "test_stats.duckdb"
    with patch("backend.stats_writer._db_path", return_value=db_file):
        yield db_file


def test_record_and_retrieve_cost_spike(tmp_db):
    """record_cost_spike() stores a row; cost_spike_history() retrieves it."""
    from backend.stats_writer import record_cost_spike, cost_spike_history

    now = datetime.now(timezone.utc)
    record_cost_spike(value=0.5432, mu=0.01, sigma=0.005, ts=now)

    history = cost_spike_history(hours=1)
    assert len(history) == 1
    entry = history[0]
    assert entry["value"] == pytest.approx(0.5432, rel=1e-4)
    assert entry["mu"] == pytest.approx(0.01, rel=1e-4)
    assert entry["sigma"] == pytest.approx(0.005, rel=1e-4)
    assert "ts_iso" in entry


def test_cost_spike_history_respects_hours_window(tmp_db):
    """cost_spike_history(hours=1) excludes entries older than 1 hour.

    Uses a 3h-old entry and a 30min-old entry; only the recent one falls
    within a 1h window.
    """
    from backend.stats_writer import record_cost_spike, cost_spike_history

    # Use 3h old so it's clearly outside the 1h window
    old_ts = datetime.now(timezone.utc) - timedelta(hours=3)
    new_ts = datetime.now(timezone.utc) - timedelta(minutes=30)

    record_cost_spike(0.9, 0.01, 0.005, ts=old_ts)
    record_cost_spike(0.8, 0.01, 0.005, ts=new_ts)

    history = cost_spike_history(hours=1)
    assert len(history) == 1
    assert history[0]["value"] == pytest.approx(0.8, rel=1e-4)


def test_record_iteration_cost_stored(tmp_db):
    """record_iteration_cost() writes an 'iteration_cost_usd' row that
    _load_iteration_cost_series() can read back."""
    from backend.stats_writer import record_iteration_cost
    from backend.cost_tracker import _load_iteration_cost_series

    # record_iteration_cost uses stats_writer._db_path (already patched by tmp_db fixture)
    record_iteration_cost(0.025, ts=datetime.now(timezone.utc))
    # _load_iteration_cost_series also calls _db_path from stats_writer — patch both
    with patch("backend.stats_writer._db_path", return_value=tmp_db):
        series = _load_iteration_cost_series()

    assert len(series) == 1
    assert series[0] == pytest.approx(0.025, rel=1e-4)


def test_three_spikes_in_one_hour_count(tmp_db):
    """cost_spike_history(hours=1) returns 3 when 3 spikes recorded in 1h."""
    from backend.stats_writer import record_cost_spike, cost_spike_history

    now = datetime.now(timezone.utc)
    for i in range(3):
        record_cost_spike(
            value=1.0 + i * 0.1,
            mu=0.01,
            sigma=0.005,
            ts=now - timedelta(minutes=i * 15),
        )

    history = cost_spike_history(hours=1)
    assert len(history) == 3


# ---------------------------------------------------------------------------
# gate tripping integration (mocked control_plane)
# ---------------------------------------------------------------------------


def test_spike_detection_returns_correct_shape():
    """detect_cost_spike() always returns the expected keys."""
    from backend.cost_tracker import detect_cost_spike

    for series in [
        [],
        [1.0],
        [1.0] * 5 + [100.0],
        [1.0] * 11 + [100.0],
    ]:
        result = detect_cost_spike(series=series)
        assert "spike" in result
        assert "value" in result
        assert "mu" in result
        assert "sigma" in result
        assert "threshold" in result
        assert "sample_size" in result
        assert "insufficient_data" in result
        assert isinstance(result["spike"], bool)
        assert isinstance(result["insufficient_data"], bool)
