"""
Tests for backend/task_specs.py — YAML frontmatter parsing and formatting.

Covers:
  - _parse_frontmatter: valid, missing, malformed YAML, edge cases
  - _parse_completion_summary: valid, missing, edge cases
  - format_frontmatter / format_completion_summary round-trips
  - compute_estimation_metrics (via kpi_engine) using frontmatter data
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.task_specs import (
    _parse_frontmatter,
    _parse_completion_summary,
    format_frontmatter,
    format_completion_summary,
)
from backend.kpi_engine import compute_estimation_metrics


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------

_VALID_BODY = """\
<!-- STATUS:SPEC_READY SINCE:2026-04-10T00:00:00Z -->
---
type: feature
complexity_points: 3
estimated_hours: 2.0
depends_on: [10, 22]
tags: [workflow, orchestration]
---

Some spec text here.
"""

_NO_FRONTMATTER_BODY = """\
<!-- STATUS:DISCUSSING SINCE:2026-04-10T00:00:00Z -->

Just plain body text with no YAML.
"""

_FRONTMATTER_NO_STATUS_BODY = """\
---
type: feature
---

Body with YAML but no STATUS comment before it.
"""

_MALFORMED_YAML_BODY = """\
<!-- STATUS:SPEC_READY SINCE:2026-04-10T00:00:00Z -->
---
type: [unclosed
complexity_points: 3
---
"""

_PARTIAL_FIELDS_BODY = """\
<!-- STATUS:SPEC_READY SINCE:2026-04-10T00:00:00Z -->
---
type: bug
complexity_points: 1
---
"""

_EMPTY_FRONTMATTER_BODY = """\
<!-- STATUS:SPEC_READY SINCE:2026-04-10T00:00:00Z -->
---
---
"""


def test_parse_frontmatter_valid():
    result = _parse_frontmatter(_VALID_BODY)
    assert result["type"] == "feature"
    assert result["complexity_points"] == 3
    assert result["estimated_hours"] == 2.0
    assert result["depends_on"] == [10, 22]
    assert result["tags"] == ["workflow", "orchestration"]


def test_parse_frontmatter_no_frontmatter_returns_empty():
    result = _parse_frontmatter(_NO_FRONTMATTER_BODY)
    assert result == {}


def test_parse_frontmatter_yaml_not_after_status_returns_empty():
    # YAML block present but no STATUS comment before it — should not parse.
    result = _parse_frontmatter(_FRONTMATTER_NO_STATUS_BODY)
    assert result == {}


def test_parse_frontmatter_malformed_yaml_returns_empty():
    result = _parse_frontmatter(_MALFORMED_YAML_BODY)
    assert result == {}


def test_parse_frontmatter_partial_fields():
    result = _parse_frontmatter(_PARTIAL_FIELDS_BODY)
    assert result["type"] == "bug"
    assert result["complexity_points"] == 1
    # Fields not in the YAML are simply absent — no KeyError
    assert "estimated_hours" not in result
    assert "tags" not in result


def test_parse_frontmatter_empty_block_returns_empty():
    result = _parse_frontmatter(_EMPTY_FRONTMATTER_BODY)
    assert result == {}


def test_parse_frontmatter_empty_string_returns_empty():
    assert _parse_frontmatter("") == {}


# ---------------------------------------------------------------------------
# _parse_completion_summary
# ---------------------------------------------------------------------------

_COMPLETION_BODY = """\
<!-- STATUS:DONE PR:#99 SINCE:2026-04-10T20:00:00Z -->

## Spec

Some spec text.

## Completion Summary
- actual_hours: 1.5
- files_changed: 4
- lines_added: 280
- lines_removed: 15
- pr_number: 99
- merged_at: 2026-04-10T20:00:00Z
"""

_NO_COMPLETION_BODY = """\
<!-- STATUS:IMPLEMENTING SINCE:2026-04-10T00:00:00Z -->

Body without a completion summary.
"""

_PARTIAL_COMPLETION_BODY = """\
<!-- STATUS:DONE SINCE:2026-04-10T20:00:00Z -->

## Completion Summary
- actual_hours: 3.0
- pr_number: 55
"""


def test_parse_completion_summary_valid():
    result = _parse_completion_summary(_COMPLETION_BODY)
    assert result["actual_hours"] == 1.5
    assert result["files_changed"] == 4
    assert result["lines_added"] == 280
    assert result["lines_removed"] == 15
    assert result["pr_number"] == 99
    assert result["merged_at"] == "2026-04-10T20:00:00Z"


def test_parse_completion_summary_absent_returns_empty():
    result = _parse_completion_summary(_NO_COMPLETION_BODY)
    assert result == {}


def test_parse_completion_summary_partial_fields():
    result = _parse_completion_summary(_PARTIAL_COMPLETION_BODY)
    assert result["actual_hours"] == 3.0
    assert result["pr_number"] == 55
    assert "files_changed" not in result


def test_parse_completion_summary_empty_string():
    assert _parse_completion_summary("") == {}


# ---------------------------------------------------------------------------
# format_frontmatter round-trip
# ---------------------------------------------------------------------------

def test_format_frontmatter_round_trip_defaults():
    block = format_frontmatter()
    body = f"<!-- STATUS:DISCUSSING SINCE:2026-04-10T00:00:00Z -->\n{block}"
    parsed = _parse_frontmatter(body)
    assert parsed["type"] == "feature"
    assert parsed["complexity_points"] == 3
    assert parsed["estimated_hours"] == 2.0
    assert parsed["depends_on"] == []
    assert parsed["tags"] == []


def test_format_frontmatter_round_trip_custom():
    block = format_frontmatter(
        type="bug",
        complexity_points=1,
        estimated_hours=0.5,
        depends_on=[5, 12],
        tags=["critical", "auth"],
    )
    body = f"<!-- STATUS:DISCUSSING SINCE:2026-04-10T00:00:00Z -->\n{block}"
    parsed = _parse_frontmatter(body)
    assert parsed["type"] == "bug"
    assert parsed["complexity_points"] == 1
    assert parsed["estimated_hours"] == 0.5
    assert parsed["depends_on"] == [5, 12]
    assert parsed["tags"] == ["critical", "auth"]


def test_format_frontmatter_starts_and_ends_with_fence():
    block = format_frontmatter()
    lines = block.splitlines()
    assert lines[0] == "---"
    assert lines[-1] == "---"


# ---------------------------------------------------------------------------
# format_completion_summary round-trip
# ---------------------------------------------------------------------------

def test_format_completion_summary_round_trip():
    block = format_completion_summary(
        actual_hours=2.5,
        files_changed=3,
        lines_added=120,
        lines_removed=10,
        pr_number=77,
        merged_at="2026-04-10T21:00:00Z",
    )
    parsed = _parse_completion_summary(block)
    assert parsed["actual_hours"] == 2.5
    assert parsed["files_changed"] == 3
    assert parsed["lines_added"] == 120
    assert parsed["lines_removed"] == 10
    assert parsed["pr_number"] == 77
    assert parsed["merged_at"] == "2026-04-10T21:00:00Z"


def test_format_completion_summary_integer_coercion():
    block = format_completion_summary(
        actual_hours=1.0,
        files_changed=2,
        lines_added=50,
        lines_removed=5,
        pr_number=10,
        merged_at="2026-04-10T00:00:00Z",
    )
    parsed = _parse_completion_summary(block)
    assert isinstance(parsed["files_changed"], int)
    assert isinstance(parsed["lines_added"], int)
    assert isinstance(parsed["pr_number"], int)
    assert isinstance(parsed["actual_hours"], float)


# ---------------------------------------------------------------------------
# compute_estimation_metrics (kpi_engine integration)
# ---------------------------------------------------------------------------

def _make_discussion(status="DONE", estimated_hours=None, actual_hours=None,
                     complexity_points=None, closed_at="2026-04-10T20:00:00Z"):
    """Helper to build a registry-style discussion dict."""
    d: dict = {"status": status, "closed_at": closed_at}
    if estimated_hours is not None or actual_hours is not None or complexity_points is not None:
        fm = {}
        if estimated_hours is not None:
            fm["estimated_hours"] = estimated_hours
        if complexity_points is not None:
            fm["complexity_points"] = complexity_points
        if fm:
            d["frontmatter"] = fm
        if actual_hours is not None:
            d["completion"] = {"actual_hours": actual_hours}
    return d


def test_estimation_metrics_empty():
    result = compute_estimation_metrics([])
    assert result["tasks_with_estimates"] == 0
    assert result["accuracy"] is None
    assert result["complexity_velocity"] is None
    assert result["bias"] is None


def test_estimation_metrics_no_data():
    discussions = [_make_discussion(status="IMPLEMENTING")]
    result = compute_estimation_metrics(discussions)
    assert result["tasks_with_estimates"] == 0


def test_estimation_accuracy_perfect():
    discussions = [_make_discussion(estimated_hours=2.0, actual_hours=2.0)]
    # min_samples=1 to test the math with a single sample (not the insufficient-data gate)
    result = compute_estimation_metrics(discussions, min_samples=1)
    assert result["tasks_with_estimates"] == 1
    assert result["accuracy"] == 1.0


def test_estimation_accuracy_off_by_half():
    # estimated=4, actual=2 → accuracy = 1 - abs(4-2)/max(4,2) = 1 - 2/4 = 0.5
    discussions = [_make_discussion(estimated_hours=4.0, actual_hours=2.0)]
    result = compute_estimation_metrics(discussions, min_samples=1)
    assert result["tasks_with_estimates"] == 1
    assert abs(result["accuracy"] - 0.5) < 0.001


def test_estimation_accuracy_mean_of_multiple():
    # Two tasks: accuracy 1.0 and 0.5 → mean = 0.75
    discussions = [
        _make_discussion(estimated_hours=2.0, actual_hours=2.0),  # acc=1.0
        _make_discussion(estimated_hours=4.0, actual_hours=2.0),  # acc=0.5
    ]
    result = compute_estimation_metrics(discussions, min_samples=1)
    assert result["tasks_with_estimates"] == 2
    assert abs(result["accuracy"] - 0.75) < 0.001


def test_estimation_bias_positive_underestimate():
    # actual > estimated → positive bias (underestimated)
    discussions = [
        _make_discussion(estimated_hours=2.0, actual_hours=4.0),  # bias = +2
        _make_discussion(estimated_hours=3.0, actual_hours=5.0),  # bias = +2
    ]
    result = compute_estimation_metrics(discussions)
    assert result["bias"] == 2.0


def test_estimation_bias_negative_overestimate():
    # actual < estimated → negative bias (overestimated)
    discussions = [_make_discussion(estimated_hours=6.0, actual_hours=2.0)]
    result = compute_estimation_metrics(discussions)
    assert result["bias"] == -4.0


def test_complexity_velocity_computed():
    discussions = [
        _make_discussion(status="DONE", complexity_points=3, closed_at="2026-04-10T00:00:00Z"),
        _make_discussion(status="DONE", complexity_points=5, closed_at="2026-04-03T00:00:00Z"),
    ]
    result = compute_estimation_metrics(discussions)
    assert result["complexity_velocity"] is not None
    assert result["complexity_velocity"] > 0


def test_complexity_velocity_only_done_counted():
    discussions = [
        _make_discussion(status="IMPLEMENTING", complexity_points=5),
        _make_discussion(status="DONE", complexity_points=2, closed_at="2026-04-10T00:00:00Z"),
    ]
    result = compute_estimation_metrics(discussions)
    # Only the DONE task contributes to velocity.
    assert result["complexity_velocity"] is not None


def test_estimation_skips_zero_estimated():
    # estimated_hours=0 is invalid and should be skipped.
    discussions = [_make_discussion(estimated_hours=0.0, actual_hours=3.0)]
    result = compute_estimation_metrics(discussions)
    assert result["tasks_with_estimates"] == 0
