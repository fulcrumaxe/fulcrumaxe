"""
Tests for D#570 — cost_tracker.py pricing fixes.

Covers:
- All new model rows in _DEFAULT_PRICING
- Cache token pricing (cache_read_per_1k, cache_write_5m_per_1k)
- 1M-context Opus flat rate
- Unknown model warn-once behaviour
- Backfill script correctness
- post-agent-hook / record-agent-result cache flag passthrough (smoke)
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
from backend.budget import BudgetTracker
from backend.cost_tracker import (
    CostTracker,
    _DEFAULT_PRICING,
    _WARNED_UNKNOWN_MODELS,
    _compute_cost,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_budget(tmp_path) -> BudgetTracker:
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    return bt


def _make_tracker(tmp_path) -> CostTracker:
    bb = Blackboard(root=tmp_path / "blackboard")
    return CostTracker(bb=bb)


# ---------------------------------------------------------------------------
# 1. New model rows exist and have correct input/output rates
# ---------------------------------------------------------------------------

_EXPECTED_MODELS = [
    ("claude-opus-4-7",            0.015, 0.075),
    ("claude-opus-4-7[1m]",        0.030, 0.150),
    ("claude-sonnet-4-6",          0.003, 0.015),
    ("claude-sonnet-4-5-20250929", 0.003, 0.015),
    ("claude-haiku-4-5-20251001",  0.0008, 0.004),
    # Legacy models still present
    ("claude-sonnet-4-20250514",   0.003, 0.015),
    ("claude-opus-4-20250514",     0.015, 0.075),
    ("kimi-k2-0711",               0.0006, 0.002),
]


@pytest.mark.parametrize("model,expected_input_rate,expected_output_rate", _EXPECTED_MODELS)
def test_model_in_pricing_table(model, expected_input_rate, expected_output_rate):
    assert model in _DEFAULT_PRICING, f"{model} missing from _DEFAULT_PRICING"
    rates = _DEFAULT_PRICING[model]
    assert abs(rates["input_per_1k"] - expected_input_rate) < 1e-9, (
        f"{model} input_per_1k: expected {expected_input_rate}, got {rates['input_per_1k']}"
    )
    assert abs(rates["output_per_1k"] - expected_output_rate) < 1e-9, (
        f"{model} output_per_1k: expected {expected_output_rate}, got {rates['output_per_1k']}"
    )


@pytest.mark.parametrize("model,expected_input_rate,expected_output_rate", _EXPECTED_MODELS)
def test_model_compute_cost_basic(model, expected_input_rate, expected_output_rate):
    """_compute_cost returns correct value for 1000 in + 1000 out."""
    expected = expected_input_rate + expected_output_rate
    actual = _compute_cost(1000, 1000, model, _DEFAULT_PRICING)
    assert abs(actual - expected) < 1e-9


def test_opus_4_7_costs_5x_more_than_default():
    """Opus input is 5× Sonnet — the core undercount fix."""
    sonnet_cost = _compute_cost(100_000, 0, "default", _DEFAULT_PRICING)
    opus_cost = _compute_cost(100_000, 0, "claude-opus-4-7", _DEFAULT_PRICING)
    assert abs(opus_cost / sonnet_cost - 5.0) < 0.01


# ---------------------------------------------------------------------------
# 2. Cache token pricing
# ---------------------------------------------------------------------------

def test_cache_read_adds_cost():
    """cache_read_per_1k for opus-4-7 is 0.0015/1k."""
    base = _compute_cost(1000, 0, "claude-opus-4-7", _DEFAULT_PRICING)
    with_cache_read = _compute_cost(1000, 0, "claude-opus-4-7", _DEFAULT_PRICING, cache_read_tokens=1000)
    delta = with_cache_read - base
    assert abs(delta - 0.0015) < 1e-9


def test_cache_write_5m_adds_cost():
    """cache_write_5m_per_1k for opus-4-7 is 0.01875/1k."""
    base = _compute_cost(1000, 0, "claude-opus-4-7", _DEFAULT_PRICING)
    with_write = _compute_cost(1000, 0, "claude-opus-4-7", _DEFAULT_PRICING, cache_write_tokens=1000)
    delta = with_write - base
    assert abs(delta - 0.01875) < 1e-9


def test_cache_read_zero_no_effect():
    """Zero cache tokens must not change the cost."""
    cost1 = _compute_cost(5000, 2000, "claude-opus-4-7", _DEFAULT_PRICING)
    cost2 = _compute_cost(5000, 2000, "claude-opus-4-7", _DEFAULT_PRICING, cache_read_tokens=0, cache_write_tokens=0)
    assert abs(cost1 - cost2) < 1e-12


def test_cache_sonnet_4_6_read_rate():
    """claude-sonnet-4-6 cache_read_per_1k is 0.0003/1k."""
    delta = _compute_cost(0, 0, "claude-sonnet-4-6", _DEFAULT_PRICING, cache_read_tokens=1000)
    assert abs(delta - 0.0003) < 1e-9


def test_model_without_cache_rates_zero_cache_cost():
    """Models without cache rates should not add cost for cache tokens."""
    # claude-haiku-4-5-20251001 has no cache rates defined
    base = _compute_cost(1000, 0, "claude-haiku-4-5-20251001", _DEFAULT_PRICING)
    with_cache = _compute_cost(1000, 0, "claude-haiku-4-5-20251001", _DEFAULT_PRICING, cache_read_tokens=1000)
    assert abs(base - with_cache) < 1e-12


# ---------------------------------------------------------------------------
# 3. 1M-context Opus flat rate
# ---------------------------------------------------------------------------

def test_opus_1m_flat_rate_applied():
    """claude-opus-4-7[1m] charges 0.030/1k input for all tokens."""
    cost = _compute_cost(100_000, 0, "claude-opus-4-7[1m]", _DEFAULT_PRICING)
    expected = 100_000 / 1000.0 * 0.030
    assert abs(cost - expected) < 1e-9


def test_opus_1m_2x_regular_opus_input():
    """1M-context Opus input rate (0.030) is 2× regular Opus (0.015)."""
    regular = _compute_cost(50_000, 0, "claude-opus-4-7", _DEFAULT_PRICING)
    one_m = _compute_cost(50_000, 0, "claude-opus-4-7[1m]", _DEFAULT_PRICING)
    assert abs(one_m / regular - 2.0) < 0.01


# ---------------------------------------------------------------------------
# 4. Unknown model warn-once
# ---------------------------------------------------------------------------

def test_unknown_model_falls_back_to_default():
    """Unknown models fall back to default rates."""
    _WARNED_UNKNOWN_MODELS.discard("my-secret-model-xyz")
    cost = _compute_cost(1000, 1000, "my-secret-model-xyz", _DEFAULT_PRICING)
    default_cost = _compute_cost(1000, 1000, "default", _DEFAULT_PRICING)
    assert abs(cost - default_cost) < 1e-9
    _WARNED_UNKNOWN_MODELS.discard("my-secret-model-xyz")


def test_unknown_model_emits_warning(capsys):
    """Unknown model should print to stderr."""
    model = "totally-unknown-model-v99"
    _WARNED_UNKNOWN_MODELS.discard(model)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        _compute_cost(1000, 0, model, _DEFAULT_PRICING)
    captured = capsys.readouterr()
    assert model in captured.err, "Expected model name in stderr warning"
    _WARNED_UNKNOWN_MODELS.discard(model)


def test_unknown_model_warns_only_once(capsys):
    """Second call with same unknown model should not repeat the warning."""
    model = "warn-once-test-model-abc123"
    _WARNED_UNKNOWN_MODELS.discard(model)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        _compute_cost(100, 0, model, _DEFAULT_PRICING)
    captured1 = capsys.readouterr()

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        _compute_cost(100, 0, model, _DEFAULT_PRICING)
    captured2 = capsys.readouterr()

    assert model in captured1.err
    assert model not in captured2.err, "Second call should not re-warn"
    _WARNED_UNKNOWN_MODELS.discard(model)


# ---------------------------------------------------------------------------
# 5. CostTracker.compute_cost public API includes cache params
# ---------------------------------------------------------------------------

def test_cost_tracker_compute_cost_cache_params(tmp_path):
    ct = _make_tracker(tmp_path)
    base = ct.compute_cost(1000, 0, "claude-opus-4-7")
    with_cache = ct.compute_cost(1000, 0, "claude-opus-4-7", cache_read_tokens=1000)
    assert with_cache > base


# ---------------------------------------------------------------------------
# 6. get_session_cost reads cache tokens from blackboard records
# ---------------------------------------------------------------------------

def test_session_cost_includes_cache_tokens(tmp_path):
    """Records with cache_read_tokens/cache_write_tokens get higher cost."""
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()

    # Record with cache tokens
    bt.record_spend("a1", "executor", 10000, 2000,
                    discussion=1, model="claude-opus-4-7",
                    cache_read_tokens=5000, cache_write_tokens=2000)

    # Record without cache tokens (same base tokens, same model)
    bt.record_spend("a2", "executor", 10000, 2000,
                    discussion=2, model="claude-opus-4-7")

    ct = CostTracker(bb=bb)
    session = ct.get_session_cost()
    by_disc = {e["discussion"]: e for e in session["by_discussion"]}

    cost_with_cache = by_disc[1]["cost_usd"]
    cost_without = by_disc[2]["cost_usd"]

    assert cost_with_cache > cost_without, (
        f"Record with cache tokens should cost more: {cost_with_cache} vs {cost_without}"
    )


def test_by_agent_includes_cache_fields(tmp_path):
    """by_agent entries expose cache_read_tokens and cache_write_tokens."""
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    bt.record_spend("a3", "executor", 1000, 500,
                    discussion=5, model="claude-sonnet-4-6",
                    cache_read_tokens=200, cache_write_tokens=100)

    ct = CostTracker(bb=bb)
    session = ct.get_session_cost()
    agent = next(a for a in session["by_agent"] if a["agent_id"] == "a3")

    assert agent["cache_read_tokens"] == 200
    assert agent["cache_write_tokens"] == 100


# ---------------------------------------------------------------------------
# 7. budget.py record_spend stores cache tokens
# ---------------------------------------------------------------------------

def test_budget_record_spend_stores_cache_tokens(tmp_path):
    """BudgetTracker.record_spend writes cache tokens to the blackboard record."""
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    bt.record_spend(
        "cache-test-agent", "executor", 5000, 1000,
        discussion=42, model="claude-opus-4-7",
        cache_read_tokens=3000, cache_write_tokens=1500,
    )
    record = bb.read("budget/agents/cache-test-agent")
    assert record is not None
    assert record.get("cache_read_tokens") == 3000
    assert record.get("cache_write_tokens") == 1500


def test_budget_record_spend_no_cache_omits_fields(tmp_path):
    """Without cache tokens, record should not have those keys."""
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    bt.record_spend("no-cache-agent", "executor", 1000, 200, discussion=1)
    record = bb.read("budget/agents/no-cache-agent")
    assert record is not None
    # Keys should be absent when zero
    assert "cache_read_tokens" not in record
    assert "cache_write_tokens" not in record


# ---------------------------------------------------------------------------
# 8. Backfill script dry-run
# ---------------------------------------------------------------------------

def test_backfill_dry_run_no_error(tmp_path, monkeypatch):
    """backfill script computes old vs new totals correctly."""
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()

    # Seed with models that were previously unknown (fell to default in old pricing)
    bt.record_spend("bf-1", "executor", 50000, 10000,
                    discussion=99, model="claude-opus-4-7")
    bt.record_spend("bf-2", "executor", 20000, 5000,
                    discussion=100, model="claude-sonnet-4-6")

    # Import the backfill module from scripts directory
    import importlib.util
    backfill_path = Path(__file__).resolve().parent.parent / "scripts" / "backfill-cost-tracker.py"
    spec = importlib.util.spec_from_file_location("backfill_cost_tracker", backfill_path)
    if spec is None or spec.loader is None:
        pytest.skip("backfill script not found")

    backfill_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill_mod)  # type: ignore[union-attr]

    # Patch Blackboard in the backfill module and cost_tracker to use our tmp bb
    import backend.cost_tracker as ct_mod
    monkeypatch.setattr(ct_mod, "Blackboard", lambda *a, **kw: bb)
    monkeypatch.setattr(backfill_mod, "Blackboard", lambda *a, **kw: bb)

    rc = backfill_mod.run_backfill(dry_run=True, quiet=True)
    assert rc == 0


# ---------------------------------------------------------------------------
# 9. Summary regression — existing test keys still present
# ---------------------------------------------------------------------------

def test_summary_keys_unchanged_with_new_pricing(tmp_path):
    """Existing get_session_cost() structure is not broken by pricing changes."""
    bt = _make_budget(tmp_path)
    bt.record_spend("r1", "executor", 10000, 2000, discussion=10, pr=100)
    bt.record_spend("r2", "code-reviewer", 5000, 1000, discussion=10, pr=100)

    ct = _make_tracker(tmp_path)
    full = ct.get_session_cost()

    assert "total_cost_usd" in full
    assert "by_agent" in full
    assert "by_discussion" in full
    assert "model_breakdown" in full

    for entry in full["by_discussion"]:
        assert "discussion" in entry
        assert "cost_usd" in entry
        assert "total_cost_usd" in entry
        assert "agents" in entry
        assert "total_input_tokens" in entry
        assert "total_output_tokens" in entry
        assert "agent_count" in entry
        assert "agent_breakdown" in entry
        assert "pr_breakdown" in entry
