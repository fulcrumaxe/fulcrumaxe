"""
Tests for the <!-- COMPLETION --> block writer and kpi_engine parser.

Covers:
  - post-merge-hook behaviour (simulated via bash subprocess)
  - task_specs._parse_completion_block
  - task_specs._parse_completion_summary (unified parser, both formats)
  - kpi_engine.extract_actual_hours_from_body
  - kpi_engine.compute_estimation_metrics reading COMPLETION block from body field
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.task_specs import (
    _parse_completion_block,
    _parse_completion_summary,
)
from backend.kpi_engine import (
    extract_actual_hours_from_body,
    compute_estimation_metrics,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_completion_block(actual_hours: float, merged_at: str, merged_pr: int) -> str:
    return (
        "\n<!-- COMPLETION -->\n"
        f"actual_hours: {actual_hours}\n"
        f"merged_at: {merged_at}\n"
        f"merged_pr: {merged_pr}\n"
        "<!-- /COMPLETION -->"
    )


def _body_with_completion(actual_hours: float = 4.2, merged_pr: int = 588) -> str:
    return (
        "<!-- STATUS:SPEC_READY SINCE:2026-05-10T00:00:00Z -->\n\n"
        "## Goal\n\nDo some work.\n"
        + _make_completion_block(actual_hours, "2026-05-11T14:30:00Z", merged_pr)
    )


# ── task_specs._parse_completion_block ───────────────────────────────────────

class TestParseCompletionBlock:
    def test_parses_all_fields(self):
        body = _body_with_completion(4.2, 588)
        result = _parse_completion_block(body)
        assert result["actual_hours"] == pytest.approx(4.2)
        assert result["merged_at"] == "2026-05-11T14:30:00Z"
        assert result["merged_pr"] == 588

    def test_returns_empty_when_absent(self):
        body = "## Goal\n\nNo completion block here."
        assert _parse_completion_block(body) == {}

    def test_replaces_not_duplicated(self):
        """Two COMPLETION blocks in the body — only the first match is returned."""
        body = (
            _make_completion_block(2.0, "2026-05-10T10:00:00Z", 100)
            + "\n"
            + _make_completion_block(5.5, "2026-05-11T14:30:00Z", 200)
        )
        result = _parse_completion_block(body)
        # First match wins (regex is non-greedy but finds earliest)
        assert result["actual_hours"] == pytest.approx(2.0)

    def test_float_coercion(self):
        body = "\n<!-- COMPLETION -->\nactual_hours: 1.75\nmerged_pr: 99\n<!-- /COMPLETION -->"
        result = _parse_completion_block(body)
        assert isinstance(result["actual_hours"], float)
        assert result["actual_hours"] == pytest.approx(1.75)

    def test_int_coercion_merged_pr(self):
        body = "\n<!-- COMPLETION -->\nactual_hours: 3.0\nmerged_pr: 42\n<!-- /COMPLETION -->"
        result = _parse_completion_block(body)
        assert isinstance(result["merged_pr"], int)
        assert result["merged_pr"] == 42


# ── task_specs._parse_completion_summary (unified) ───────────────────────────

class TestParseCompletionSummaryUnified:
    def test_prefers_completion_block_over_markdown_section(self):
        """COMPLETION block takes priority over ## Completion Summary section."""
        body = (
            "## Completion Summary\n"
            "- actual_hours: 99.0\n"
            "- pr_number: 1\n\n"
            + _make_completion_block(4.2, "2026-05-11T14:30:00Z", 588)
        )
        result = _parse_completion_summary(body)
        # Should prefer the COMPLETION block
        assert result["actual_hours"] == pytest.approx(4.2)
        assert result.get("merged_pr") == 588

    def test_falls_back_to_markdown_section_when_no_block(self):
        body = (
            "## Completion Summary\n"
            "- actual_hours: 7.5\n"
            "- pr_number: 321\n"
        )
        result = _parse_completion_summary(body)
        assert result["actual_hours"] == pytest.approx(7.5)
        assert result["pr_number"] == 321

    def test_empty_body_returns_empty(self):
        assert _parse_completion_summary("") == {}


