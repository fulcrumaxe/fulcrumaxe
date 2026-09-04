"""Tests for the /budget/status endpoint in backend/api.py.

Mocks CostTracker.aggregate_daily_monthly_spend and
subscription_usage.current_plan_limits to verify the endpoint shape.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_budget_status(project_id: str = "") -> dict:
    """Invoke _project_sub_endpoint("budget/status", project_id) from api.py."""
    # Clear module-level budget cache between test calls
    from backend import api as api_mod
    fn = api_mod._project_sub_endpoint
    cache = fn.__dict__.get("_budget_cache")
    if cache is not None:
        cache.clear()

    return fn("budget/status", project_id=project_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_budget_status_returns_aggregated_spend():
    """dailySpend and monthlySpend must come from CostTracker, not token arithmetic."""
    mock_spend = {"daily_usd": 1.2345, "monthly_usd": 42.0}
    mock_limits = {
        "daily_usd_cap": 6.6667,
        "monthly_usd_cap": 200.0,
        "source": "estimated",
    }

    with (
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              return_value=mock_spend),
        patch("backend.subscription_usage.current_plan_limits",
              return_value=mock_limits),
    ):
        result = _call_budget_status()

    assert result["dailySpend"] == round(1.2345, 4)
    assert result["monthlySpend"] == round(42.0, 4)


def test_budget_status_uses_subscription_limits():
    """dailyLimit and monthlyLimit must come from current_plan_limits()."""
    mock_spend = {"daily_usd": 0.5, "monthly_usd": 3.0}
    mock_limits = {
        "daily_usd_cap": 3.3333,
        "monthly_usd_cap": 100.0,
        "source": "estimated",
    }

    with (
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              return_value=mock_spend),
        patch("backend.subscription_usage.current_plan_limits",
              return_value=mock_limits),
    ):
        result = _call_budget_status()

    assert result["dailyLimit"] == round(3.3333, 4)
    assert result["monthlyLimit"] == round(100.0, 4)


def test_budget_status_includes_limit_source():
    """limitSource must propagate from current_plan_limits()."""
    mock_spend = {"daily_usd": 0.0, "monthly_usd": 0.0}
    mock_limits = {
        "daily_usd_cap": 15.0,
        "monthly_usd_cap": 450.0,
        "source": "hardcoded-fallback",
    }

    with (
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              return_value=mock_spend),
        patch("backend.subscription_usage.current_plan_limits",
              return_value=mock_limits),
    ):
        result = _call_budget_status()

    assert result["limitSource"] == "hardcoded-fallback"


def test_budget_status_config_source():
    """limitSource = 'config' when values come from explicit config entries."""
    mock_spend = {"daily_usd": 0.1, "monthly_usd": 2.5}
    mock_limits = {
        "daily_usd_cap": 10.0,
        "monthly_usd_cap": 300.0,
        "source": "config",
    }

    with (
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              return_value=mock_spend),
        patch("backend.subscription_usage.current_plan_limits",
              return_value=mock_limits),
    ):
        result = _call_budget_status()

    assert result["limitSource"] == "config"
    assert result["dailyLimit"] == 10.0
    assert result["monthlyLimit"] == 300.0


def test_budget_status_schema():
    """Response must contain all required fields with correct types."""
    mock_spend = {"daily_usd": 0.0, "monthly_usd": 0.0}
    mock_limits = {
        "daily_usd_cap": 15.0,
        "monthly_usd_cap": 450.0,
        "source": "hardcoded-fallback",
    }

    with (
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              return_value=mock_spend),
        patch("backend.subscription_usage.current_plan_limits",
              return_value=mock_limits),
    ):
        result = _call_budget_status()

    required_fields = {
        "dailySpend": float,
        "monthlySpend": float,
        "dailyLimit": float,
        "monthlyLimit": float,
        "currency": str,
        "alertThreshold": float,
        "limitSource": str,
    }
    for field, ftype in required_fields.items():
        assert field in result, f"missing field: {field}"
        assert isinstance(result[field], ftype), (
            f"{field} expected {ftype.__name__}, got {type(result[field]).__name__}"
        )

    assert result["currency"] == "USD"
    assert result["alertThreshold"] == 0.8


def test_budget_status_fallback_on_exception():
    """On CostTracker error the endpoint must still return a valid shape."""
    with (
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              side_effect=RuntimeError("blackboard unavailable")),
        patch("backend.subscription_usage.current_plan_limits",
              side_effect=RuntimeError("config missing")),
    ):
        result = _call_budget_status()

    # Should get hardcoded-fallback values
    assert result["dailySpend"] == 0.0
    assert result["monthlySpend"] == 0.0
    assert result["dailyLimit"] == 15.0
    assert result["monthlyLimit"] == 450.0
    assert result["limitSource"] == "hardcoded-fallback"

def _seed_budget_cache(spend: dict | None = None, project_id: str = "") -> None:
    """Prime the 60-second per-project cache so a reset test can verify it gets cleared."""
    from backend import api as api_mod
    import time as _time
    fn = api_mod._project_sub_endpoint
    all_caches = fn.__dict__.setdefault("_budget_cache", {})
    proj_cache = all_caches.setdefault(project_id, {})
    proj_cache["bucket"] = int(_time.time() // 60)
    proj_cache["result"] = {
        "dailySpend": (spend or {}).get("daily_usd", 9.99),
        "monthlySpend": (spend or {}).get("monthly_usd", 99.99),
        "dailyLimit": 15.0,
        "monthlyLimit": 450.0,
        "currency": "USD",
        "alertThreshold": 0.8,
        "limitSource": "test-seed",
    }


# ---------------------------------------------------------------------------
# Reset + cache-bust propagation (D#854 sub-2)
# ---------------------------------------------------------------------------

def test_bust_budget_cache_clears_stale_values():
    """_bust_budget_cache() must clear all per-project cache entries."""
    # Prime the cache with non-zero values for both AF (global) and projectb.
    _seed_budget_cache({"daily_usd": 9.99, "monthly_usd": 99.99}, project_id="")
    _seed_budget_cache({"daily_usd": 1.11, "monthly_usd": 11.11}, project_id="projectb")

    from backend import api as api_mod
    fn = api_mod._project_sub_endpoint
    all_caches = fn.__dict__.get("_budget_cache", {})
    assert all_caches.get("", {}).get("result", {}).get("dailySpend") == 9.99
    assert all_caches.get("projectb", {}).get("result", {}).get("dailySpend") == 1.11

    api_mod._bust_budget_cache()

    # After bust, the outer cache dict must be empty — all per-project entries cleared.
    cache = fn.__dict__.get("_budget_cache", {})
    assert len(cache) == 0


def test_budget_status_after_reset_reflects_zero():
    """After reset + cache bust, GET /budget/status must return zero spend — not pre-reset value."""
    # Prime the cache with non-zero spend to simulate the stale state.
    _seed_budget_cache({"daily_usd": 5.55, "monthly_usd": 55.55})

    # Simulate reset: bust the cache, then call status with zero spend.
    from backend import api as api_mod
    api_mod._bust_budget_cache()

    mock_spend = {"daily_usd": 0.0, "monthly_usd": 0.0}
    mock_limits = {"daily_usd_cap": 15.0, "monthly_usd_cap": 450.0, "source": "hardcoded-fallback"}

    with (
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              return_value=mock_spend),
        patch("backend.subscription_usage.current_plan_limits",
              return_value=mock_limits),
    ):
        result = api_mod._project_sub_endpoint("budget/status", project_id="")

    assert result["dailySpend"] == 0.0, (
        f"Expected 0.0 after reset, got {result['dailySpend']} — cache was not busted"
    )
    assert result["monthlySpend"] == 0.0, (
        f"Expected 0.0 after reset, got {result['monthlySpend']} — cache was not busted"
    )


# ---------------------------------------------------------------------------
# Per-project scoping (D#1204 — projectb shows spent=0 bug)
# ---------------------------------------------------------------------------

def test_budget_status_per_project_reads_project_blackboard():
    """budget/status for a non-AF project must read that project's own Blackboard."""
    from unittest.mock import MagicMock, patch, call
    from backend import api as api_mod

    # Clear cache before test
    fn = api_mod._project_sub_endpoint
    cache = fn.__dict__.get("_budget_cache")
    if cache is not None:
        cache.clear()

    projectb_spend = {"daily_usd": 3.75, "monthly_usd": 75.0}
    mock_limits = {"daily_usd_cap": 15.0, "monthly_usd_cap": 450.0, "source": "estimated"}

    mock_paths = MagicMock()
    mock_paths.state_dir = MagicMock()

    mock_bb = MagicMock()
    mock_tracker = MagicMock()
    mock_tracker.aggregate_daily_monthly_spend.return_value = projectb_spend

    with (
        patch("backend.state_paths.for_project", return_value=mock_paths) as mock_fp,
        patch("backend.blackboard.Blackboard", return_value=mock_bb) as mock_bb_cls,
        patch("backend.cost_tracker.CostTracker", return_value=mock_tracker) as mock_ct,
        patch("backend.subscription_usage.current_plan_limits", return_value=mock_limits),
    ):
        result = fn("budget/status", project_id="projectb")

    # for_project was called with "projectb"
    mock_fp.assert_called_once_with("projectb")
    # Blackboard was constructed with the projectb state dir / "blackboard"
    mock_bb_cls.assert_called_once_with(root=mock_paths.state_dir / "blackboard")
    # CostTracker received the projectb blackboard
    mock_ct.assert_called_once_with(bb=mock_bb)
    # Response reflects projectb spend
    assert result["dailySpend"] == round(3.75, 4)
    assert result["monthlySpend"] == round(75.0, 4)


