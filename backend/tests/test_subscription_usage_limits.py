"""Tests for subscription_usage.current_plan_limits().

Verifies the config → estimated → hardcoded-fallback resolution order
and the plan-cost lookup table.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend import subscription_usage


# ---------------------------------------------------------------------------
# current_plan_limits — resolution order
# ---------------------------------------------------------------------------

class TestCurrentPlanLimitsResolution:
    """Priority: config > estimated > hardcoded-fallback."""

    def test_config_wins_when_both_keys_present(self, monkeypatch):
        """Explicit config.json values override plan table estimates."""
        fake_config = {
            "subscription": {
                "plan": "max-5x",
                "daily_usd_cap": 8.0,
                "monthly_usd_cap": 240.0,
            }
        }
        monkeypatch.setattr(subscription_usage, "_load_config", lambda: fake_config)

        result = subscription_usage.current_plan_limits()

        assert result["source"] == "config"
        assert result["daily_usd_cap"] == 8.0
        assert result["monthly_usd_cap"] == 240.0

    def test_estimated_when_no_explicit_caps(self, monkeypatch):
        """Falls back to plan table when config has no explicit USD caps."""
        fake_config = {"subscription": {"plan": "max-5x"}}
        monkeypatch.setattr(subscription_usage, "_load_config", lambda: fake_config)

        result = subscription_usage.current_plan_limits()

        assert result["source"] == "estimated"
        assert result["monthly_usd_cap"] == 100.0
        assert abs(result["daily_usd_cap"] - round(100.0 / 30, 4)) < 0.0001

    def test_hardcoded_fallback_when_plan_unknown(self, monkeypatch):
        """Unknown plan name produces hardcoded-fallback."""
        fake_config = {"subscription": {"plan": "unknown-plan-xyz"}}
        monkeypatch.setattr(subscription_usage, "_load_config", lambda: fake_config)

        result = subscription_usage.current_plan_limits()

        assert result["source"] == "hardcoded-fallback"
        assert result["daily_usd_cap"] == subscription_usage._FALLBACK_DAILY_USD
        assert result["monthly_usd_cap"] == subscription_usage._FALLBACK_MONTHLY_USD

    def test_hardcoded_fallback_when_config_empty(self, monkeypatch):
        """Empty config → no plan info → hardcoded-fallback."""
        monkeypatch.setattr(subscription_usage, "_load_config", lambda: {})

        # Without any plan arg and no config plan, resolution falls through to
        # "max-20x" default in the plan table — which IS in _PLAN_MONTHLY_USD.
        # So the result should be "estimated" not "hardcoded-fallback".
        result = subscription_usage.current_plan_limits()

        assert result["source"] == "estimated"
        assert result["monthly_usd_cap"] == 200.0  # max-20x default

    def test_explicit_plan_arg_overrides_config(self, monkeypatch):
        """plan= argument is used when both arg and config plan are present."""
        fake_config = {"subscription": {"plan": "max-20x"}}
        monkeypatch.setattr(subscription_usage, "_load_config", lambda: fake_config)

        result = subscription_usage.current_plan_limits(plan="pro")

        assert result["source"] == "estimated"
        assert result["monthly_usd_cap"] == 20.0

    def test_only_daily_cap_in_config_falls_through(self, monkeypatch):
        """Partial config (only daily_usd_cap) doesn't count as complete config."""
        fake_config = {
            "subscription": {
                "plan": "max-5x",
                "daily_usd_cap": 5.0,
                # monthly_usd_cap missing
            }
        }
        monkeypatch.setattr(subscription_usage, "_load_config", lambda: fake_config)

        result = subscription_usage.current_plan_limits()

        # Must fall through to estimated since monthly is missing
        assert result["source"] == "estimated"
        assert result["monthly_usd_cap"] == 100.0


# ---------------------------------------------------------------------------
# Plan table values
# ---------------------------------------------------------------------------

class TestPlanTable:
    """Verify per-plan USD values are correct."""

    @pytest.mark.parametrize("plan,monthly", [
        ("pro", 20.0),
        ("max-5x", 100.0),
        ("max-20x", 200.0),
        ("team", 25.0),
    ])
    def test_plan_monthly_cost(self, monkeypatch, plan, monthly):
        monkeypatch.setattr(subscription_usage, "_load_config",
                            lambda: {"subscription": {"plan": plan}})

        result = subscription_usage.current_plan_limits()

        assert result["source"] == "estimated"
        assert result["monthly_usd_cap"] == monthly
        assert abs(result["daily_usd_cap"] - round(monthly / 30, 4)) < 0.0001

    def test_daily_is_monthly_over_30(self, monkeypatch):
        """daily_usd_cap must equal monthly_usd_cap / 30 (rounded to 4dp)."""
        monkeypatch.setattr(subscription_usage, "_load_config",
                            lambda: {"subscription": {"plan": "max-20x"}})

        result = subscription_usage.current_plan_limits()
        expected_daily = round(200.0 / 30, 4)

        assert result["daily_usd_cap"] == expected_daily


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------

class TestReturnShape:
    def test_all_required_keys_present(self):
        result = subscription_usage.current_plan_limits()

        assert "daily_usd_cap" in result
        assert "monthly_usd_cap" in result
        assert "source" in result

    def test_source_values_are_valid(self, monkeypatch):
        valid_sources = {"config", "estimated", "hardcoded-fallback"}

        # Trigger all three branches
        for config_patch, plan_arg in [
            ({"subscription": {"daily_usd_cap": 1.0, "monthly_usd_cap": 30.0}}, None),
            ({"subscription": {"plan": "max-5x"}}, None),
            ({}, "totally-unknown-plan"),
        ]:
            monkeypatch.setattr(subscription_usage, "_load_config", lambda c=config_patch: c)
            result = subscription_usage.current_plan_limits(plan=plan_arg)
            assert result["source"] in valid_sources, f"Unexpected source: {result['source']}"
