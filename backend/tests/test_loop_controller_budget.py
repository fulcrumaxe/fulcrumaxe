"""Tests for the Loop Controller Budget tile data path.

D#1039 item 5: the Budget tile showed spent=0 despite real token use.

Root causes fixed:
1. budget.py record_spend() wrote the agent record AFTER the CAS increment on
   session_spent. A CAS failure (RuntimeError) prevented the agent record from
   ever being written, so _sum_agent_tokens() returned 0.
2. team_status._budget_summary() shelled out to `python3 backend/budget.py status`
   via _run(), which concatenates stdout+stderr.  Any warning line in stderr
   contaminated the JSON, causing json.loads to fail and returning no spent key
   (rendered as 0 in the UI).  Now uses BudgetTracker library directly.

These tests verify:
- record_spend writes the agent record before the CAS increment
- _budget_summary returns correct spent from blackboard agent records
- A session_spent of 0 in the blackboard does NOT mask non-zero actual spend
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.blackboard import Blackboard
from backend.budget import BudgetTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_budget_tracker(tmp_path: Path) -> BudgetTracker:
    """Return a BudgetTracker backed by a temporary Blackboard directory."""
    bb = Blackboard(root=tmp_path / "blackboard")
    bt = BudgetTracker(bb=bb)
    bt.init_session(ceiling=5_000_000)
    return bt


# ---------------------------------------------------------------------------
# record_spend: agent record survives CAS failure
# ---------------------------------------------------------------------------

class TestRecordSpendAgentRecordFirst:
    """record_spend must write the agent record before the CAS increment.

    Previously the CAS came first — a RuntimeError left the agent record
    unwritten and _sum_agent_tokens() returned 0.
    """

    def test_agent_record_written_when_cas_raises(self, tmp_path: Path) -> None:
        """If the CAS increment raises RuntimeError the agent record must still exist."""
        bt = _make_budget_tracker(tmp_path)

        # Force the CAS increment to fail by patching it.
        with patch.object(bt, "_cas_increment", side_effect=RuntimeError("CAS conflict")):
            # Should not re-raise — the RuntimeError is swallowed after the record is written.
            bt.record_spend(
                agent_id="executor-9999-1700000000",
                agent_role="executor",
                input_tokens=50_000,
                output_tokens=20_000,
                discussion=9999,
            )

        # The agent record must be present in the blackboard.
        status = bt.get_status()
        assert status["spent"] == 70_000, (
            f"expected 70000, got {status['spent']} — agent record was not written before CAS"
        )

    def test_agent_record_written_when_cas_succeeds(self, tmp_path: Path) -> None:
        """Normal path: CAS succeeds and agent record is written."""
        bt = _make_budget_tracker(tmp_path)
        bt.record_spend(
            agent_id="executor-100-1700000000",
            agent_role="executor",
            input_tokens=30_000,
            output_tokens=10_000,
            discussion=100,
        )
        status = bt.get_status()
        assert status["spent"] == 40_000

    def test_multiple_agents_accumulate_correctly(self, tmp_path: Path) -> None:
        """get_status() sums across all agent records."""
        bt = _make_budget_tracker(tmp_path)
        bt.record_spend("agent-a", "executor", 10_000, 5_000)
        bt.record_spend("agent-b", "code-reviewer", 20_000, 8_000)
        bt.record_spend("agent-c", "security-reviewer", 15_000, 6_000)
        status = bt.get_status()
        assert status["spent"] == 10_000 + 5_000 + 20_000 + 8_000 + 15_000 + 6_000  # 64_000


# ---------------------------------------------------------------------------
# get_status: session_spent=0 does NOT mask real spend
# ---------------------------------------------------------------------------

class TestGetStatusIgnoresSessionSpent:
    """get_status() derives spent from agents[], NOT from session_spent.

    session_spent is unreliable: CAS conflicts under concurrency silently drop
    increments, leaving it at 0 even after many agent completions.
    """

    def test_spent_nonzero_when_session_spent_is_zero(self, tmp_path: Path) -> None:
        """Even if session_spent is 0, the agent record sum must be returned."""
        bt = _make_budget_tracker(tmp_path)
        # Directly force session_spent back to 0 to simulate a CAS-missed session.
        bt._bb.write("budget/session_spent", 0, updated_by="test")
        # Record spend (the agent record write should succeed independently).
        bt.record_spend("exec-1", "executor", 100_000, 40_000)
        # Manually reset session_spent to 0 again to confirm get_status doesn't read it.
        bt._bb.write("budget/session_spent", 0, updated_by="test")

        status = bt.get_status()
        assert status["spent"] == 140_000, (
            f"spent should be 140000 (from agents[]), got {status['spent']} "
            "(session_spent=0 must not override agents[] sum)"
        )


# ---------------------------------------------------------------------------
# team_status._budget_summary: returns correct spent from BudgetTracker
# ---------------------------------------------------------------------------

class TestTeamStatusBudgetSummary:
    """_budget_summary() must return spent matching actual token totals.

    The Loop Controller Budget tile calls team_status.snapshot → _gather →
    _budget_summary.  When this returned {'spent': 0} or {'spent': None} the
    tile showed 0.

    We test _budget_summary indirectly by verifying BudgetTracker.get_status()
    (the library it now calls in-process) returns the correct shape and values.
    This is equivalent to verifying the tile data — _budget_summary is a thin
    wrapper that passes through ceiling/spent/remaining from get_status().
    """

    def test_get_status_returns_correct_spent(self, tmp_path: Path) -> None:
        """BudgetTracker.get_status() reports spent matching recorded agent tokens."""
        bb = Blackboard(root=tmp_path / "blackboard")
        bt = BudgetTracker(bb=bb)
        bt.init_session(ceiling=5_000_000)
        bt.record_spend("agent-x", "executor", 80_000, 30_000)
        bt.record_spend("agent-y", "code-reviewer", 40_000, 15_000)
        expected_spent = 80_000 + 30_000 + 40_000 + 15_000  # 165_000

        status = bt.get_status()
        assert status["spent"] == expected_spent, (
            f"expected spent={expected_spent}, got {status['spent']}"
        )

    def test_get_status_spent_not_zero_when_agents_present(self, tmp_path: Path) -> None:
        """get_status() must not return spent=0 when agent records exist."""
        bb = Blackboard(root=tmp_path / "blackboard")
        bt = BudgetTracker(bb=bb)
        bt.init_session(ceiling=5_000_000)
        bt.record_spend("heavy-agent", "executor", 200_000, 75_000)

        status = bt.get_status()
        assert status["spent"] == 275_000
        assert status["spent"] != 0, "spent must not be 0 when agents have token records"

    def test_budget_summary_returns_three_keys(self, tmp_path: Path) -> None:
        """_budget_summary must return ceiling, spent, remaining (all non-None)."""
        import backend.team_status as ts_mod

        bb = Blackboard(root=tmp_path / "blackboard")
        bt = BudgetTracker(bb=bb)
        bt.init_session(ceiling=5_000_000)
        bt.record_spend("agent-z", "executor", 50_000, 20_000)

        # Inject our isolated BudgetTracker via sys.modules so the in-function
        # `from budget import BudgetTracker` resolves to our stub.
        import sys
        import types

        fake_budget_mod = types.ModuleType("budget")
        fake_budget_mod.BudgetTracker = lambda: bt  # type: ignore[attr-defined]
        original = sys.modules.get("budget")
        sys.modules["budget"] = fake_budget_mod
        try:
            result = ts_mod._budget_summary()
        finally:
            if original is None:
                sys.modules.pop("budget", None)
            else:
                sys.modules["budget"] = original

        assert "ceiling" in result, f"missing 'ceiling' in {result}"
        assert "spent" in result, f"missing 'spent' in {result}"
        assert "remaining" in result, f"missing 'remaining' in {result}"
        assert result.get("spent") is not None, "spent must not be None"
        assert result["spent"] == 70_000, (
            f"expected spent=70000, got {result['spent']}"
        )

    def test_budget_summary_has_no_agents_recorded_field(self, tmp_path: Path) -> None:
        """_budget_summary must return no_agents_recorded=True when blackboard is empty."""
        import backend.team_status as ts_mod

        bb = Blackboard(root=tmp_path / "blackboard")
        bt = BudgetTracker(bb=bb)
        bt.init_session(ceiling=5_000_000)
        # No agents recorded yet

        fake_budget_mod = types.ModuleType("budget")
        fake_budget_mod.BudgetTracker = lambda: bt  # type: ignore[attr-defined]
        fake_blackboard_mod = types.ModuleType("blackboard")
        fake_blackboard_mod.Blackboard = Blackboard  # type: ignore[attr-defined]
        orig_budget = sys.modules.get("budget")
        orig_blackboard = sys.modules.get("blackboard")
        sys.modules["budget"] = fake_budget_mod
        sys.modules["blackboard"] = fake_blackboard_mod
        try:
            result = ts_mod._budget_summary()
        finally:
            if orig_budget is None:
                sys.modules.pop("budget", None)
            else:
                sys.modules["budget"] = orig_budget
            if orig_blackboard is None:
                sys.modules.pop("blackboard", None)
            else:
                sys.modules["blackboard"] = orig_blackboard

        assert "no_agents_recorded" in result, f"missing 'no_agents_recorded' in {result}"
        assert result["no_agents_recorded"] is True, (
            f"expected no_agents_recorded=True when no agents, got {result['no_agents_recorded']}"
        )

    def test_budget_summary_no_agents_recorded_false_when_agents_present(self, tmp_path: Path) -> None:
        """_budget_summary returns no_agents_recorded=False when agents have been recorded."""
        import backend.team_status as ts_mod

        bb = Blackboard(root=tmp_path / "blackboard")
        bt = BudgetTracker(bb=bb)
        bt.init_session(ceiling=5_000_000)
        bt.record_spend("agent-1", "executor", 10_000, 5_000)

        fake_budget_mod = types.ModuleType("budget")
        fake_budget_mod.BudgetTracker = lambda: bt  # type: ignore[attr-defined]
        fake_blackboard_mod = types.ModuleType("blackboard")
        fake_blackboard_mod.Blackboard = Blackboard  # type: ignore[attr-defined]
        orig_budget = sys.modules.get("budget")
        orig_blackboard = sys.modules.get("blackboard")
        sys.modules["budget"] = fake_budget_mod
        sys.modules["blackboard"] = fake_blackboard_mod
        try:
            result = ts_mod._budget_summary()
        finally:
            if orig_budget is None:
                sys.modules.pop("budget", None)
            else:
                sys.modules["budget"] = orig_budget
            if orig_blackboard is None:
                sys.modules.pop("blackboard", None)
            else:
                sys.modules["blackboard"] = orig_blackboard

        assert result.get("no_agents_recorded") is False, (
            f"expected no_agents_recorded=False when agents exist, got {result.get('no_agents_recorded')}"
        )


# ---------------------------------------------------------------------------
# _budget_summary: project-scoped blackboard reads from correct state dir
# ---------------------------------------------------------------------------

class TestBudgetSummaryProjectScoped:
    """_budget_summary(project=...) must read from the project's blackboard, not AF's.

    When the dashboard switches to a non-AF project (e.g. projectb), the Budget tile
    should reflect that project's token spend rather than AF's.
    """

    def test_project_budget_reads_from_project_blackboard(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Budget data for a project comes from its own blackboard, not AF's."""
        import backend.team_status as ts_mod
        from backend.state_paths import ProjectPaths

        # Set up a fake project blackboard with known spend
        project_bb_dir = tmp_path / "fake-project-state" / "blackboard"
        project_bb = Blackboard(root=project_bb_dir)
        project_bt = BudgetTracker(bb=project_bb)
        project_bt.init_session(ceiling=1_000_000)
        project_bt.record_spend("proj-agent-1", "executor", 50_000, 10_000)

        # Set up AF blackboard with different spend to confirm we read the right one
        af_bb_dir = tmp_path / "af-state" / "blackboard"
        af_bb = Blackboard(root=af_bb_dir)
        af_bt = BudgetTracker(bb=af_bb)
        af_bt.init_session(ceiling=5_000_000)
        af_bt.record_spend("af-agent-1", "executor", 999_999, 999_999)

        # Patch for_project to return a ProjectPaths pointing at our fake dir
        fake_paths = ProjectPaths(
            name="fake-project",
            state_dir=tmp_path / "fake-project-state",
            stats_db=tmp_path / "fake-project-state" / "stats.duckdb",
            state_db=tmp_path / "fake-project-state" / "state.db",
            audit_log=tmp_path / "fake-project-state" / "audit.jsonl",
            repo=None,
        )
        monkeypatch.setattr("backend.team_status.sys.path", sys.path)

        # We need to patch _fp inside _budget_summary's local import scope.
        # The easiest way is to patch state_paths.for_project via monkeypatch.
        import backend.state_paths as sp_mod
        monkeypatch.setattr(sp_mod, "for_project", lambda name: fake_paths)

        # Patch BudgetTracker and Blackboard inside team_status's local import scope
        import sys as _sys
        fake_budget_mod = types.ModuleType("budget")
        # BudgetTracker(bb=...) should use our project_bb when called with bb
        def _make_tracker(bb=None):
            return BudgetTracker(bb=bb)
        fake_budget_mod.BudgetTracker = _make_tracker  # type: ignore[attr-defined]
        fake_blackboard_mod = types.ModuleType("blackboard")
        fake_blackboard_mod.Blackboard = Blackboard  # type: ignore[attr-defined]
        orig_budget = _sys.modules.get("budget")
        orig_blackboard = _sys.modules.get("blackboard")
        _sys.modules["budget"] = fake_budget_mod
        _sys.modules["blackboard"] = fake_blackboard_mod
        try:
            result = ts_mod._budget_summary(project="fake-project")
        finally:
            if orig_budget is None:
                _sys.modules.pop("budget", None)
            else:
                _sys.modules["budget"] = orig_budget
            if orig_blackboard is None:
                _sys.modules.pop("blackboard", None)
            else:
                _sys.modules["blackboard"] = orig_blackboard

        # Should read from project blackboard (60_000) not AF's (1_999_998)
        assert result.get("spent") == 60_000, (
            f"expected project spent=60000, got {result.get('spent')} "
            "(project scoping failed — reading from wrong blackboard)"
        )

    def test_rpc_handler_passes_project_to_gather(self) -> None:
        """team_status.snapshot RPC handler forwards the project param to _gather."""
        import server

        handler = server._RPC_METHODS.get("team_status.snapshot")
        assert handler is not None, "team_status.snapshot must be registered"

        calls: list[dict] = []

        def _fake_gather(snapshot, stale_msg, project=None):
            calls.append({"project": project})
            return {"budget": {}, "discussions": {}, "prs": {}, "agents": {},
                    "kpi": {}, "recent_merges": [], "errors": [],
                    "queue": {"depth": 0, "pending": []}, "snapshot_age_seconds": None}

        import backend.team_status as ts_mod
        original_gather = ts_mod._gather
        original_load = ts_mod._load_snapshot
        ts_mod._gather = _fake_gather  # type: ignore[attr-defined]
        ts_mod._load_snapshot = lambda: (None, None)  # type: ignore[attr-defined]
        try:
            handler({"project": "projectb"})
        finally:
            ts_mod._gather = original_gather
            ts_mod._load_snapshot = original_load

        assert len(calls) == 1
        assert calls[0]["project"] == "projectb", (
            f"expected project='projectb' forwarded to _gather, got {calls[0]['project']!r}"
        )