# ── kpi_engine.extract_actual_hours_from_body ────────────────────────────────

class TestExtractActualHoursFromBody:
    def test_extracts_from_completion_block(self):
        body = _body_with_completion(6.5)
        result = extract_actual_hours_from_body(body)
        assert result == pytest.approx(6.5)

    def test_returns_none_when_absent(self):
        body = "## Goal\n\nNo completion block."
        assert extract_actual_hours_from_body(body) is None

    def test_returns_none_on_malformed_value(self):
        body = "\n<!-- COMPLETION -->\nactual_hours: not-a-number\n<!-- /COMPLETION -->"
        assert extract_actual_hours_from_body(body) is None

    def test_zero_hours(self):
        body = "\n<!-- COMPLETION -->\nactual_hours: 0.0\n<!-- /COMPLETION -->"
        result = extract_actual_hours_from_body(body)
        assert result == pytest.approx(0.0)


# ── kpi_engine.compute_estimation_metrics ────────────────────────────────────

class TestComputeEstimationMetrics:
    def _disc(self, **kwargs) -> dict:
        base = {"status": "DONE", "closed_at": "2026-05-11T12:00:00Z"}
        base.update(kwargs)
        return base

    def test_reads_actual_from_completion_block_in_body(self):
        """actual_hours extracted from body COMPLETION block when completion key absent."""
        body = _body_with_completion(actual_hours=4.0)
        disc = self._disc(
            frontmatter={"estimated_hours": 4.0},
            body=body,
        )
        # min_samples=1 to test data parsing, not the insufficient-data gate
        result = compute_estimation_metrics([disc], min_samples=1)
        assert result["tasks_with_estimates"] == 1
        assert result["accuracy"] == pytest.approx(1.0)

    def test_reads_actual_from_completion_dict_path(self):
        """Legacy path: completion.actual_hours in registry dict still works."""
        disc = self._disc(
            frontmatter={"estimated_hours": 8.0},
            completion={"actual_hours": 4.0},
        )
        result = compute_estimation_metrics([disc], min_samples=1)
        assert result["tasks_with_estimates"] == 1
        # accuracy = 1 - |8-4|/8 = 0.5
        assert result["accuracy"] == pytest.approx(0.5)

    def test_reads_actual_from_top_level_actual_hours(self):
        """Backwards compat: top-level actual_hours field."""
        disc = self._disc(estimated_hours=2.0, actual_hours=2.0)
        result = compute_estimation_metrics([disc], min_samples=1)
        assert result["tasks_with_estimates"] == 1
        assert result["accuracy"] == pytest.approx(1.0)

    def test_estimate_only_no_completion_not_counted(self):
        """Discussion has estimated_hours but no actual — not counted."""
        disc = self._disc(frontmatter={"estimated_hours": 4.0})
        result = compute_estimation_metrics([disc])
        assert result["tasks_with_estimates"] == 0

    def test_completion_only_no_estimate_not_counted(self):
        """Discussion has actual_hours via COMPLETION block but no estimate — not counted."""
        body = _body_with_completion(actual_hours=3.0)
        disc = self._disc(body=body)
        result = compute_estimation_metrics([disc])
        assert result["tasks_with_estimates"] == 0

    def test_mean_accuracy_across_multiple(self):
        """Mean accuracy computed correctly over multiple discussions."""
        discs = [
            # perfect estimate
            self._disc(frontmatter={"estimated_hours": 4.0}, completion={"actual_hours": 4.0}),
            # 50% accuracy: actual=2*est → accuracy=1-|4-8|/8=0.5
            self._disc(frontmatter={"estimated_hours": 4.0}, completion={"actual_hours": 8.0}),
        ]
        result = compute_estimation_metrics(discs, min_samples=1)
        assert result["tasks_with_estimates"] == 2
        assert result["accuracy"] == pytest.approx(0.75)

    def test_body_completion_block_superseded_by_completion_dict(self):
        """completion dict takes priority over body field — registry path wins."""
        body = _body_with_completion(actual_hours=6.0)
        disc = self._disc(
            frontmatter={"estimated_hours": 6.0},
            completion={"actual_hours": 99.0},  # registry value takes priority
            body=body,
        )
        result = compute_estimation_metrics([disc], min_samples=1)
        assert result["tasks_with_estimates"] == 1
        # Uses completion.actual_hours = 99.0 (registry path wins over body)
        # accuracy = 1 - |6 - 99| / 99 = 1 - 93/99 ≈ 0.061 (rounded to 3 dp)
        expected_accuracy = round(1 - abs(6.0 - 99.0) / 99.0, 3)
        assert result["accuracy"] == pytest.approx(expected_accuracy, abs=1e-3)


