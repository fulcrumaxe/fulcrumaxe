"""Tests for backend/cost_formatter.py — format_cost_table().

Covers:
  - Normal case: multiple roles, non-zero costs, full token counts
  - Single-role Discussion
  - Zero-cost entry: no comment should be emitted
  - Empty input: graceful return of empty string
  - Missing optional fields: total_input/output_tokens absent
  - agent_breakdown has zero-cost role that is skipped
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.cost_formatter import format_cost_table


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_data(
    discussion: int = 42,
    total_cost_usd: float = 1.2345,
    total_input_tokens: int = 50000,
    total_output_tokens: int = 12000,
    agent_count: int = 3,
    agent_breakdown: dict | None = None,
) -> dict:
    if agent_breakdown is None:
        agent_breakdown = {
            "executor": 0.8,
            "code-reviewer": 0.3,
            "acceptance-tester": 0.1345,
        }
    return {
        "discussion": discussion,
        "total_cost_usd": total_cost_usd,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "agent_count": agent_count,
        "agent_breakdown": agent_breakdown,
    }


# ---------------------------------------------------------------------------
# Normal case
# ---------------------------------------------------------------------------

def test_normal_case_contains_header():
    data = _make_data()
    md = format_cost_table(data)
    assert "| Role | Input tokens | Output tokens | Cost (USD) |" in md


def test_normal_case_contains_total_row():
    data = _make_data(
        total_cost_usd=1.2345,
        total_input_tokens=50000,
        total_output_tokens=12000,
    )
    md = format_cost_table(data)
    assert "**Total**" in md
    assert "50,000" in md
    assert "12,000" in md
    assert "$1.2345" in md


def test_normal_case_lists_all_nonzero_roles():
    data = _make_data(
        agent_breakdown={
            "executor": 0.8,
            "code-reviewer": 0.3,
            "acceptance-tester": 0.1345,
        }
    )
    md = format_cost_table(data)
    assert "executor" in md
    assert "code-reviewer" in md
    assert "acceptance-tester" in md


def test_normal_case_discussion_number_in_output():
    data = _make_data(discussion=999)
    md = format_cost_table(data)
    assert "#999" in md


def test_normal_case_agent_count_in_output():
    data = _make_data(agent_count=5)
    md = format_cost_table(data)
    assert "5 agent runs" in md


def test_single_agent_grammatical_singular():
    data = _make_data(agent_count=1)
    md = format_cost_table(data)
    assert "1 agent run" in md
    assert "1 agent runs" not in md


# ---------------------------------------------------------------------------
# Single-role Discussion
# ---------------------------------------------------------------------------

def test_single_role():
    data = _make_data(
        total_cost_usd=0.5,
        agent_count=1,
        agent_breakdown={"executor": 0.5},
    )
    md = format_cost_table(data)
    assert "executor" in md
    assert "**Total**" in md
    assert "$0.5000" in md


# ---------------------------------------------------------------------------
# Zero-cost — no comment should be produced
# ---------------------------------------------------------------------------

def test_zero_cost_returns_empty_string():
    data = _make_data(total_cost_usd=0.0, agent_breakdown={"executor": 0.0})
    result = format_cost_table(data)
    assert result == ""


def test_zero_cost_explicit_zero():
    data = {
        "discussion": 1,
        "total_cost_usd": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "agent_count": 0,
        "agent_breakdown": {},
    }
    assert format_cost_table(data) == ""


# ---------------------------------------------------------------------------
# Empty / malformed input
# ---------------------------------------------------------------------------

def test_empty_dict_returns_empty_string():
    assert format_cost_table({}) == ""


def test_none_returns_empty_string():
    assert format_cost_table(None) == ""  # type: ignore[arg-type]


def test_non_dict_returns_empty_string():
    assert format_cost_table("not a dict") == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Missing optional fields
# ---------------------------------------------------------------------------

def test_missing_token_fields_does_not_raise():
    data = {
        "discussion": 7,
        "total_cost_usd": 0.25,
        "agent_count": 1,
        "agent_breakdown": {"executor": 0.25},
        # total_input_tokens and total_output_tokens intentionally absent
    }
    md = format_cost_table(data)
    assert "**Total**" in md
    assert "$0.2500" in md


def test_missing_agent_breakdown_shows_total_only():
    data = {
        "discussion": 8,
        "total_cost_usd": 0.1,
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "agent_count": 1,
        # agent_breakdown absent
    }
    md = format_cost_table(data)
    assert "**Total**" in md


# ---------------------------------------------------------------------------
# Zero-cost roles in breakdown are skipped
# ---------------------------------------------------------------------------

def test_zero_cost_role_in_breakdown_skipped():
    data = _make_data(
        total_cost_usd=0.8,
        agent_breakdown={
            "executor": 0.8,
            "free-rider": 0.0,
        }
    )
    md = format_cost_table(data)
    assert "executor" in md
    # free-rider rows should be absent (zero cost filtered out)
    assert "free-rider" not in md
