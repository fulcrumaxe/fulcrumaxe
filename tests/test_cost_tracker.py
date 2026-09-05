"""
Tests for backend/cost_tracker.py — per-Discussion cost aggregation.

Covers Discussion #401 acceptance criteria:
- agent_breakdown and pr_breakdown on per-Discussion entries
- per-discussion and top CLI aliases
- record_spend pr= kwarg written to blackboard
- summary subcommand unchanged (regression)
"""

from pathlib import Path

import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
from backend.budget import BudgetTracker
from backend.cost_tracker import CostTracker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_budget(tmp_path) -> BudgetTracker:
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    return bt


def _make_tracker(tmp_path) -> CostTracker:
    bb = Blackboard(root=tmp_path / "blackboard")
    return CostTracker(bb=bb)


def _seed_records(bt: BudgetTracker) -> None:
    """Seed 4 spend records across 2 discussions, 2 roles, 2 PRs."""
    # Discussion 10 — executor, PR 100
    bt.record_spend("exec-10-1", "executor", 10000, 2000, discussion=10, pr=100)
    # Discussion 10 — code-reviewer, PR 100
    bt.record_spend("rev-10-1", "code-reviewer", 5000, 1000, discussion=10, pr=100)
    # Discussion 20 — executor, PR 200
    bt.record_spend("exec-20-1", "executor", 8000, 1500, discussion=20, pr=200)
    # Discussion 20 — security-reviewer, no PR
    bt.record_spend("sec-20-1", "security-reviewer", 3000, 800, discussion=20)


# ---------------------------------------------------------------------------
# 1. agent_breakdown correctness
# ---------------------------------------------------------------------------

def test_agent_breakdown_keys(tmp_path):
    bt = _make_budget(tmp_path)
    _seed_records(bt)
    ct = _make_tracker(tmp_path)
    full = ct.get_session_cost()
    by_disc = {e["discussion"]: e for e in full["by_discussion"]}

    # Discussion 10 should have executor + code-reviewer
    ab10 = by_disc[10]["agent_breakdown"]
    assert set(ab10.keys()) == {"executor", "code-reviewer"}

    # Discussion 20 should have executor + security-reviewer
    ab20 = by_disc[20]["agent_breakdown"]
    assert set(ab20.keys()) == {"executor", "security-reviewer"}


def test_agent_breakdown_sums(tmp_path):
    bt = _make_budget(tmp_path)
    _seed_records(bt)
    ct = _make_tracker(tmp_path)
    full = ct.get_session_cost()
    by_disc = {e["discussion"]: e for e in full["by_discussion"]}

    # For discussion 10:
    #   executor: input=10000, output=2000, model=default (rates: 0.003/1k in + 0.015/1k out)
    #   = 0.03 + 0.03 = 0.06
    #   code-reviewer: 5000 in + 1000 out = 0.015 + 0.015 = 0.030
    ab10 = by_disc[10]["agent_breakdown"]
    assert abs(ab10["executor"] - 0.06) < 1e-4
    assert abs(ab10["code-reviewer"] - 0.030) < 1e-4


# ---------------------------------------------------------------------------
# 2. pr_breakdown correctness
# ---------------------------------------------------------------------------

def test_pr_breakdown_keys(tmp_path):
    bt = _make_budget(tmp_path)
    _seed_records(bt)
    ct = _make_tracker(tmp_path)
    full = ct.get_session_cost()
    by_disc = {e["discussion"]: e for e in full["by_discussion"]}

    # Discussion 10: both records tagged PR 100
    pb10 = by_disc[10]["pr_breakdown"]
    assert "100" in pb10
    assert len(pb10) == 1

    # Discussion 20: one record with PR 200, one with no PR
    pb20 = by_disc[20]["pr_breakdown"]
    assert "200" in pb20
    # The security-reviewer record had no PR — should not appear
    assert len(pb20) == 1


def test_pr_breakdown_sums(tmp_path):
    bt = _make_budget(tmp_path)
    _seed_records(bt)
    ct = _make_tracker(tmp_path)
    full = ct.get_session_cost()
    by_disc = {e["discussion"]: e for e in full["by_discussion"]}

    # PR 100 covers executor (0.06) + code-reviewer (0.030) = 0.090
    pb10 = by_disc[10]["pr_breakdown"]
    assert abs(pb10["100"] - 0.090) < 1e-4


# ---------------------------------------------------------------------------
# 3. record_spend pr= kwarg
# ---------------------------------------------------------------------------

def test_record_spend_pr_written(tmp_path):
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    bt.record_spend("exec-pr-test", "executor", 100, 100, discussion=5, pr=42)

    # Read back the blackboard record
    keys = bb.list_keys("budget/agents/")
    assert any("exec-pr-test" in k for k in keys)
    record = bb.read("budget/agents/exec-pr-test")
    assert record is not None
    assert record.get("pr") == 42