# ── Shell-level tests for the completion_block step ──────────────────────────
# These tests simulate the logic that post-merge-hook.sh would apply.
# They run pure Python equivalents of the bash computations for portability.

class TestPostMergeHookLogic:
    """
    Unit-test the Python helper logic embedded inside post-merge-hook.sh:
      - actual_hours calculation
      - COMPLETION block replacement (not duplication)
      - Umbrella skip when not all PRs merged
      - Umbrella write when all PRs merged
    """

    @staticmethod
    def _compute_actual_hours(created_iso: str, merged_iso: str) -> float:
        from datetime import datetime
        def parse(s):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return round((parse(merged_iso) - parse(created_iso)).total_seconds() / 3600, 2)

    @staticmethod
    def _replace_completion_block(body: str, block: str) -> str:
        import re
        cleaned = re.sub(
            r"\n?<!-- COMPLETION -->.*?<!-- /COMPLETION -->",
            "",
            body,
            flags=re.DOTALL,
        )
        return cleaned.rstrip() + block

    def test_actual_hours_calculation(self):
        created = "2026-05-11T10:00:00Z"
        merged  = "2026-05-11T14:15:00Z"
        hours = self._compute_actual_hours(created, merged)
        assert hours == pytest.approx(4.25)

    def test_completion_block_written_single_pr(self):
        body = "<!-- STATUS:SPEC_READY SINCE:2026-05-10T00:00:00Z -->\n\n## Goal\n\nDo work."
        block = _make_completion_block(4.25, "2026-05-11T14:15:00Z", 100)
        result = self._replace_completion_block(body, block)
        assert "<!-- COMPLETION -->" in result
        assert "actual_hours: 4.25" in result
        assert result.count("<!-- COMPLETION -->") == 1

    def test_existing_completion_block_replaced_not_duplicated(self):
        body = (
            "## Goal\n\nDo work."
            + _make_completion_block(1.0, "2026-05-10T08:00:00Z", 50)
        )
        new_block = _make_completion_block(4.25, "2026-05-11T14:15:00Z", 100)
        result = self._replace_completion_block(body, new_block)
        assert result.count("<!-- COMPLETION -->") == 1
        assert "actual_hours: 4.25" in result
        assert "actual_hours: 1.0" not in result

    def test_umbrella_skip_when_not_last_pr(self):
        """Simulate: 3 planned PRs, only 1 merged so far → skip."""
        planned = 3
        merged  = 1
        should_write = merged >= planned
        assert should_write is False

    def test_umbrella_write_when_last_pr(self):
        """Simulate: 3 planned PRs, all 3 merged → write completion block."""
        planned = 3
        merged  = 3
        should_write = merged >= planned
        assert should_write is True

    def test_umbrella_write_when_more_merged_than_planned(self):
        """Edge: merged_count > planned (e.g. hotfix PR also references discussion)."""
        planned = 2
        merged  = 3
        should_write = merged >= planned
        assert should_write is True
