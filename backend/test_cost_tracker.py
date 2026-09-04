"""
Unit tests for backend/cost_tracker.py

Run with:
    python -m pytest backend/test_cost_tracker.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from backend.cost_tracker import CostTracker, _compute_cost, _DEFAULT_PRICING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bb_with_records(records: list[dict]) -> MagicMock:
    """Return a mock Blackboard pre-populated with the given spend records."""
    bb = MagicMock()
    keys = [f"budget/agents/{r['agent_id']}" for r in records]
    bb.list_keys.return_value = keys

    # Map each key to its record
    key_map = {f"budget/agents/{r['agent_id']}": r for r in records}
    bb.read.side_effect = lambda k: key_map.get(k)
    return bb


# ---------------------------------------------------------------------------
# Test: pricing lookup
# ---------------------------------------------------------------------------


def test_pricing_lookup_known_model():
    """Known model should use its explicit rates."""
    pricing = {
        "claude-opus-4-20250514": {"input_per_1k": 0.015, "output_per_1k": 0.075},
        "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    }
    cost = _compute_cost(1000, 1000, "claude-opus-4-20250514", pricing)
    assert cost == pytest.approx(0.015 + 0.075)


def test_pricing_lookup_missing_model_falls_back_to_default():
    """Unknown model should fall back to the 'default' pricing entry."""
    pricing = {
        "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    }
    cost = _compute_cost(1000, 1000, "unknown-model-xyz", pricing)
    expected = 0.003 + 0.015
    assert cost == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Test: cost computation with known values
# ---------------------------------------------------------------------------


def test_cost_computation_known_values():
    """Verify formula: cost = (input/1000 * input_rate) + (output/1000 * output_rate)."""
    pricing = {"default": {"input_per_1k": 0.003, "output_per_1k": 0.015}}
    # 2000 input + 500 output with default pricing
    cost = _compute_cost(2000, 500, "default", pricing)
    expected = (2000 / 1000 * 0.003) + (500 / 1000 * 0.015)  # 0.006 + 0.0075 = 0.0135
    assert cost == pytest.approx(expected)


def test_cost_computation_opus_rates():
    """Opus has 5x more expensive output; verify ratio holds."""
    pricing = {
        "claude-opus-4-20250514": {"input_per_1k": 0.015, "output_per_1k": 0.075},
    }
    cost = _compute_cost(0, 1000, "claude-opus-4-20250514", pricing)
    assert cost == pytest.approx(0.075)


# ---------------------------------------------------------------------------
# Test: zero-token edge case
# ---------------------------------------------------------------------------


def test_zero_token_cost_is_zero():
    """Recording zero tokens should produce zero cost."""
    pricing = {"default": {"input_per_1k": 0.003, "output_per_1k": 0.015}}
    cost = _compute_cost(0, 0, "default", pricing)
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Test: aggregate computations
# ---------------------------------------------------------------------------


def test_get_session_cost_single_agent():
    """Single agent record should aggregate correctly."""
    records = [
        {
            "agent_id": "executor-161-1",
            "agent": "executor",
            "input": 1000,
            "output": 500,
            "model": "claude-sonnet-4-20250514",
            "finished": "2026-04-10T12:00:00+00:00",
            "discussion": 161,
        }
    ]
    bb = _make_bb_with_records(records)
    ct = CostTracker(bb=bb)
    # Patch pricing to use known values
    ct._pricing = {
        "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
        "claude-sonnet-4-20250514": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    }

    result = ct.get_session_cost()

    expected_cost = (1000 / 1000 * 0.003) + (500 / 1000 * 0.015)  # 0.003 + 0.0075 = 0.0105
    assert result["total_cost_usd"] == pytest.approx(0.0105, abs=1e-6)
    assert len(result["by_agent"]) == 1
    assert result["by_agent"][0]["agent_id"] == "executor-161-1"

    assert len(result["by_discussion"]) == 1
    assert result["by_discussion"][0]["discussion"] == 161

    assert len(result["model_breakdown"]) == 1
    assert result["model_breakdown"][0]["model"] == "claude-sonnet-4-20250514"


def test_get_session_cost_multiple_models():
    """Multiple models should produce separate model_breakdown entries."""
    records = [
        {
            "agent_id": "executor-161-1",
            "agent": "executor",
            "input": 1000,
            "output": 1000,
            "model": "claude-sonnet-4-20250514",
            "finished": "2026-04-10T12:00:00+00:00",
        },
        {
            "agent_id": "code-reviewer-161-1",
            "agent": "code-reviewer",
            "input": 2000,
            "output": 500,
            "model": "kimi-k2-0711",
            "finished": "2026-04-10T12:05:00+00:00",
        },
    ]
    bb = _make_bb_with_records(records)
    ct = CostTracker(bb=bb)
    ct._pricing = {
        "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
        "claude-sonnet-4-20250514": {"input_per_1k": 0.003, "output_per_1k": 0.015},
        "kimi-k2-0711": {"input_per_1k": 0.0006, "output_per_1k": 0.002},
    }

    result = ct.get_session_cost()

    sonnet_cost = (1000 / 1000 * 0.003) + (1000 / 1000 * 0.015)  # 0.018
    kimi_cost = (2000 / 1000 * 0.0006) + (500 / 1000 * 0.002)   # 0.0012 + 0.001 = 0.0022
    expected_total = sonnet_cost + kimi_cost

    assert result["total_cost_usd"] == pytest.approx(expected_total, abs=1e-6)
    assert len(result["model_breakdown"]) == 2


def test_get_session_cost_empty_blackboard():
    """Empty blackboard should return zero cost and empty breakdowns."""
    bb = MagicMock()
    bb.list_keys.return_value = []
    ct = CostTracker(bb=bb)

    result = ct.get_session_cost()

    assert result["total_cost_usd"] == 0.0
    assert result["by_agent"] == []
    assert result["by_discussion"] == []
    assert result["model_breakdown"] == []


# ---------------------------------------------------------------------------
# Test: get_summary format
# ---------------------------------------------------------------------------


def test_get_summary_returns_four_decimal_total():
    """get_summary total_cost_usd should be a float rounded to 4 decimal places."""
    bb = MagicMock()
    bb.list_keys.return_value = []
    ct = CostTracker(bb=bb)

    summary = ct.get_summary()

    assert "total_cost_usd" in summary
    assert "model_breakdown" in summary
    # Verify the value is a float
    assert isinstance(summary["total_cost_usd"], float)
    # Verify rounding to 4 decimal places (0.0000 for empty)
    assert summary["total_cost_usd"] == round(summary["total_cost_usd"], 4)


# ---------------------------------------------------------------------------
# Test: default model fallback in record
# ---------------------------------------------------------------------------


def test_default_model_fallback_when_missing_from_record():
    """Records without a 'model' key should use the 'default' pricing entry."""
    records = [
        {
            "agent_id": "exec-no-model",
            "agent": "executor",
            "input": 1000,
            "output": 1000,
            # No 'model' key
            "finished": "2026-04-10T12:00:00+00:00",
        }
    ]
    bb = _make_bb_with_records(records)
    ct = CostTracker(bb=bb)
    ct._pricing = {
        "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    }

    result = ct.get_session_cost()

    expected = (1000 / 1000 * 0.003) + (1000 / 1000 * 0.015)  # 0.018
    assert result["total_cost_usd"] == pytest.approx(expected, abs=1e-6)
    assert result["model_breakdown"][0]["model"] == "default"


# ---------------------------------------------------------------------------
# Test: by-discussion extended fields (Discussion #367)
# ---------------------------------------------------------------------------


def test_by_discussion_extended_fields():
    """Each by_discussion entry must include all six required fields."""
    records = [
        {
            "agent_id": "executor-367-1",
            "agent": "executor",
            "input": 2000,
            "output": 800,
            "model": "claude-sonnet-4-20250514",
            "finished": "2026-05-09T10:00:00+00:00",
            "discussion": 367,
        },
        {
            "agent_id": "code-reviewer-367-1",
            "agent": "code-reviewer",
            "input": 1500,
            "output": 400,
            "model": "claude-sonnet-4-20250514",
            "finished": "2026-05-09T10:30:00+00:00",
            "discussion": 367,
        },
    ]
    bb = _make_bb_with_records(records)
    ct = CostTracker(bb=bb)
    ct._pricing = {
        "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
        "claude-sonnet-4-20250514": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    }

    result = ct.get_session_cost()
    assert len(result["by_discussion"]) == 1

    entry = result["by_discussion"][0]
    # All six required fields must be present.
    assert "discussion" in entry
    assert "cost_usd" in entry
    assert "total_cost_usd" in entry
    assert "agent_count" in entry
    assert "total_input_tokens" in entry
    assert "total_output_tokens" in entry
    assert "agents" in entry

    # cost_usd and total_cost_usd must be equal (total_cost_usd is an alias).
    assert entry["cost_usd"] == entry["total_cost_usd"]

    # Token totals must reflect both agents.
    assert entry["total_input_tokens"] == 3500
    assert entry["total_output_tokens"] == 1200
    assert entry["agent_count"] == 2
    assert entry["discussion"] == 367

    # Backward-compat: cost_usd is preserved.
    expected_cost = (3500 / 1000 * 0.003) + (1200 / 1000 * 0.015)
    assert entry["cost_usd"] == pytest.approx(expected_cost, abs=1e-6)


def test_by_discussion_cli_subcommand(tmp_path, monkeypatch):
    """by-discussion subcommand returns sorted JSON with all required fields."""
    import io
    from unittest.mock import patch as mock_patch

    records_367 = [
        {
            "agent_id": "executor-367-cli",
            "agent": "executor",
            "input": 1000,
            "output": 500,
            "model": "default",
            "finished": "2026-05-09T10:00:00+00:00",
            "discussion": 367,
        }
    ]
    records_354 = [
        {
            "agent_id": "executor-354-cli",
            "agent": "executor",
            "input": 5000,
            "output": 2000,
            "model": "default",
            "finished": "2026-05-09T09:00:00+00:00",
            "discussion": 354,
        }
    ]
    bb = _make_bb_with_records(records_367 + records_354)

    from backend import cost_tracker as ct_mod

    with mock_patch.object(ct_mod, "Blackboard", return_value=bb):
        captured = io.StringIO()
        with mock_patch("sys.stdout", captured):
            rc = ct_mod.main(["by-discussion"])
        assert rc == 0

    output = captured.getvalue()
    entries = json.loads(output)
    assert isinstance(entries, list)
    assert len(entries) == 2

    # Must be sorted by total_cost_usd descending.
    costs = [e["total_cost_usd"] for e in entries]
    assert costs == sorted(costs, reverse=True)

    # Each entry must have all required fields.
    for entry in entries:
        for field in ("discussion", "cost_usd", "total_cost_usd", "agent_count",
                      "total_input_tokens", "total_output_tokens", "agents"):
            assert field in entry, f"Missing field {field!r} in entry: {entry}"


def test_by_discussion_cli_top_filter(monkeypatch):
    """--top N truncates output to N entries."""
    import io
    from unittest.mock import patch as mock_patch

    records = [
        {
            "agent_id": f"executor-{disc}-cli",
            "agent": "executor",
            "input": disc * 100,
            "output": disc * 50,
            "model": "default",
            "finished": "2026-05-09T10:00:00+00:00",
            "discussion": disc,
        }
        for disc in [100, 200, 300, 400, 500]
    ]
    bb = _make_bb_with_records(records)

    from backend import cost_tracker as ct_mod

    with mock_patch.object(ct_mod, "Blackboard", return_value=bb):
        captured = io.StringIO()
        with mock_patch("sys.stdout", captured):
            rc = ct_mod.main(["by-discussion", "--top", "3"])
        assert rc == 0

    entries = json.loads(captured.getvalue())
    assert len(entries) == 3


def test_by_discussion_cli_single_discussion(monkeypatch):
    """--discussion N returns one entry or null."""
    import io
    from unittest.mock import patch as mock_patch

    records = [
        {
            "agent_id": "executor-367-single",
            "agent": "executor",
            "input": 1000,
            "output": 500,
            "model": "default",
            "finished": "2026-05-09T10:00:00+00:00",
            "discussion": 367,
        }
    ]
    bb = _make_bb_with_records(records)

    from backend import cost_tracker as ct_mod

    # Existing discussion.
    with mock_patch.object(ct_mod, "Blackboard", return_value=bb):
        captured = io.StringIO()
        with mock_patch("sys.stdout", captured):
            rc = ct_mod.main(["by-discussion", "--discussion", "367"])
        assert rc == 0
    entry = json.loads(captured.getvalue())
    assert entry is not None
    assert entry["discussion"] == 367

    # Non-existent discussion → JSON null.
    with mock_patch.object(ct_mod, "Blackboard", return_value=bb):
        captured2 = io.StringIO()
        with mock_patch("sys.stdout", captured2):
            rc2 = ct_mod.main(["by-discussion", "--discussion", "9999"])
        assert rc2 == 0
    assert json.loads(captured2.getvalue()) is None
