"""Tests for backend/stats/anomaly_config.py — threshold_for() and module constants.

Covers:
  - DEFAULT_THRESHOLD is a positive float
  - METRIC_THRESHOLDS maps str → positive float for every entry
  - threshold_for returns the exact per-metric override when the metric is listed
  - threshold_for falls back to DEFAULT_THRESHOLD for unknown metrics
  - threshold_for handles empty string, whitespace-only, and numeric-looking names
  - All listed cost metrics use the tighter 5x threshold
  - All listed duration metrics use the looser 20x threshold
  - Rate and count metrics use the 10x threshold
  - threshold_for is idempotent (same input → same output on repeated calls)
  - METRIC_THRESHOLDS does not contain zero or negative values
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.stats.anomaly_config import (
    DEFAULT_THRESHOLD,
    METRIC_THRESHOLDS,
    threshold_for,
)


# ---------------------------------------------------------------------------
# Module-level constant invariants
# ---------------------------------------------------------------------------


def test_default_threshold_is_positive_float() -> None:
    assert isinstance(DEFAULT_THRESHOLD, float)
    assert DEFAULT_THRESHOLD > 0.0


def test_metric_thresholds_is_dict_of_str_to_positive_float() -> None:
    assert isinstance(METRIC_THRESHOLDS, dict)
    for name, value in METRIC_THRESHOLDS.items():
        assert isinstance(name, str), f"key {name!r} is not str"
        assert isinstance(value, float), f"value for {name!r} is not float"
        assert value > 0.0, f"threshold for {name!r} must be positive, got {value}"


def test_no_zero_or_negative_thresholds_in_metric_thresholds() -> None:
    """Defends against future accidental zero/negative additions."""
    for name, value in METRIC_THRESHOLDS.items():
        assert value > 0.0, f"{name!r} has non-positive threshold {value}"


# ---------------------------------------------------------------------------
# threshold_for — known metrics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metric", [
    "total_cost_usd",
    "budget_used_usd",
    "tokens_per_iter",
    "team_lead_tokens_per_iter",
])
def test_cost_metrics_use_5x_threshold(metric: str) -> None:
    """Cost metrics must be flagged at 5x — tighter than default."""
    assert threshold_for(metric) == 5.0


@pytest.mark.parametrize("metric", [
    "duration_s",
    "loop_duration_s",
    "agent_duration_s",
])
def test_duration_metrics_use_20x_threshold(metric: str) -> None:
    """Duration metrics must be flagged at 20x — looser than default."""
    assert threshold_for(metric) == 20.0


@pytest.mark.parametrize("metric", [
    "orphan_worktree_rate",
    "wasted_tokens_ratio",
    "impersonation_rate",
    "fail_rate",
    "agents_spawned",
    "prs_merged",
    "hard_rule_violation_count",
])
def test_rate_and_count_metrics_use_10x_threshold(metric: str) -> None:
    """Rate and count metrics use the same 10x as the default."""
    assert threshold_for(metric) == 10.0


def test_threshold_for_returns_exact_override_not_default() -> None:
    """Sanity check: a cost metric must NOT return DEFAULT_THRESHOLD."""
    # total_cost_usd is 5.0; DEFAULT_THRESHOLD is 10.0 — they differ.
    assert threshold_for("total_cost_usd") != DEFAULT_THRESHOLD
    assert threshold_for("total_cost_usd") == METRIC_THRESHOLDS["total_cost_usd"]


def test_all_listed_metrics_return_their_exact_value() -> None:
    """threshold_for must be consistent with the METRIC_THRESHOLDS dict."""
    for name, expected in METRIC_THRESHOLDS.items():
        assert threshold_for(name) == expected, (
            f"threshold_for({name!r}) = {threshold_for(name)!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# threshold_for — unknown / edge-case metric names
# ---------------------------------------------------------------------------


def test_unknown_metric_returns_default_threshold() -> None:
    assert threshold_for("completely_unknown_metric_xyz") == DEFAULT_THRESHOLD


def test_empty_string_returns_default_threshold() -> None:
    assert threshold_for("") == DEFAULT_THRESHOLD


def test_whitespace_only_name_returns_default_threshold() -> None:
    assert threshold_for("   ") == DEFAULT_THRESHOLD


def test_numeric_looking_name_returns_default_threshold() -> None:
    assert threshold_for("42") == DEFAULT_THRESHOLD


def test_partial_match_does_not_leak_override() -> None:
    """'total_cost' is not 'total_cost_usd' — must not return 5x."""
    assert threshold_for("total_cost") == DEFAULT_THRESHOLD


def test_case_sensitive_lookup() -> None:
    """'TOTAL_COST_USD' must not match 'total_cost_usd'."""
    assert threshold_for("TOTAL_COST_USD") == DEFAULT_THRESHOLD
    assert threshold_for("Duration_s") == DEFAULT_THRESHOLD


def test_trailing_space_not_matched() -> None:
    """'duration_s ' (with trailing space) must not match 'duration_s'."""
    assert threshold_for("duration_s ") == DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_threshold_for_is_idempotent_for_known_metric() -> None:
    result1 = threshold_for("total_cost_usd")
    result2 = threshold_for("total_cost_usd")
    assert result1 == result2


def test_threshold_for_is_idempotent_for_unknown_metric() -> None:
    result1 = threshold_for("nonexistent_metric")
    result2 = threshold_for("nonexistent_metric")
    assert result1 == result2


# ---------------------------------------------------------------------------
# Return-type invariant
# ---------------------------------------------------------------------------


def test_threshold_for_always_returns_float() -> None:
    """Return type must always be float — callers may do float arithmetic."""
    assert isinstance(threshold_for("total_cost_usd"), float)
    assert isinstance(threshold_for("duration_s"), float)
    assert isinstance(threshold_for("unknown_metric_abc"), float)