def test_budget_status_global_does_not_use_for_project():
    """budget/status with empty project_id uses global CostTracker, not for_project."""
    from backend import api as api_mod

    fn = api_mod._project_sub_endpoint
    cache = fn.__dict__.get("_budget_cache")
    if cache is not None:
        cache.clear()

    af_spend = {"daily_usd": 12.0, "monthly_usd": 200.0}
    mock_limits = {"daily_usd_cap": 15.0, "monthly_usd_cap": 450.0, "source": "estimated"}

    with (
        patch("backend.state_paths.for_project") as mock_fp,
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              return_value=af_spend),
        patch("backend.subscription_usage.current_plan_limits", return_value=mock_limits),
    ):
        result = fn("budget/status", project_id="")

    # for_project must NOT be called for the global/AF path
    mock_fp.assert_not_called()
    assert result["dailySpend"] == round(12.0, 4)


def test_budget_status_cache_does_not_cross_contaminate():
    """A cached projectb result must not be served when querying AF, and vice versa."""
    from backend import api as api_mod

    fn = api_mod._project_sub_endpoint
    cache = fn.__dict__.get("_budget_cache")
    if cache is not None:
        cache.clear()

    projectb_spend = {"daily_usd": 5.5, "monthly_usd": 55.5}
    af_spend = {"daily_usd": 20.0, "monthly_usd": 400.0}
    mock_limits = {"daily_usd_cap": 15.0, "monthly_usd_cap": 450.0, "source": "estimated"}

    mock_paths = MagicMock()
    mock_paths.state_dir = MagicMock()
    mock_bb = MagicMock()
    mock_tracker = MagicMock()
    mock_tracker.aggregate_daily_monthly_spend.return_value = projectb_spend

    # First: query projectb — seeds the projectb cache entry
    with (
        patch("backend.state_paths.for_project", return_value=mock_paths),
        patch("backend.blackboard.Blackboard", return_value=mock_bb),
        patch("backend.cost_tracker.CostTracker", return_value=mock_tracker),
        patch("backend.subscription_usage.current_plan_limits", return_value=mock_limits),
    ):
        projectb_result = fn("budget/status", project_id="projectb")

    # Second: query AF — must not return the projectb cached value
    with (
        patch("backend.cost_tracker.CostTracker.aggregate_daily_monthly_spend",
              return_value=af_spend),
        patch("backend.subscription_usage.current_plan_limits", return_value=mock_limits),
    ):
        af_result = fn("budget/status", project_id="")

    assert projectb_result["dailySpend"] == round(5.5, 4), "projectb result incorrect"
    assert af_result["dailySpend"] == round(20.0, 4), "AF result contaminated by projectb cache"
    assert projectb_result["dailySpend"] != af_result["dailySpend"], "cache cross-contamination detected"


def test_budget_status_missing_project_state_returns_zero_spend():
    """If a project's state dir or blackboard cannot be read, return zeroed spend shape."""
    from backend import api as api_mod

    fn = api_mod._project_sub_endpoint
    cache = fn.__dict__.get("_budget_cache")
    if cache is not None:
        cache.clear()

    mock_paths = MagicMock()
    mock_paths.state_dir = MagicMock()
    mock_limits = {"daily_usd_cap": 15.0, "monthly_usd_cap": 450.0, "source": "hardcoded-fallback"}

    with (
        patch("backend.state_paths.for_project", return_value=mock_paths),
        patch("backend.blackboard.Blackboard", side_effect=OSError("no state dir")),
        patch("backend.subscription_usage.current_plan_limits", return_value=mock_limits),
    ):
        result = fn("budget/status", project_id="newproject")

    # Fallback: zeroed spend, full valid schema
    assert result["dailySpend"] == 0.0
    assert result["monthlySpend"] == 0.0
    assert result["currency"] == "USD"
    assert result["alertThreshold"] == 0.8
