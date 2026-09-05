"""
Tests for backend/agent_retry.py.

Covers: retry decisions, delay math, blackboard tracking, policy loading, clear.
Uses an isolated Blackboard (tmp_path) patched into the module-level _bb.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
import backend.agent_retry as ar
from backend.agent_retry import (
    RetryPolicy,
    RetryDecision,
    should_retry,
    load_retry_policy,
    record_attempt,
    clear_retries,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def isolated_bb(tmp_path):
    """Return a fresh Blackboard and patch it into the agent_retry module."""
    bb = Blackboard(root=tmp_path / "blackboard")
    with patch.object(ar, "_bb", bb):
        yield bb


@pytest.fixture()
def default_policy():
    return RetryPolicy()


# ------------------------------------------------------------------
# should_retry — retry decisions
# ------------------------------------------------------------------


def test_should_retry_first_attempt_returns_true(default_policy):
    decision = should_retry(14, "executor", attempt=0, policy=default_policy)
    assert decision.retry is True


def test_should_retry_second_attempt_returns_true(default_policy):
    # max_retries=2 means attempts 0 and 1 are ok
    decision = should_retry(14, "executor", attempt=1, policy=default_policy)
    assert decision.retry is True


def test_should_retry_at_max_returns_false(default_policy):
    decision = should_retry(14, "executor", attempt=2, policy=default_policy)
    assert decision.retry is False


def test_should_retry_beyond_max_returns_false(default_policy):
    decision = should_retry(14, "executor", attempt=5, policy=default_policy)
    assert decision.retry is False


def test_should_retry_returns_retry_decision_type(default_policy):
    decision = should_retry(14, "executor", attempt=0, policy=default_policy)
    assert isinstance(decision, RetryDecision)


# ------------------------------------------------------------------
# should_retry — delay math
# ------------------------------------------------------------------


def test_delay_attempt_0_equals_base_delay(default_policy):
    # attempt=0: base * factor^0 = 30 * 1 = 30
    decision = should_retry(14, "executor", attempt=0, policy=default_policy)
    assert decision.delay_seconds == pytest.approx(30.0)


def test_delay_attempt_1_doubles(default_policy):
    # attempt=1: base * factor^1 = 30 * 2 = 60
    decision = should_retry(14, "executor", attempt=1, policy=default_policy)
    assert decision.delay_seconds == pytest.approx(60.0)


def test_delay_capped_at_max(default_policy):
    # Large attempt — delay would exceed max_delay
    policy = RetryPolicy(max_retries=20, base_delay_seconds=30.0, max_delay_seconds=90.0, backoff_factor=2.0)
    decision = should_retry(14, "executor", attempt=10, policy=policy)
    assert decision.delay_seconds == pytest.approx(90.0)


def test_delay_exactly_at_cap_boundary():
    # attempt=1: 30 * 2 = 60, cap at 60 → still 60
    policy = RetryPolicy(max_retries=5, base_delay_seconds=30.0, max_delay_seconds=60.0, backoff_factor=2.0)
    decision = should_retry(14, "executor", attempt=1, policy=policy)
    assert decision.delay_seconds == pytest.approx(60.0)


def test_delay_is_zero_when_no_retry(default_policy):
    decision = should_retry(14, "executor", attempt=2, policy=default_policy)
    assert decision.delay_seconds == pytest.approx(0.0)


def test_delay_with_custom_backoff_factor():
    policy = RetryPolicy(max_retries=5, base_delay_seconds=10.0, max_delay_seconds=1000.0, backoff_factor=3.0)
    # attempt=2: 10 * 3^2 = 90
    decision = should_retry(14, "executor", attempt=2, policy=policy)
    assert decision.delay_seconds == pytest.approx(90.0)


# ------------------------------------------------------------------
# load_retry_policy — config loading
# ------------------------------------------------------------------


def test_load_retry_policy_returns_defaults_when_no_config(tmp_path):
    non_existent = tmp_path / "config.json"
    with patch("backend.agent_retry._DEFAULT_CONFIG_PATH", non_existent):
        policy = load_retry_policy("executor")
    assert policy.max_retries == RetryPolicy.max_retries
    assert policy.base_delay_seconds == RetryPolicy.base_delay_seconds
    assert policy.max_delay_seconds == RetryPolicy.max_delay_seconds
    assert policy.backoff_factor == RetryPolicy.backoff_factor


def test_load_retry_policy_reads_from_config(tmp_path):
    config = {
        "policies": {
            "executor": {
                "retry": {
                    "max_retries": 5,
                    "base_delay_seconds": 10.0,
                    "max_delay_seconds": 120.0,
                    "backoff_factor": 3.0,
                }
            }
        }
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    with patch("backend.agent_retry._DEFAULT_CONFIG_PATH", config_path):
        policy = load_retry_policy("executor")
    assert policy.max_retries == 5
    assert policy.base_delay_seconds == pytest.approx(10.0)
    assert policy.max_delay_seconds == pytest.approx(120.0)
    assert policy.backoff_factor == pytest.approx(3.0)


def test_load_retry_policy_falls_back_for_missing_role(tmp_path):
    config = {"policies": {"project-manager": {}}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    with patch("backend.agent_retry._DEFAULT_CONFIG_PATH", config_path):
        policy = load_retry_policy("executor")
    # no executor entry → all defaults
    assert policy.max_retries == RetryPolicy.max_retries


def test_load_retry_policy_partial_overrides(tmp_path):
    config = {"policies": {"executor": {"retry": {"max_retries": 4}}}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    with patch("backend.agent_retry._DEFAULT_CONFIG_PATH", config_path):
        policy = load_retry_policy("executor")
    assert policy.max_retries == 4
    assert policy.base_delay_seconds == RetryPolicy.base_delay_seconds


def test_load_retry_policy_handles_corrupt_json(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{not valid json}")
    with patch("backend.agent_retry._DEFAULT_CONFIG_PATH", config_path):
        policy = load_retry_policy("executor")
    assert policy.max_retries == RetryPolicy.max_retries


# ------------------------------------------------------------------
# record_attempt — blackboard tracking
# ------------------------------------------------------------------


def test_record_attempt_creates_entry(isolated_bb):
    result = record_attempt(14, "executor")
    assert result["attempt"] == 1
    assert result["agent"] == "executor"
    assert "last_attempt" in result


def test_record_attempt_increments(isolated_bb):
    record_attempt(14, "executor")
    record_attempt(14, "executor")
    result = record_attempt(14, "executor")
    assert result["attempt"] == 3


def test_record_attempt_persists_in_blackboard(isolated_bb):
    record_attempt(42, "code-reviewer")
    stored = isolated_bb.read("retries/42")
    assert stored is not None
    assert stored["attempt"] == 1
    assert stored["agent"] == "code-reviewer"


def test_record_attempt_independent_discussions(isolated_bb):
    record_attempt(1, "executor")
    record_attempt(1, "executor")
    record_attempt(2, "executor")
    assert isolated_bb.read("retries/1")["attempt"] == 2
    assert isolated_bb.read("retries/2")["attempt"] == 1


# ------------------------------------------------------------------
# clear_retries — blackboard cleanup
# ------------------------------------------------------------------


def test_clear_retries_removes_entry(isolated_bb):
    record_attempt(14, "executor")
    clear_retries(14)
    assert isolated_bb.read("retries/14") is None


def test_clear_retries_idempotent(isolated_bb):
    # Clearing a non-existent discussion should not raise
    clear_retries(999)
    assert isolated_bb.read("retries/999") is None


def test_clear_retries_does_not_affect_other_discussions(isolated_bb):
    record_attempt(10, "executor")
    record_attempt(11, "executor")
    clear_retries(10)
    assert isolated_bb.read("retries/10") is None
    assert isolated_bb.read("retries/11") is not None
