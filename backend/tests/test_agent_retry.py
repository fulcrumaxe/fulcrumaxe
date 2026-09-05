"""
Tests for backend/agent_retry.py

Covers:
- should_retry: retry decision when attempts remain vs. max reached
- Exponential backoff delay calculation and sequence
- max-attempts boundary (exact cap behaviour)
- load_retry_policy: loading from injected config, missing policy → defaults
- record_attempt / clear_retries via a temp blackboard (real state never touched)

Run with:
    python3 -m pytest backend/tests/test_agent_retry.py -v
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.agent_retry as retry_mod
from backend.agent_retry import (
    RetryDecision,
    RetryPolicy,
    clear_retries,
    load_retry_policy,
    record_attempt,
    should_retry,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_blackboard(tmp_path):
    """Replace the module-level _bb with a fresh temp-dir Blackboard.

    Guarantees tests never touch ~/.autonomous-forever-state/ or any real dir.
    """
    from backend.blackboard import Blackboard
    bb = Blackboard(root=tmp_path / "bb")
    with patch.object(retry_mod, "_bb", bb):
        yield bb


# ---------------------------------------------------------------------------
# should_retry — retry decision logic
# ---------------------------------------------------------------------------


class TestShouldRetryDecision:
    """Retry decision: fail/needs-fix verdict → retry if attempts remain."""

    def test_retry_on_first_failure(self):
        """attempt=0 with retries remaining → retry=True."""
        policy = RetryPolicy(max_retries=2, base_delay_seconds=10.0)
        decision = should_retry(14, "executor", attempt=0, policy=policy)
        assert decision.retry is True
        assert decision.delay_seconds > 0

    def test_retry_on_second_failure(self):
        """attempt=1 still within max_retries=2 → retry=True."""
        policy = RetryPolicy(max_retries=2, base_delay_seconds=10.0)
        decision = should_retry(14, "executor", attempt=1, policy=policy)
        assert decision.retry is True

    def test_no_retry_at_max(self):
        """attempt == max_retries → retry=False."""
        policy = RetryPolicy(max_retries=2, base_delay_seconds=10.0)
        decision = should_retry(14, "executor", attempt=2, policy=policy)
        assert decision.retry is False
        assert decision.delay_seconds == 0.0

    def test_no_retry_beyond_max(self):
        """attempt > max_retries → retry=False (defensive check)."""
        policy = RetryPolicy(max_retries=2, base_delay_seconds=10.0)
        decision = should_retry(14, "executor", attempt=5, policy=policy)
        assert decision.retry is False

    def test_reason_string_contains_role_and_discussion(self):
        """Reason message includes the agent role and discussion number."""
        policy = RetryPolicy(max_retries=3)
        decision = should_retry(42, "code-reviewer", attempt=0, policy=policy)
        assert "code-reviewer" in decision.reason
        assert "42" in decision.reason

    def test_fail_reason_mentions_max_retries(self):
        """When refusing, reason mentions the max_retries cap."""
        policy = RetryPolicy(max_retries=1)
        decision = should_retry(7, "executor", attempt=1, policy=policy)
        assert decision.retry is False
        assert "1" in decision.reason  # max_retries value present

    def test_returns_retry_decision_type(self):
        """Return type is always RetryDecision."""
        policy = RetryPolicy()
        result = should_retry(1, "executor", attempt=0, policy=policy)
        assert isinstance(result, RetryDecision)


# ---------------------------------------------------------------------------
# should_retry — exponential backoff schedule
# ---------------------------------------------------------------------------


class TestBackoffSchedule:
    """Delays grow per the policy: min(base * factor^attempt, max_delay)."""

    def test_attempt0_gives_base_delay(self):
        """attempt=0: delay = base_delay * factor^0 = base_delay."""
        policy = RetryPolicy(
            max_retries=5,
            base_delay_seconds=30.0,
            backoff_factor=2.0,
            max_delay_seconds=9999.0,
        )
        d = should_retry(1, "executor", attempt=0, policy=policy)
        assert d.delay_seconds == pytest.approx(30.0)

    def test_attempt1_doubles(self):
        """attempt=1: delay = base * 2^1 = 60."""
        policy = RetryPolicy(
            max_retries=5,
            base_delay_seconds=30.0,
            backoff_factor=2.0,
            max_delay_seconds=9999.0,
        )
        d = should_retry(1, "executor", attempt=1, policy=policy)
        assert d.delay_seconds == pytest.approx(60.0)

    def test_attempt2_quadruples(self):
        """attempt=2: delay = base * 2^2 = 120."""
        policy = RetryPolicy(
            max_retries=5,
            base_delay_seconds=30.0,
            backoff_factor=2.0,
            max_delay_seconds=9999.0,
        )
        d = should_retry(1, "executor", attempt=2, policy=policy)
        assert d.delay_seconds == pytest.approx(120.0)

    def test_full_sequence_grows_correctly(self):
        """The full delay sequence matches base * factor^attempt."""
        policy = RetryPolicy(
            max_retries=4,
            base_delay_seconds=10.0,
            backoff_factor=3.0,
            max_delay_seconds=9999.0,
        )
        expected = [10.0, 30.0, 90.0, 270.0]
        for attempt, exp_delay in enumerate(expected):
            d = should_retry(99, "executor", attempt=attempt, policy=policy)
            assert d.retry is True
            assert d.delay_seconds == pytest.approx(exp_delay), (
                f"attempt={attempt}: expected {exp_delay}, got {d.delay_seconds}"
            )

    def test_delay_capped_at_max(self):
        """Delay never exceeds max_delay_seconds."""
        policy = RetryPolicy(
            max_retries=5,
            base_delay_seconds=100.0,
            backoff_factor=10.0,
            max_delay_seconds=300.0,
        )
        # attempt=2: 100 * 10^2 = 10000, capped at 300
        d = should_retry(1, "executor", attempt=2, policy=policy)
        assert d.delay_seconds == pytest.approx(300.0)

    def test_delay_below_max_not_capped(self):
        """When raw delay < max_delay, it is returned unchanged."""
        policy = RetryPolicy(
            max_retries=5,
            base_delay_seconds=5.0,
            backoff_factor=2.0,
            max_delay_seconds=300.0,
        )
        # attempt=0: 5 * 1 = 5, well below 300
        d = should_retry(1, "executor", attempt=0, policy=policy)
        assert d.delay_seconds == pytest.approx(5.0)

    def test_delays_are_strictly_increasing_before_cap(self):
        """Each successive delay is larger than the previous (pre-cap)."""
        policy = RetryPolicy(
            max_retries=5,
            base_delay_seconds=2.0,
            backoff_factor=2.0,
            max_delay_seconds=99999.0,
        )
        delays = [
            should_retry(1, "executor", attempt=i, policy=policy).delay_seconds
            for i in range(5)
        ]
        for i in range(1, len(delays)):
            assert delays[i] > delays[i - 1], (
                f"delay[{i}]={delays[i]} should exceed delay[{i-1}]={delays[i-1]}"
            )


# ---------------------------------------------------------------------------
# max-attempts boundary
# ---------------------------------------------------------------------------


class TestMaxAttemptsBoundary:
    """Stops retrying exactly at the cap — no off-by-one."""

    def test_max_retries_one_allows_exactly_one_retry(self):
        """max_retries=1: attempt=0 retries, attempt=1 stops."""
        policy = RetryPolicy(max_retries=1, base_delay_seconds=5.0)
        assert should_retry(1, "executor", attempt=0, policy=policy).retry is True
        assert should_retry(1, "executor", attempt=1, policy=policy).retry is False

    def test_max_retries_zero_never_retries(self):
        """max_retries=0: even attempt=0 is refused."""
        policy = RetryPolicy(max_retries=0, base_delay_seconds=5.0)
        assert should_retry(1, "executor", attempt=0, policy=policy).retry is False

    def test_max_retries_three_allows_three_retries(self):
        """max_retries=3: attempts 0,1,2 retry; attempt=3 stops."""
        policy = RetryPolicy(max_retries=3, base_delay_seconds=1.0)
        for attempt in range(3):
            assert should_retry(1, "executor", attempt=attempt, policy=policy).retry is True
        assert should_retry(1, "executor", attempt=3, policy=policy).retry is False

    def test_boundary_is_exact_not_off_by_one(self):
        """attempt == max_retries is refused; attempt == max_retries - 1 is allowed."""
        policy = RetryPolicy(max_retries=4, base_delay_seconds=1.0)
        assert should_retry(1, "e", attempt=3, policy=policy).retry is True   # last allowed
        assert should_retry(1, "e", attempt=4, policy=policy).retry is False  # first refused


# ---------------------------------------------------------------------------
# load_retry_policy — config loading and defaults
# ---------------------------------------------------------------------------


class TestLoadRetryPolicy:
    """Policy loading from injected config; missing keys fall back to defaults."""

    def _make_config(self, tmp_path: Path, role: str, retry_cfg: dict) -> Path:
        """Write a minimal config.json with a retry section for *role*."""
        cfg = {"policies": {role: {"retry": retry_cfg}}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(cfg))
        return config_file

    def test_loads_max_retries_from_config(self, tmp_path):
        """max_retries in config overrides the dataclass default."""
        self._make_config(tmp_path, "executor", {"max_retries": 5})
        config_path = tmp_path / "config.json"
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", config_path):
            policy = load_retry_policy("executor")
        assert policy.max_retries == 5

    def test_loads_base_delay_from_config(self, tmp_path):
        """base_delay_seconds in config overrides the default."""
        self._make_config(tmp_path, "executor", {"base_delay_seconds": 60.0})
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", tmp_path / "config.json"):
            policy = load_retry_policy("executor")
        assert policy.base_delay_seconds == pytest.approx(60.0)

    def test_loads_backoff_factor_from_config(self, tmp_path):
        """backoff_factor in config overrides the default."""
        self._make_config(tmp_path, "executor", {"backoff_factor": 3.0})
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", tmp_path / "config.json"):
            policy = load_retry_policy("executor")
        assert policy.backoff_factor == pytest.approx(3.0)

    def test_loads_max_delay_from_config(self, tmp_path):
        """max_delay_seconds in config overrides the default."""
        self._make_config(tmp_path, "executor", {"max_delay_seconds": 120.0})
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", tmp_path / "config.json"):
            policy = load_retry_policy("executor")
        assert policy.max_delay_seconds == pytest.approx(120.0)

    def test_missing_role_returns_defaults(self, tmp_path):
        """Role absent from config → documented defaults."""
        cfg = {"policies": {}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", tmp_path / "config.json"):
            policy = load_retry_policy("nonexistent-role")
        assert policy.max_retries == RetryPolicy.max_retries
        assert policy.base_delay_seconds == RetryPolicy.base_delay_seconds
        assert policy.max_delay_seconds == RetryPolicy.max_delay_seconds
        assert policy.backoff_factor == RetryPolicy.backoff_factor

    def test_missing_config_file_returns_defaults(self, tmp_path):
        """Unreadable config → documented defaults (graceful fallback)."""
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", tmp_path / "no-such-file.json"):
            policy = load_retry_policy("executor")
        assert policy.max_retries == RetryPolicy.max_retries
        assert policy.base_delay_seconds == RetryPolicy.base_delay_seconds

    def test_malformed_config_returns_defaults(self, tmp_path):
        """JSON parse error → falls back to defaults, does not raise."""
        bad_config = tmp_path / "config.json"
        bad_config.write_text("{not valid json}")
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", bad_config):
            policy = load_retry_policy("executor")
        assert policy.max_retries == RetryPolicy.max_retries

    def test_partial_retry_section_uses_defaults_for_missing_keys(self, tmp_path):
        """Only max_retries in config: other fields still come from defaults."""
        self._make_config(tmp_path, "executor", {"max_retries": 7})
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", tmp_path / "config.json"):
            policy = load_retry_policy("executor")
        assert policy.max_retries == 7
        # Unspecified fields are defaults
        assert policy.base_delay_seconds == RetryPolicy.base_delay_seconds
        assert policy.backoff_factor == RetryPolicy.backoff_factor
        assert policy.max_delay_seconds == RetryPolicy.max_delay_seconds

    def test_role_specific_config_does_not_bleed_to_other_role(self, tmp_path):
        """Policy for 'executor' does not affect 'code-reviewer' lookup."""
        cfg = {
            "policies": {
                "executor": {"retry": {"max_retries": 9}},
                "code-reviewer": {"retry": {"max_retries": 1}},
            }
        }
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", tmp_path / "config.json"):
            p_exec = load_retry_policy("executor")
            p_rev = load_retry_policy("code-reviewer")
        assert p_exec.max_retries == 9
        assert p_rev.max_retries == 1

    def test_load_retry_policy_resolves_path_relative_to_repo(self, tmp_path):
        """load_retry_policy patches _DEFAULT_CONFIG_PATH correctly as a Path."""
        self._make_config(tmp_path, "executor", {"max_retries": 3})
        target = tmp_path / "config.json"
        with patch.object(retry_mod, "_DEFAULT_CONFIG_PATH", target):
            policy = load_retry_policy("executor")
        assert policy.max_retries == 3


# ---------------------------------------------------------------------------
# record_attempt / clear_retries — blackboard interaction
# ---------------------------------------------------------------------------


class TestRecordAttempt:
    """record_attempt increments per-discussion counter in the (mocked) blackboard."""

    def test_first_attempt_returns_one(self):
        state = record_attempt(10, "executor")
        assert state["attempt"] == 1

    def test_second_attempt_increments(self):
        record_attempt(10, "executor")
        state = record_attempt(10, "executor")
        assert state["attempt"] == 2

    def test_record_sets_agent_field(self):
        state = record_attempt(10, "code-reviewer")
        assert state["agent"] == "code-reviewer"

    def test_record_sets_last_attempt_iso(self):
        state = record_attempt(10, "executor")
        ts = state["last_attempt"]
        assert "T" in ts  # ISO-8601 format includes 'T'

    def test_different_discussions_are_independent(self):
        record_attempt(10, "executor")
        record_attempt(10, "executor")
        record_attempt(20, "executor")
        state_10 = record_attempt(10, "executor")
        state_20 = record_attempt(20, "executor")
        assert state_10["attempt"] == 3
        assert state_20["attempt"] == 2

    def test_clear_retries_resets_counter(self):
        record_attempt(30, "executor")
        record_attempt(30, "executor")
        clear_retries(30)
        # After clear, a fresh attempt should start at 1 again
        state = record_attempt(30, "executor")
        assert state["attempt"] == 1

    def test_clear_retries_removes_key_from_blackboard(self, isolated_blackboard):
        record_attempt(40, "executor")
        clear_retries(40)
        assert isolated_blackboard.read("retries/40") is None

    def test_clear_retries_nonexistent_does_not_raise(self):
        """clear_retries on a key that was never written should not raise."""
        clear_retries(99999)  # Should not raise


# ---------------------------------------------------------------------------
# Integration: record_attempt + should_retry together
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end: use record_attempt output as input to should_retry."""

    def test_retry_on_first_recorded_attempt(self):
        policy = RetryPolicy(max_retries=2, base_delay_seconds=5.0)
        state = record_attempt(50, "executor")
        # attempt index = recorded attempt - 1 (per module docstring)
        decision = should_retry(50, "executor", state["attempt"] - 1, policy)
        assert decision.retry is True

    def test_no_retry_after_max_recorded_attempts(self):
        policy = RetryPolicy(max_retries=2, base_delay_seconds=5.0)
        # Simulate 3 attempts (0-indexed: 0, 1, 2 — but max is 2)
        for _ in range(3):
            state = record_attempt(50, "executor")
        decision = should_retry(50, "executor", state["attempt"] - 1, policy)
        assert decision.retry is False

    def test_clear_then_retry_starts_fresh(self):
        policy = RetryPolicy(max_retries=1, base_delay_seconds=5.0)
        record_attempt(60, "executor")
        record_attempt(60, "executor")
        clear_retries(60)
        state = record_attempt(60, "executor")
        decision = should_retry(60, "executor", state["attempt"] - 1, policy)
        assert decision.retry is True  # fresh start: attempt=0 < max_retries=1


