"""Tests for compute_estimation_metrics min_samples gate.

Verifies that accuracy is null (None) when total_measured < 5 and a real
number when 5+ discussions have both estimated_hours and actual_hours.
"""

import pytest
from backend.kpi_engine import compute_estimation_metrics


def _discussion(est: float, act: float) -> dict:
    """Build a minimal discussion dict with both hours fields."""
    return {"estimated_hours": est, "actual_hours": act, "status": "DONE"}


def _frontmatter_discussion(est: float, act: float) -> dict:
    """Build a discussion with frontmatter/completion nesting."""
    return {
        "frontmatter": {"estimated_hours": est},
        "completion": {"actual_hours": act},
        "status": "DONE",
    }


class TestAccuracyNullBelowMinSamples:
    def test_zero_measured_returns_null_accuracy(self):
        result = compute_estimation_metrics([])
        assert result["accuracy"] is None
        assert result["total_measured"] == 0

    def test_one_measured_returns_null_accuracy(self):
        result = compute_estimation_metrics([_discussion(4.0, 5.0)])
        assert result["accuracy"] is None
        assert result["total_measured"] == 1

    def test_four_measured_returns_null_accuracy(self):
        discussions = [_discussion(float(i), float(i) + 0.5) for i in range(1, 5)]
        result = compute_estimation_metrics(discussions)
        assert result["accuracy"] is None
        assert result["total_measured"] == 4

    def test_accuracy_null_when_missing_actual(self):
        """Discussions missing actual_hours don't count toward total_measured."""
        discussions = [{"estimated_hours": 3.0, "status": "DONE"}] * 10
        result = compute_estimation_metrics(discussions)
        assert result["accuracy"] is None
        assert result["total_measured"] == 0

    def test_accuracy_null_when_missing_estimated(self):
        """Discussions missing estimated_hours don't count toward total_measured."""
        discussions = [{"actual_hours": 3.0, "status": "DONE"}] * 10
        result = compute_estimation_metrics(discussions)
        assert result["accuracy"] is None
        assert result["total_measured"] == 0


class TestAccuracyRealNumberAtMinSamples:
    def test_exactly_five_measured_returns_accuracy(self):
        # Perfect estimates → accuracy should be 1.0
        discussions = [_discussion(4.0, 4.0) for _ in range(5)]
        result = compute_estimation_metrics(discussions)
        assert result["accuracy"] is not None
        assert result["total_measured"] == 5
        assert result["accuracy"] == pytest.approx(1.0, abs=0.001)

    def test_ten_measured_returns_accuracy(self):
        discussions = [_discussion(10.0, 8.0) for _ in range(10)]
        result = compute_estimation_metrics(discussions)
        assert result["accuracy"] is not None
        assert result["total_measured"] == 10
        # accuracy per discussion = 1 - |10-8| / max(10,8) = 1 - 2/10 = 0.8
        assert result["accuracy"] == pytest.approx(0.8, abs=0.001)

    def test_frontmatter_nesting_counted(self):
        """Discussions using frontmatter/completion nesting are counted."""
        discussions = [_frontmatter_discussion(5.0, 6.0) for _ in range(5)]
        result = compute_estimation_metrics(discussions)
        assert result["accuracy"] is not None
        assert result["total_measured"] == 5

    def test_mixed_sources_counted(self):
        """Mix of top-level and nested fields both count toward total_measured."""
        discussions = (
            [_discussion(3.0, 3.0) for _ in range(3)]
            + [_frontmatter_discussion(3.0, 3.0) for _ in range(2)]
        )
        result = compute_estimation_metrics(discussions)
        assert result["total_measured"] == 5
        assert result["accuracy"] is not None


class TestMinSamplesConfig:
    def test_custom_min_samples_respected(self):
        """min_samples parameter changes the threshold."""
        # 3 measured discussions — below default 5 but above custom 3
        discussions = [_discussion(2.0, 2.0) for _ in range(3)]
        result = compute_estimation_metrics(discussions, min_samples=3)
        assert result["accuracy"] is not None
        assert result["min_samples"] == 3

    def test_min_samples_one_allows_single_discussion(self):
        result = compute_estimation_metrics([_discussion(5.0, 5.0)], min_samples=1)
        assert result["accuracy"] is not None
        assert result["total_measured"] == 1

    def test_min_samples_in_output(self):
        """min_samples is echoed back in the return dict."""
        result = compute_estimation_metrics([], min_samples=5)
        assert result["min_samples"] == 5

    def test_four_below_default_threshold(self):
        """4 samples with default min_samples=5 → null."""
        discussions = [_discussion(1.0, 1.0) for _ in range(4)]
        result_default = compute_estimation_metrics(discussions)
        assert result_default["accuracy"] is None

        result_lower = compute_estimation_metrics(discussions, min_samples=4)
        assert result_lower["accuracy"] is not None
