"""
Tests for backend/agent_cards.py — AgentCards class.
"""

import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agent_cards import AgentCards, AgentNotFoundError
from backend.workflow_runner import WorkflowNotFoundError


def _write_card(d: Path, role: str, content: dict = None) -> Path:
    if content is None:
        content = {
            "role": role,
            "description": f"Test card for {role}",
            "capabilities": [],
            "authorized_tools": [],
        }
    p = d / f"{role}.json"
    p.write_text(json.dumps(content, indent=2), encoding="utf-8")
    return p


def _write_workflow(d: Path, name: str, agents: list) -> Path:
    steps = "\n".join(
        f"  - id: step{i}\n    agent: {agent}\n    prompt_template: do something\n"
        for i, agent in enumerate(agents)
    )
    content = (
        f"name: {name}\n"
        f"description: Test workflow\n"
        f"pattern: sequence\n"
        f"steps:\n{steps}"
    )
    p = d / f"{name}.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_list_agents(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    _write_card(d, "executor")
    _write_card(d, "code-reviewer")
    ac = AgentCards(agents_dir=d)
    result = ac.list_agents()
    assert result == ["code-reviewer", "executor"]


def test_list_agents_empty_dir(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    ac = AgentCards(agents_dir=d)
    assert ac.list_agents() == []


def test_get_card_success(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    _write_card(d, "executor", {"role": "executor", "capabilities": ["git"]})
    ac = AgentCards(agents_dir=d)
    card = ac.get_card("executor")
    assert card["role"] == "executor"
    assert card["capabilities"] == ["git"]


def test_get_card_not_found(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    ac = AgentCards(agents_dir=d)
    with pytest.raises(AgentNotFoundError):
        ac.get_card("nonexistent-role")


def test_validate_workflow_all_agents_present(tmp_path):
    agents_d = tmp_path / "agents"
    agents_d.mkdir()
    _write_card(agents_d, "executor")

    workflows_d = tmp_path / "workflows"
    workflows_d.mkdir()
    _write_workflow(workflows_d, "impl-wf", ["executor"])

    # AgentCards.validate_workflow uses WorkflowRunner internally, which needs
    # the workflows_dir. We patch the runner by using a subclass that overrides _dir.
    from backend.workflow_runner import WorkflowRunner
    import backend.agent_cards as ac_module

    original_runner_class = ac_module.WorkflowRunner

    class PatchedRunner(WorkflowRunner):
        def __init__(self, *args, **kwargs):
            super().__init__(workflows_dir=workflows_d)

    ac_module.WorkflowRunner = PatchedRunner
    try:
        ac = AgentCards(agents_dir=agents_d)
        errors = ac.validate_workflow("impl-wf")
        assert errors == []
    finally:
        ac_module.WorkflowRunner = original_runner_class


def test_validate_workflow_missing_agent(tmp_path):
    agents_d = tmp_path / "agents"
    agents_d.mkdir()
    # No executor card created

    workflows_d = tmp_path / "workflows"
    workflows_d.mkdir()
    _write_workflow(workflows_d, "impl-wf", ["executor"])

    from backend.workflow_runner import WorkflowRunner
    import backend.agent_cards as ac_module

    original_runner_class = ac_module.WorkflowRunner

    class PatchedRunner(WorkflowRunner):
        def __init__(self, *args, **kwargs):
            super().__init__(workflows_dir=workflows_d)

    ac_module.WorkflowRunner = PatchedRunner
    try:
        ac = AgentCards(agents_dir=agents_d)
        errors = ac.validate_workflow("impl-wf")
        assert len(errors) > 0
        assert any("executor" in err for err in errors)
    finally:
        ac_module.WorkflowRunner = original_runner_class
