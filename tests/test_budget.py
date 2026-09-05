"""
Tests for backend/budget.py — BudgetTracker class.
"""

from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
from backend.budget import BudgetTracker, _DEFAULT_BUDGET


def _make_tracker(tmp_path):
    """Create an isolated BudgetTracker backed by a temp blackboard."""
    bb = Blackboard(root=tmp_path / "blackboard")
    return BudgetTracker(bb=bb), bb


def test_init_session_sets_ceiling(tmp_path):
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=1000)
    status = bt.get_status()
    assert status["ceiling"] == 1000


def test_init_session_default_ceiling(tmp_path):
    bt, _ = _make_tracker(tmp_path)
    bt.init_session()
    status = bt.get_status()
    assert status["ceiling"] == _DEFAULT_BUDGET["session_ceiling"]


def test_check_budget_allowed(tmp_path):
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=1_000_000)
    result = bt.check_budget("executor")
    assert result["allowed"] is True
    assert result["remaining"] == 1_000_000


def test_check_budget_over(tmp_path):
    bt, _ = _make_tracker(tmp_path)
    # Set a very tight ceiling — less than per_agent_ceiling
    bt.init_session(ceiling=100)
    # Spend it all
    bt.record_spend("agent-1", "executor", 60, 40)
    result = bt.check_budget("executor")
    assert result["allowed"] is False


def test_record_spend_increments_total(tmp_path):
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=5_000_000)
    bt.record_spend("agent-1", "executor", 1000, 500)
    status = bt.get_status()
    assert status["spent"] == 1500


def test_record_spend_stores_agent_record(tmp_path):
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=5_000_000)
    bt.record_spend("exec-1", "executor", 2000, 800)
    status = bt.get_status()
    agents = status["agents"]
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "exec-1"
    assert agents[0]["agent"] == "executor"
    assert agents[0]["input"] == 2000
    assert agents[0]["output"] == 800
    assert agents[0]["total"] == 2800


def test_check_budget_warn_threshold(tmp_path):
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=1000)
    # Spend 81% — above the 80% warn threshold
    bt.record_spend("agent-w", "executor", 810, 0)
    result = bt.check_budget("executor")
    assert result["warn"] is True


def test_reset_clears_all(tmp_path):
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=5_000_000)
    bt.record_spend("agent-r", "executor", 5000, 1000)
    bt.reset()
    status = bt.get_status()
    assert status["spent"] == 0
    assert status["agents"] == []


def test_budget_uses_blackboard(tmp_path):
    """Integration: BudgetTracker writes to the blackboard — verify the key directly."""
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session(ceiling=5_000_000)
    bt.record_spend("exec-bb", "executor", 3000, 1000)

    # Read the blackboard key directly
    raw_spent = bb.read("budget/session_spent")
    assert raw_spent == 4000


# ---------------------------------------------------------------------------
# Tests for Discussion #568 — spent derived from agents[], not session_spent
# ---------------------------------------------------------------------------


def test_spent_derived_from_agents_not_session_key(tmp_path):
    """get_status() derives spent from agents[], ignoring a stale session_spent key.

    Simulates the CAS-race scenario where session_spent gets stuck at 0
    while agents[] accumulates real spend entries.
    """
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session(ceiling=5_000_000)

    bt.record_spend("a1", "executor", 10_000, 2_000)
    bt.record_spend("a2", "code-reviewer", 5_000, 1_000)

    # Corrupt session_spent — simulate the CAS-race scenario
    bb.write("budget/session_spent", 0, updated_by="test-corrupt")

    status = bt.get_status()
    assert status["spent"] == 18_000  # 12_000 + 6_000


def test_check_budget_uses_derived_spent(tmp_path):
    """check_budget() uses agents[]-derived spent for ceiling enforcement."""
    bt, bb = _make_tracker(tmp_path)
    bt.init_session(ceiling=1_000)
    bt.record_spend("a1", "executor", 600, 400)  # exactly at ceiling

    # Corrupt session_spent
    bb.write("budget/session_spent", 0, updated_by="test-corrupt")

    result = bt.check_budget("executor")
    assert result["spent"] == 1_000
    assert result["remaining"] == 0
    assert result["allowed"] is False


def test_warn_threshold_fires_when_derived_spent_crosses(tmp_path):
    """Warn flag triggers when agents[]-derived spent > 80% of ceiling."""
    bt, bb = _make_tracker(tmp_path)
    bt.init_session(ceiling=1_000)

    bt.record_spend("a1", "executor", 850, 0)

    # Corrupt session_spent
    bb.write("budget/session_spent", 0, updated_by="test-corrupt")

    result = bt.check_budget("executor")
    assert result["warn"] is True


def test_ceiling_blocks_when_spent_exceeds(tmp_path):
    """allowed=False when agents[]-derived spent >= ceiling."""
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=200)
    bt.record_spend("a1", "executor", 150, 50)  # used 200 = ceiling

    result = bt.check_budget("executor")
    assert result["allowed"] is False
    assert result["remaining"] == 0


def test_multiple_records_accumulate_correctly(tmp_path):
    """Multiple record_spend calls accumulate correctly in get_status."""
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=5_000_000)

    bt.record_spend("r1", "executor", 1_000, 200)
    bt.record_spend("r2", "code-reviewer", 2_000, 400)
    bt.record_spend("r3", "security-reviewer", 500, 100)

    status = bt.get_status()
    assert status["spent"] == 4_200
    assert len(status["agents"]) == 3


def test_spent_zero_when_no_agents(tmp_path):
    """Fresh session with no recorded agents returns spent=0."""
    bt, _ = _make_tracker(tmp_path)
    bt.init_session(ceiling=5_000_000)

    status = bt.get_status()
    assert status["spent"] == 0
    assert status["agents"] == []
