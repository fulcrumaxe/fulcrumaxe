"""tests/orchestrator/test_credit_tracker.py — Unit tests for credit tracker."""

import json
import time
from pathlib import Path

import pytest

from backend.orchestrator.credit_tracker import CreditTracker, _DEFAULT_INITIAL_USD


@pytest.fixture
def credit_file(tmp_path):
    return tmp_path / "sdk_credit.json"


@pytest.fixture
def tracker(credit_file):
    return CreditTracker(credit_file=credit_file)


class TestCreditTrackerBasics:
    def test_initial_state_full(self, tracker):
        assert tracker.remaining_usd() == _DEFAULT_INITIAL_USD
        assert tracker.used_usd() == 0.0

    def test_not_exhausted_initially(self, tracker):
        assert not tracker.is_exhausted()

    def test_soft_cap_not_breached_initially(self, tracker):
        # $200 remaining >> $50 threshold
        assert not tracker.soft_cap_breached()

    def test_decrement_reduces_remaining(self, tracker):
        tracker.decrement(10.0)
        assert abs(tracker.remaining_usd() - 190.0) < 0.001

    def test_decrement_negative_raises(self, tracker):
        with pytest.raises(ValueError):
            tracker.decrement(-1.0)

    def test_soft_cap_triggered_at_50_remaining(self, credit_file):
        """soft_cap_breached() returns True when remaining <= $50."""
        data = {
            "initial_usd": 200.0,
            "used_usd": 150.0,  # $50 remaining
            "last_updated": "2026-05-16T00:00:00Z",
            "cache_ts": "2026-05-16T00:00:00Z",
        }
        credit_file.write_text(json.dumps(data))
        tracker = CreditTracker(credit_file=credit_file)
        assert tracker.soft_cap_breached()

    def test_exhausted_at_zero(self, credit_file):
        data = {
            "initial_usd": 200.0,
            "used_usd": 200.0,
            "last_updated": "2026-05-16T00:00:00Z",
            "cache_ts": "2026-05-16T00:00:00Z",
        }
        credit_file.write_text(json.dumps(data))
        tracker = CreditTracker(credit_file=credit_file)
        assert tracker.is_exhausted()
        assert tracker.remaining_usd() == 0.0

    def test_reset_restores_full_balance(self, tracker):
        tracker.decrement(50.0)
        tracker.reset()
        assert tracker.remaining_usd() == _DEFAULT_INITIAL_USD

    def test_file_persists_across_instances(self, credit_file):
        t1 = CreditTracker(credit_file=credit_file)
        t1.decrement(25.0)

        t2 = CreditTracker(credit_file=credit_file)
        assert abs(t2.remaining_usd() - 175.0) < 0.001

    def test_snapshot_returns_copy(self, tracker):
        snap = tracker.snapshot()
        assert isinstance(snap, dict)
        assert "initial_usd" in snap
        assert "used_usd" in snap


class TestCreditTrackerFileResilience:
    def test_missing_file_creates_defaults(self, tmp_path):
        missing = tmp_path / "nosuchdir" / "sdk_credit.json"
        tracker = CreditTracker(credit_file=missing)
        assert tracker.remaining_usd() == _DEFAULT_INITIAL_USD
        assert missing.exists()

    def test_corrupt_json_returns_defaults(self, credit_file):
        credit_file.write_text("NOT JSON {{")
        tracker = CreditTracker(credit_file=credit_file)
        assert tracker.remaining_usd() == _DEFAULT_INITIAL_USD