def test_record_spend_no_pr_not_written(tmp_path):
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    bt.record_spend("exec-nope", "executor", 100, 100, discussion=5)
    record = bb.read("budget/agents/exec-nope")
    assert record is not None
    assert "pr" not in record


# ---------------------------------------------------------------------------
# 4. per-discussion CLI alias
# ---------------------------------------------------------------------------

def test_per_discussion_cli_alias(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bt = _make_budget(tmp_path)
    bt.record_spend("exec-1", "executor", 10000, 2000, discussion=401, pr=500)
    ct = _make_tracker(tmp_path)

    import io
    from contextlib import redirect_stdout
    from backend.cost_tracker import main

    # Monkey-patch CostTracker to use the temp blackboard
    import backend.cost_tracker as _ct_mod
    orig_cls = _ct_mod.CostTracker
    _ct_mod.CostTracker = lambda: ct  # type: ignore[assignment]
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["per-discussion", "--discussion", "401"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["discussion"] == 401
        assert "agent_breakdown" in data
        assert "pr_breakdown" in data
    finally:
        _ct_mod.CostTracker = orig_cls


def test_per_discussion_missing(tmp_path):
    import io
    from contextlib import redirect_stdout
    from backend.cost_tracker import main

    ct = _make_tracker(tmp_path)
    import backend.cost_tracker as _ct_mod
    orig_cls = _ct_mod.CostTracker
    _ct_mod.CostTracker = lambda: ct  # type: ignore[assignment]
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["per-discussion", "--discussion", "99999"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data is None or data == "null"
    finally:
        _ct_mod.CostTracker = orig_cls


# ---------------------------------------------------------------------------
# 5. top CLI alias
# ---------------------------------------------------------------------------

def test_top_sorted_desc(tmp_path):
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    # Seed 3 discussions with different costs
    bt.record_spend("a1", "executor", 50000, 5000, discussion=1)
    bt.record_spend("a2", "executor", 10000, 1000, discussion=2)
    bt.record_spend("a3", "executor", 30000, 3000, discussion=3)

    ct = CostTracker(bb=bb)
    import io
    from contextlib import redirect_stdout
    from backend.cost_tracker import main
    import backend.cost_tracker as _ct_mod
    orig_cls = _ct_mod.CostTracker
    _ct_mod.CostTracker = lambda: ct  # type: ignore[assignment]
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["top", "--limit", "10"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)
        # Should be sorted desc by total_cost_usd
        costs = [e["total_cost_usd"] for e in data]
        assert costs == sorted(costs, reverse=True)
    finally:
        _ct_mod.CostTracker = orig_cls


def test_top_respects_limit(tmp_path):
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    for i in range(5):
        bt.record_spend(f"a{i}", "executor", 1000 * (i + 1), 100, discussion=i + 1)

    ct = CostTracker(bb=bb)
    import io
    from contextlib import redirect_stdout
    from backend.cost_tracker import main
    import backend.cost_tracker as _ct_mod
    orig_cls = _ct_mod.CostTracker
    _ct_mod.CostTracker = lambda: ct  # type: ignore[assignment]
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["top", "--limit", "2"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert len(data) == 2
    finally:
        _ct_mod.CostTracker = orig_cls


def test_top_excludes_unattributed(tmp_path):
    """Records without a discussion= field should not appear in top output."""
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session()
    bt.record_spend("a1", "executor", 50000, 5000)  # no discussion
    bt.record_spend("a2", "executor", 10000, 1000, discussion=99)

    ct = CostTracker(bb=bb)
    import io
    from contextlib import redirect_stdout
    from backend.cost_tracker import main
    import backend.cost_tracker as _ct_mod
    orig_cls = _ct_mod.CostTracker
    _ct_mod.CostTracker = lambda: ct  # type: ignore[assignment]
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["top", "--limit", "10"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        disc_numbers = [e["discussion"] for e in data]
        assert 99 in disc_numbers
        # The unattributed record has no discussion; should not be in output
        for e in data:
            assert e["discussion"] is not None
    finally:
        _ct_mod.CostTracker = orig_cls


# ---------------------------------------------------------------------------
# 6. summary regression — existing keys unchanged
# ---------------------------------------------------------------------------

def test_summary_keys_unchanged(tmp_path):
    bt = _make_budget(tmp_path)
    _seed_records(bt)
    ct = _make_tracker(tmp_path)
    full = ct.get_session_cost()

    assert "total_cost_usd" in full
    assert "by_agent" in full
    assert "by_discussion" in full
    assert "model_breakdown" in full

    # Each by_discussion entry retains all original keys
    for entry in full["by_discussion"]:
        assert "discussion" in entry
        assert "cost_usd" in entry
        assert "total_cost_usd" in entry
        assert "agents" in entry
        assert "total_input_tokens" in entry
        assert "total_output_tokens" in entry
        assert "agent_count" in entry
        # New keys
        assert "agent_breakdown" in entry
        assert "pr_breakdown" in entry
