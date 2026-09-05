"""
Tests for backend/workflow_runner.py — WorkflowRunner class.
"""

from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.workflow_runner import (
    WorkflowRunner,
    WorkflowNotFoundError,
    MissingInputError,
    ValidationError,
)


def _make_runner(workflows_dir):
    return WorkflowRunner(workflows_dir=workflows_dir)


def _write_workflow(d: Path, name: str, content: str) -> Path:
    p = d / f"{name}.yaml"
    p.write_text(content, encoding="utf-8")
    return p


VALID_WORKFLOW = (
    "name: test-workflow\n"
    "description: Test workflow\n"
    "pattern: sequence\n"
    "inputs:\n"
    "  discussion_number:\n"
    "    required: true\n"
    "    description: Discussion number\n"
    "steps:\n"
    "  - id: implement\n"
    "    agent: executor\n"
    "    prompt_template: Implement discussion {{discussion_number}}\n"
    "    expects:\n"
    "      - pr_number\n"
    "    timeout_minutes: 30\n"
)


def test_list_workflows(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    _write_workflow(d, "alpha", VALID_WORKFLOW)
    _write_workflow(d, "beta", VALID_WORKFLOW)
    runner = _make_runner(d)
    result = runner.list_workflows()
    assert result == ["alpha", "beta"]


def test_list_workflows_empty_dir(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    runner = _make_runner(d)
    assert runner.list_workflows() == []


def test_validate_valid_workflow(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    _write_workflow(d, "valid-wf", VALID_WORKFLOW)
    runner = _make_runner(d)
    errors = runner.validate("valid-wf")
    assert errors == []


def test_validate_missing_fields(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    # Minimal YAML missing name, description, pattern, steps
    _write_workflow(d, "broken", "nothing: here\n")
    runner = _make_runner(d)
    errors = runner.validate("broken")
    missing_fields = {"name", "description", "pattern", "steps"}
    for field in missing_fields:
        assert any(field in err for err in errors), f"Expected error about '{field}' in {errors}"


def test_validate_unsupported_pattern(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    content = (
        "name: unsupported-wf\n"
        "description: Parallel workflow\n"
        "pattern: totally-unknown-pattern\n"
        "steps:\n"
        "  - id: step1\n"
        "    agent: executor\n"
        "    prompt_template: do something\n"
    )
    _write_workflow(d, "unsupported-wf", content)
    runner = _make_runner(d)
    errors = runner.validate("unsupported-wf")
    assert any("Unsupported" in err or "pattern" in err.lower() for err in errors)


def test_resolve_interpolates_inputs(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    _write_workflow(d, "interp-wf", VALID_WORKFLOW)
    runner = _make_runner(d)
    steps = runner.resolve("interp-wf", {"discussion_number": 42})
    assert len(steps) == 1
    assert "42" in steps[0]["prompt"]
    assert "{{discussion_number}}" not in steps[0]["prompt"]


def test_resolve_preserves_step_refs(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    content = (
        "name: ref-wf\n"
        "description: Workflow with step refs\n"
        "pattern: sequence\n"
        "inputs:\n"
        "  discussion_number:\n"
        "    required: true\n"
        "steps:\n"
        "  - id: review\n"
        "    agent: code-reviewer\n"
        "    prompt_template: Review PR {{steps.implement.pr}} for discussion {{discussion_number}}\n"
    )
    _write_workflow(d, "ref-wf", content)
    runner = _make_runner(d)
    steps = runner.resolve("ref-wf", {"discussion_number": 7})
    prompt = steps[0]["prompt"]
    # Step ref preserved as-is, discussion_number interpolated
    assert "{{steps.implement.pr}}" in prompt
    assert "7" in prompt


def test_resolve_missing_required_input(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    _write_workflow(d, "req-wf", VALID_WORKFLOW)
    runner = _make_runner(d)
    with pytest.raises(MissingInputError):
        runner.resolve("req-wf", {})  # discussion_number is required


def test_resolve_marks_conditional_steps(tmp_path):
    """Steps with a condition field are conditional and excluded when condition is falsy/absent.

    Conditional steps are opt-in: absent context key → condition unevaluable → step excluded.
    When the condition evaluates to True, the step is included and flagged conditional=True.
    """
    d = tmp_path / "workflows"
    d.mkdir()
    content = (
        "name: cond-wf\n"
        "description: Workflow with conditional steps\n"
        "pattern: sequence\n"
        "steps:\n"
        "  - id: always\n"
        "    agent: executor\n"
        "    prompt_template: Always runs\n"
        "  - id: sometimes\n"
        "    agent: code-reviewer\n"
        "    prompt_template: Sometimes runs\n"
        "    condition: security_triggered == true\n"
    )
    _write_workflow(d, "cond-wf", content)
    runner = _make_runner(d)

    # Without the condition key: sometimes step is excluded (opt-in default)
    steps_no_ctx = runner.resolve("cond-wf", {})
    assert any(s["id"] == "always" for s in steps_no_ctx)
    assert not any(s["id"] == "sometimes" for s in steps_no_ctx)

    # With the condition key set to true: sometimes step IS included
    steps_with_ctx = runner.resolve("cond-wf", {"security_triggered": "true"})
    ids_with_ctx = [s["id"] for s in steps_with_ctx]
    assert "always" in ids_with_ctx
    assert "sometimes" in ids_with_ctx
    always = [s for s in steps_with_ctx if s["id"] == "always"][0]
    sometimes = [s for s in steps_with_ctx if s["id"] == "sometimes"][0]
    assert always["conditional"] is False
    assert sometimes["conditional"] is True
    assert sometimes["condition"] == "security_triggered == true"


def test_workflow_not_found(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    runner = _make_runner(d)
    with pytest.raises(WorkflowNotFoundError):
        runner.resolve("does-not-exist", {})
