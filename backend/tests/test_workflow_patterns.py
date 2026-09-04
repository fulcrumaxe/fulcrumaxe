"""Tests for the 8 new orchestration patterns in backend.workflow_runner."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.workflow_runner import (
    DelegateDepthError,
    MissingInputError,
    ValidationError,
    WorkflowNotFoundError,
    WorkflowRunner,
    _try_evaluate_condition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(workflows_dir: Path, name: str, data: dict) -> Path:
    p = workflows_dir / f"{name}.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


def _seq_step(id_: str = "s1", agent: str = "executor") -> dict:
    return {"id": id_, "agent": agent, "prompt_template": f"Do {id_}."}


@pytest.fixture()
def wdir(tmp_path: Path) -> Path:
    d = tmp_path / "workflows"
    d.mkdir()
    return d


@pytest.fixture()
def runner(wdir: Path) -> WorkflowRunner:
    return WorkflowRunner(workflows_dir=wdir)


# ---------------------------------------------------------------------------
# parallel
# ---------------------------------------------------------------------------


class TestParallel:
    def test_valid(self, wdir, runner):
        _write(wdir, "p", {
            "name": "p", "description": "d", "pattern": "parallel",
            "steps": [_seq_step("a"), _seq_step("b")],
        })
        errors = runner.validate("p")
        assert errors == []

    def test_resolve_all_steps_have_parallel_flag(self, wdir, runner):
        _write(wdir, "p", {
            "name": "p", "description": "d", "pattern": "parallel",
            "steps": [_seq_step("a"), _seq_step("b"), _seq_step("c")],
        })
        plan = runner.resolve("p", {})
        assert len(plan) == 3
        assert all(s["parallel"] is True for s in plan)

    def test_ids_preserved(self, wdir, runner):
        _write(wdir, "p", {
            "name": "p", "description": "d", "pattern": "parallel",
            "steps": [_seq_step("alpha"), _seq_step("beta")],
        })
        plan = runner.resolve("p", {})
        assert [s["id"] for s in plan] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# compete
# ---------------------------------------------------------------------------


class TestCompete:
    def test_valid_no_winner_strategy(self, wdir, runner):
        _write(wdir, "c", {
            "name": "c", "description": "d", "pattern": "compete",
            "steps": [_seq_step("a"), _seq_step("b")],
        })
        assert runner.validate("c") == []

    def test_valid_with_winner_strategy(self, wdir, runner):
        _write(wdir, "c", {
            "name": "c", "description": "d", "pattern": "compete",
            "winner_strategy": "first_pass",
            "steps": [_seq_step("a"), _seq_step("b")],
        })
        assert runner.validate("c") == []

    def test_invalid_winner_strategy(self, wdir, runner):
        _write(wdir, "c", {
            "name": "c", "description": "d", "pattern": "compete",
            "winner_strategy": "bad-value",
            "steps": [_seq_step("a")],
        })
        errors = runner.validate("c")
        assert any("winner_strategy" in e for e in errors)

    def test_resolve_compete_flag_and_strategy(self, wdir, runner):
        _write(wdir, "c", {
            "name": "c", "description": "d", "pattern": "compete",
            "winner_strategy": "best_score",
            "steps": [_seq_step("a"), _seq_step("b")],
        })
        plan = runner.resolve("c", {})
        assert all(s["compete"] is True for s in plan)
        assert all(s["winner_strategy"] == "best_score" for s in plan)

    def test_default_winner_strategy_is_first_pass(self, wdir, runner):
        _write(wdir, "c", {
            "name": "c", "description": "d", "pattern": "compete",
            "steps": [_seq_step("a")],
        })
        plan = runner.resolve("c", {})
        assert plan[0]["winner_strategy"] == "first_pass"


# ---------------------------------------------------------------------------
# escalation
# ---------------------------------------------------------------------------


class TestEscalation:
    def test_valid(self, wdir, runner):
        _write(wdir, "e", {
            "name": "e", "description": "d", "pattern": "escalation",
            "steps": [
                {"id": "s1", "agent": "executor", "prompt_template": ".",
                 "escalation_level": 1},
                {"id": "s2", "agent": "executor", "prompt_template": ".",
                 "escalation_level": 2},
            ],
        })
        assert runner.validate("e") == []

    def test_missing_escalation_level_fails(self, wdir, runner):
        _write(wdir, "e", {
            "name": "e", "description": "d", "pattern": "escalation",
            "steps": [_seq_step("s1")],
        })
        errors = runner.validate("e")
        assert any("escalation_level" in e for e in errors)

    def test_resolve_ordered_by_level(self, wdir, runner):
        _write(wdir, "e", {
            "name": "e", "description": "d", "pattern": "escalation",
            "steps": [
                {"id": "high", "agent": "executor", "prompt_template": ".", "escalation_level": 3},
                {"id": "low",  "agent": "executor", "prompt_template": ".", "escalation_level": 1},
            ],
        })
        plan = runner.resolve("e", {})
        assert plan[0]["id"] == "low"
        assert plan[1]["id"] == "high"

    def test_resolve_first_is_not_fallback(self, wdir, runner):
        _write(wdir, "e", {
            "name": "e", "description": "d", "pattern": "escalation",
            "steps": [
                {"id": "s1", "agent": "executor", "prompt_template": ".", "escalation_level": 1},
                {"id": "s2", "agent": "executor", "prompt_template": ".", "escalation_level": 2},
            ],
        })
        plan = runner.resolve("e", {})
        assert plan[0]["fallback"] is False
        assert plan[1]["fallback"] is True


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------


class TestSupervisor:
    def _valid_supervisor_wf(self):
        return {
            "name": "s", "description": "d", "pattern": "supervisor",
            "steps": [
                {"id": "sup", "agent": "project-manager", "prompt_template": ".",
                 "role": "supervisor"},
                {"id": "w1",  "agent": "executor", "prompt_template": ".", "role": "worker"},
                {"id": "w2",  "agent": "code-reviewer", "prompt_template": ".", "role": "worker"},
            ],
        }

    def test_valid(self, wdir, runner):
        _write(wdir, "s", self._valid_supervisor_wf())
        assert runner.validate("s") == []

    def test_no_supervisor_fails(self, wdir, runner):
        wf = self._valid_supervisor_wf()
        wf["steps"] = [s for s in wf["steps"] if s.get("role") != "supervisor"]
        _write(wdir, "s", wf)
        errors = runner.validate("s")
        assert any("supervisor" in e for e in errors)

    def test_no_workers_fails(self, wdir, runner):
        wf = self._valid_supervisor_wf()
        wf["steps"] = [s for s in wf["steps"] if s.get("role") == "supervisor"]
        _write(wdir, "s", wf)
        errors = runner.validate("s")
        assert any("worker" in e for e in errors)

    def test_resolve_supervisor_gets_supervises(self, wdir, runner):
        _write(wdir, "s", self._valid_supervisor_wf())
        plan = runner.resolve("s", {})
        sup_step = next(s for s in plan if s["supervisor_role"] == "supervisor")
        assert "w1" in sup_step["supervises"]
        assert "w2" in sup_step["supervises"]

    def test_resolve_workers_get_supervised_by(self, wdir, runner):
        _write(wdir, "s", self._valid_supervisor_wf())
        plan = runner.resolve("s", {})
        workers = [s for s in plan if s["supervisor_role"] == "worker"]
        assert all(s["supervised_by"] == "sup" for s in workers)


# ---------------------------------------------------------------------------
# alongside
# ---------------------------------------------------------------------------


class TestAlongside:
    def _valid_alongside_wf(self):
        return {
            "name": "a", "description": "d", "pattern": "alongside",
            "steps": [
                {"id": "bg", "agent": "executor", "prompt_template": ".", "background": True},
                {"id": "main1", "agent": "executor", "prompt_template": "."},
                {"id": "main2", "agent": "code-reviewer", "prompt_template": "."},
            ],
        }

    def test_valid(self, wdir, runner):
        _write(wdir, "a", self._valid_alongside_wf())
        assert runner.validate("a") == []

    def test_no_background_step_fails(self, wdir, runner):
        wf = self._valid_alongside_wf()
        for s in wf["steps"]:
            s.pop("background", None)
        _write(wdir, "a", wf)
        errors = runner.validate("a")
        assert any("background" in e for e in errors)

    def test_resolve_background_flag(self, wdir, runner):
        _write(wdir, "a", self._valid_alongside_wf())
        plan = runner.resolve("a", {})
        bg = next(s for s in plan if s["id"] == "bg")
        mains = [s for s in plan if s["id"] != "bg"]
        assert bg["background"] is True
        assert all(s["background"] is False for s in mains)

    def test_resolve_main_steps_have_sequence_order(self, wdir, runner):
        _write(wdir, "a", self._valid_alongside_wf())
        plan = runner.resolve("a", {})
        mains = [s for s in plan if not s["background"]]
        assert mains[0]["sequence_order"] == 0
        assert mains[1]["sequence_order"] == 1


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------


class TestLoop:
    def test_valid(self, wdir, runner):
        _write(wdir, "l", {
            "name": "l", "description": "d", "pattern": "loop",
            "max_iterations": 3,
            "exit_condition": "review-passed",
            "steps": [_seq_step("s1"), _seq_step("s2")],
        })
        assert runner.validate("l") == []

    def test_missing_max_iterations_fails(self, wdir, runner):
        _write(wdir, "l", {
            "name": "l", "description": "d", "pattern": "loop",
            "exit_condition": "done",
            "steps": [_seq_step("s1")],
        })
        errors = runner.validate("l")
        assert any("max_iterations" in e for e in errors)

    def test_missing_exit_condition_fails(self, wdir, runner):
        _write(wdir, "l", {
            "name": "l", "description": "d", "pattern": "loop",
            "max_iterations": 5,
            "steps": [_seq_step("s1")],
        })
        errors = runner.validate("l")
        assert any("exit_condition" in e for e in errors)

    def test_zero_max_iterations_fails(self, wdir, runner):
        _write(wdir, "l", {
            "name": "l", "description": "d", "pattern": "loop",
            "max_iterations": 0,
            "exit_condition": "done",
            "steps": [_seq_step("s1")],
        })
        errors = runner.validate("l")
        assert any("max_iterations" in e for e in errors)

    def test_resolve_loop_fields(self, wdir, runner):
        _write(wdir, "l", {
            "name": "l", "description": "d", "pattern": "loop",
            "max_iterations": 7,
            "exit_condition": "code-review-passed",
            "steps": [_seq_step("s1"), _seq_step("s2")],
        })
        plan = runner.resolve("l", {})
        assert len(plan) == 2
        assert all(s["loop_group"] is True for s in plan)
        assert all(s["max_iterations"] == 7 for s in plan)
        assert all(s["exit_condition"] == "code-review-passed" for s in plan)


# ---------------------------------------------------------------------------
# conditional
# ---------------------------------------------------------------------------


class TestConditional:
    def _valid_cond_wf(self):
        return {
            "name": "c", "description": "d", "pattern": "conditional",
            "condition": "security_triggered",
            "if_steps": [_seq_step("if-step")],
            "else_steps": [_seq_step("else-step")],
        }

    def test_valid(self, wdir, runner):
        _write(wdir, "c", self._valid_cond_wf())
        assert runner.validate("c") == []

    def test_missing_condition_fails(self, wdir, runner):
        wf = self._valid_cond_wf()
        del wf["condition"]
        _write(wdir, "c", wf)
        errors = runner.validate("c")
        assert any("condition" in e for e in errors)

    def test_missing_if_steps_fails(self, wdir, runner):
        wf = self._valid_cond_wf()
        del wf["if_steps"]
        _write(wdir, "c", wf)
        errors = runner.validate("c")
        assert any("if_steps" in e for e in errors)

    def test_resolve_if_branch_when_condition_true(self, wdir, runner):
        _write(wdir, "c", self._valid_cond_wf())
        plan = runner.resolve("c", {"security_triggered": "true"})
        assert all(s["branch"] == "if" for s in plan)
        assert all(s["id"] == "if-step" for s in plan)

    def test_resolve_else_branch_when_condition_false(self, wdir, runner):
        _write(wdir, "c", self._valid_cond_wf())
        plan = runner.resolve("c", {"security_triggered": "false"})
        assert all(s["branch"] == "else" for s in plan)
        assert all(s["id"] == "else-step" for s in plan)

    def test_resolve_both_branches_when_unevaluable(self, wdir, runner):
        _write(wdir, "c", self._valid_cond_wf())
        # No context provided — condition cannot be evaluated.
        plan = runner.resolve("c", {})
        branches = {s["branch"] for s in plan}
        assert "if" in branches
        assert "else" in branches
        assert all(s.get("branch_unevaluated") is True for s in plan)

    def test_resolve_equality_condition_true(self, wdir, runner):
        wf = self._valid_cond_wf()
        wf["condition"] = "env == production"
        _write(wdir, "c", wf)
        plan = runner.resolve("c", {"env": "production"})
        assert all(s["branch"] == "if" for s in plan)

    def test_resolve_equality_condition_false(self, wdir, runner):
        wf = self._valid_cond_wf()
        wf["condition"] = "env == production"
        _write(wdir, "c", wf)
        plan = runner.resolve("c", {"env": "staging"})
        assert all(s["branch"] == "else" for s in plan)

    def test_no_else_steps_is_valid(self, wdir, runner):
        wf = self._valid_cond_wf()
        del wf["else_steps"]
        _write(wdir, "c", wf)
        assert runner.validate("c") == []


# ---------------------------------------------------------------------------
# delegate
# ---------------------------------------------------------------------------


class TestDelegate:
    def test_valid(self, wdir, runner):
        _write(wdir, "target", {
            "name": "target", "description": "d", "pattern": "sequence",
            "steps": [_seq_step("t1")],
        })
        _write(wdir, "d", {
            "name": "d", "description": "d", "pattern": "delegate",
            "steps": [{"id": "phase1", "workflow": "target"}],
        })
        assert runner.validate("d") == []

    def test_missing_workflow_field_fails(self, wdir, runner):
        _write(wdir, "d", {
            "name": "d", "description": "d", "pattern": "delegate",
            "steps": [{"id": "phase1"}],
        })
        errors = runner.validate("d")
        assert any("workflow" in e for e in errors)

    def test_resolve_delegates_to_target(self, wdir, runner):
        _write(wdir, "target", {
            "name": "target", "description": "d", "pattern": "sequence",
            "steps": [_seq_step("t1"), _seq_step("t2")],
        })
        _write(wdir, "d", {
            "name": "d", "description": "d", "pattern": "delegate",
            "steps": [{"id": "phase1", "workflow": "target"}],
        })
        plan = runner.resolve("d", {})
        assert len(plan) == 2
        assert plan[0]["id"] == "t1"
        assert plan[0]["delegated_from"] == "d"
        assert plan[0]["delegate_step_id"] == "phase1"

    def test_resolve_depth_limit_raises(self, wdir, runner):
        # Create a chain of 6 delegates (exceeds _MAX_DELEGATE_DEPTH=5).
        for i in range(6):
            name = f"wf{i}"
            if i == 5:
                _write(wdir, name, {
                    "name": name, "description": "d", "pattern": "sequence",
                    "steps": [_seq_step("leaf")],
                })
            else:
                _write(wdir, name, {
                    "name": name, "description": "d", "pattern": "delegate",
                    "steps": [{"id": f"phase{i}", "workflow": f"wf{i + 1}"}],
                })
        with pytest.raises(DelegateDepthError):
            runner.resolve("wf0", {})

    def test_resolve_nested_delegate_within_depth_limit(self, wdir, runner):
        # 3 levels — should succeed.
        _write(wdir, "leaf", {
            "name": "leaf", "description": "d", "pattern": "sequence",
            "steps": [_seq_step("leaf-step")],
        })
        _write(wdir, "mid", {
            "name": "mid", "description": "d", "pattern": "delegate",
            "steps": [{"id": "mid-phase", "workflow": "leaf"}],
        })
        _write(wdir, "top", {
            "name": "top", "description": "d", "pattern": "delegate",
            "steps": [{"id": "top-phase", "workflow": "mid"}],
        })
        plan = runner.resolve("top", {})
        assert len(plan) == 1
        assert plan[0]["id"] == "leaf-step"


# ---------------------------------------------------------------------------
# _try_evaluate_condition helper
# ---------------------------------------------------------------------------


class TestTryEvaluateCondition:
    def test_truthy_key_present_and_true(self):
        assert _try_evaluate_condition("flag", {"flag": "true"}) is True

    def test_truthy_key_present_and_false(self):
        assert _try_evaluate_condition("flag", {"flag": "false"}) is False

    def test_truthy_key_absent_returns_none(self):
        assert _try_evaluate_condition("flag", {}) is None

    def test_equality_matches(self):
        assert _try_evaluate_condition("env == production", {"env": "production"}) is True

    def test_equality_no_match(self):
        assert _try_evaluate_condition("env == production", {"env": "staging"}) is False

    def test_inequality_matches(self):
        assert _try_evaluate_condition("env != production", {"env": "staging"}) is True

    def test_inequality_no_match(self):
        assert _try_evaluate_condition("env != production", {"env": "production"}) is False

    def test_complex_expression_returns_none(self):
        assert _try_evaluate_condition("a > b", {"a": "5", "b": "3"}) is None


# ---------------------------------------------------------------------------
# Existing sequence pattern still works (regression)
# ---------------------------------------------------------------------------


class TestSequenceRegression:
    def test_sequence_still_resolves(self, wdir, runner):
        _write(wdir, "seq", {
            "name": "seq", "description": "d", "pattern": "sequence",
            "steps": [_seq_step("s1"), _seq_step("s2")],
        })
        plan = runner.resolve("seq", {})
        assert len(plan) == 2
        assert plan[0]["id"] == "s1"
        assert plan[1]["id"] == "s2"
        # No extra pattern fields on sequence steps.
        assert "parallel" not in plan[0]
        assert "compete" not in plan[0]

    def test_sequence_validates(self, wdir, runner):
        _write(wdir, "seq", {
            "name": "seq", "description": "d", "pattern": "sequence",
            "steps": [_seq_step("s1")],
        })
        assert runner.validate("seq") == []


# ---------------------------------------------------------------------------
# _SUPPORTED_PATTERNS completeness
# ---------------------------------------------------------------------------


def test_all_nine_patterns_supported():
    from backend.workflow_runner import _SUPPORTED_PATTERNS
    expected = {
        "sequence", "parallel", "compete", "escalation",
        "supervisor", "alongside", "loop", "conditional", "delegate",
    }
    assert _SUPPORTED_PATTERNS == expected