# ---------------------------------------------------------------------------
# CLI: check and clear subcommands
# ---------------------------------------------------------------------------


class TestCli:
    """CLI smoke tests — exercise main() with a mocked blackboard."""

    def test_check_no_retries_recorded(self, capsys):
        """check with no prior record → attempt=0, should show retry=yes if policy allows."""
        policy = RetryPolicy(max_retries=2, base_delay_seconds=5.0)
        with patch.object(retry_mod, "load_retry_policy", return_value=policy):
            rc = main(["check", "100", "executor"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "retry: yes" in out

    def test_check_after_max_attempts(self, capsys, isolated_blackboard):
        """check with max retries reached → retry: no."""
        policy = RetryPolicy(max_retries=1, base_delay_seconds=5.0)
        # Write attempt=1 directly so check sees max reached
        from backend.agent_retry import _retry_key
        isolated_blackboard.write(
            _retry_key(200), {"attempt": 1, "agent": "executor"}, updated_by="test"
        )
        with patch.object(retry_mod, "load_retry_policy", return_value=policy):
            rc = main(["check", "200", "executor"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "retry: no" in out

    def test_clear_command(self, capsys, isolated_blackboard):
        """clear command removes the retry state."""
        from backend.agent_retry import _retry_key
        isolated_blackboard.write(
            _retry_key(300), {"attempt": 2, "agent": "executor"}, updated_by="test"
        )
        rc = main(["clear", "300"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "cleared" in out
        assert isolated_blackboard.read(_retry_key(300)) is None
