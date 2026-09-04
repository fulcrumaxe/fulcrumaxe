"""Tests for backend.workflow_runner."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Ensure project root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.workflow_runner import (
    MissingInputError,
    ValidationError,
    WorkflowNotFoundError,
    WorkflowRunner,
    _interpolate,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_workflow(**overrides) -> dict:
    """Return a valid minimal workflow dict, with optional field overrides."""
    base = {
        "name": "test-workflow",
        "description": "A test workflow.",
        "pattern": "sequence",
        "steps": [
            {
                "id": "step1",
                "agent": "executor",
                "prompt_template": "Do the thing for {{discussion_number}}.",
            }
        ],
    }
    base.update(overrides)
    return base


def _write_workflow(workflows_dir: Path, name: str, data: dict) -> Path:
    p = workflows_dir / f"{name}.yaml"
    p.write_text(yaml.dump(data), encoding="utf-8")
    return p


@pytest.fixture()
def workflows_dir(tmp_path: Path) -> Path:
    d = tmp_path / "workflows"
    d.mkdir()
    return d


@pytest.fixture()
def runner(workflows_dir: Path) -> WorkflowRunner:
    return WorkflowRunner(workflows_dir=workflows_dir)


# ---------------------------------------------------------------------------
# list_workflows
# ---------------------------------------------------------------------------


def test_list_workflows_empty(runner):
    assert runner.list_workflows() == []


def test_list_workflows_returns_sorted_names(workflows_dir, runner):
    for name in ("zebra", "alpha", "middle"):
        _write_workflow(workflows_dir, name, _minimal_workflow(name=name))
    assert runner.list_workflows() == ["alpha", "middle", "zebra"]


def test_list_workflows_nonexistent_dir():
    r = WorkflowRunner(workflows_dir="/nonexistent/path/that/does/not/exist")
    assert r.list_workflows() == []


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_valid_workflow(workflows_dir, runner):
    _write_workflow(workflows_dir, "valid", _minimal_workflow())
    errors = runner.validate("valid")
    assert errors == []


def test_validate_missing_required_fields(workflows_dir, runner):
    # workflow missing 'description' and 'pattern'
    bad = {"name": "bad", "steps": [{"id": "s1", "agent": "executor", "prompt_template": "x"}]}
    _write_workflow(workflows_dir, "bad", bad)
    errors = runner.validate("bad")
    assert any("description" in e for e in errors)
    assert any("pattern" in e for e in errors)


def test_validate_invalid_step_type(workflows_dir, runner):
    wf = _minimal_workflow()
    wf["steps"] = "not-a-list"
    _write_workflow(workflows_dir, "bad-steps", wf)
    errors = runner.validate("bad-steps")
    assert any("list" in e for e in errors)


def test_validate_workflow_not_found(runner):
    with pytest.raises(WorkflowNotFoundError):
        runner.validate("nonexistent")


def test_validate_unsupported_pattern(workflows_dir, runner):
    wf = _minimal_workflow(pattern="totally-unknown-pattern")
    _write_workflow(workflows_dir, "bad-pattern", wf)
    errors = runner.validate("bad-pattern")
    assert any("pattern" in e.lower() or "Unsupported" in e for e in errors)


def test_validate_empty_steps(workflows_dir, runner):
    wf = _minimal_workflow()
    wf["steps"] = []
    _write_workflow(workflows_dir, "empty-steps", wf)
    errors = runner.validate("empty-steps")
    assert any("empty" in e for e in errors)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_variable_substitution(workflows_dir, runner):
    wf = _minimal_workflow(
        inputs={"discussion_number": {"required": True}},
    )
    _write_workflow(workflows_dir, "resolve-test", wf)
    steps = runner.resolve("resolve-test", {"discussion_number": "42"})
    assert len(steps) == 1
    assert "42" in steps[0]["prompt"]


def test_resolve_missing_variable_raises(workflows_dir, runner):
    wf = _minimal_workflow(
        inputs={"discussion_number": {"required": True}},
    )
    _write_workflow(workflows_dir, "missing-var", wf)
    with pytest.raises(MissingInputError) as exc_info:
        runner.resolve("missing-var", {})
    assert "discussion_number" in str(exc_info.value)


def test_resolve_workflow_not_found(runner):
    with pytest.raises(WorkflowNotFoundError):
        runner.resolve("no-such", {})


def test_resolve_invalid_workflow_raises_validation_error(workflows_dir, runner):
    bad = {"name": "bad", "steps": [{"id": "s1", "agent": "executor", "prompt_template": "x"}]}
    _write_workflow(workflows_dir, "bad-wf", bad)
    with pytest.raises(ValidationError):
        runner.resolve("bad-wf", {})


def test_resolve_step_references_preserved(workflows_dir, runner):
    wf = _minimal_workflow()
    wf["steps"][0]["prompt_template"] = "Use {{steps.step0.output}} here."
    _write_workflow(workflows_dir, "step-ref", wf)
    steps = runner.resolve("step-ref", {})
    assert "{{steps.step0.output}}" in steps[0]["prompt"]


def test_resolve_conditional_step_excluded_when_unevaluable(workflows_dir, runner):
    """A step whose condition cannot be evaluated (key absent) is excluded from the plan.

    Conditional steps are opt-in: absent context key → condition unevaluable → step excluded.
    When the condition evaluates to True, the step is included and flagged conditional=True.
    """
    wf = _minimal_workflow()
    wf["steps"][0]["condition"] = "{{some_flag}} == true"
    _write_workflow(workflows_dir, "conditional", wf)
    steps = runner.resolve("conditional", {})
    # The step is absent from the plan because condition could not be evaluated.
    assert len(steps) == 0


def test_resolve_conditional_step_included_when_truthy(workflows_dir, runner):
    """A step whose condition evaluates to True IS included in the plan."""
    wf = _minimal_workflow()
    wf["steps"][0]["condition"] = "some_flag"
    _write_workflow(workflows_dir, "conditional-true", wf)
    steps = runner.resolve("conditional-true", {"some_flag": "true"})
    assert len(steps) == 1
    assert steps[0]["conditional"] is True
    assert steps[0]["condition"] == "some_flag"


def test_resolve_non_conditional_step(workflows_dir, runner):
    wf = _minimal_workflow()
    _write_workflow(workflows_dir, "no-cond", wf)
    steps = runner.resolve("no-cond", {})
    assert steps[0]["conditional"] is False
    assert steps[0]["condition"] is None


# ---------------------------------------------------------------------------
# Error class messages
# ---------------------------------------------------------------------------


def test_workflow_not_found_error_message(runner):
    try:
        runner.validate("missing")
    except WorkflowNotFoundError as exc:
        assert "missing" in str(exc)


def test_validation_error_message(workflows_dir, runner):
    bad = {"name": "bad", "steps": [{"id": "s1", "agent": "executor", "prompt_template": "x"}]}
    _write_workflow(workflows_dir, "bad-ve", bad)
    try:
        runner.resolve("bad-ve", {})
    except ValidationError as exc:
        assert "bad-ve" in str(exc)


def test_missing_input_error_message(workflows_dir, runner):
    wf = _minimal_workflow(inputs={"myinput": {"required": True}})
    _write_workflow(workflows_dir, "mi-wf", wf)
    try:
        runner.resolve("mi-wf", {})
    except MissingInputError as exc:
        assert "myinput" in str(exc)


# ---------------------------------------------------------------------------
# _interpolate helper
# ---------------------------------------------------------------------------


def test_interpolate_basic():
    result = _interpolate("Hello {{name}}!", {"name": "world"})
    assert result == "Hello world!"


def test_interpolate_nested_vars():
    result = _interpolate("{{a}} and {{b}}", {"a": "foo", "b": "bar"})
    assert result == "foo and bar"


def test_interpolate_escaped_braces_left_intact():
    """Tokens not in context are left as-is."""
    result = _interpolate("{{unknown}}", {})
    assert result == "{{unknown}}"


def test_interpolate_step_references_untouched():
    result = _interpolate("{{steps.step1.output}}", {"steps.step1.output": "should-not-replace"})
    # step references are left as-is regardless
    assert result == "{{steps.step1.output}}"


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


def test_main_list_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # Use a custom workflows dir that is empty
    # We patch via argv and rely on main() using default dir (no workflows there)
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    # Patch WorkflowRunner to use our tmp dir
    import backend.workflow_runner as wr_mod
    original_init = wr_mod.WorkflowRunner.__init__

    def patched_init(self, workflows_dir=None):
        original_init(self, workflows_dir=str(wf_dir))

    monkeypatch.setattr(wr_mod.WorkflowRunner, "__init__", patched_init)
    rc = main(["list"])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "no workflows" in out


def test_main_list_with_workflows(tmp_path, monkeypatch, capsys):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow(wf_dir, "my-flow", _minimal_workflow())
    import backend.workflow_runner as wr_mod
    original_init = wr_mod.WorkflowRunner.__init__

    def patched_init(self, workflows_dir=None):
        original_init(self, workflows_dir=str(wf_dir))

    monkeypatch.setattr(wr_mod.WorkflowRunner, "__init__", patched_init)
    rc = main(["list"])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "my-flow" in out


def test_main_validate_valid(tmp_path, monkeypatch, capsys):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow(wf_dir, "ok-wf", _minimal_workflow())
    import backend.workflow_runner as wr_mod
    original_init = wr_mod.WorkflowRunner.__init__

    def patched_init(self, workflows_dir=None):
        original_init(self, workflows_dir=str(wf_dir))

    monkeypatch.setattr(wr_mod.WorkflowRunner, "__init__", patched_init)
    rc = main(["validate", "ok-wf"])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "valid" in out


def test_main_validate_not_found(tmp_path, monkeypatch, capsys):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    import backend.workflow_runner as wr_mod
    original_init = wr_mod.WorkflowRunner.__init__

    def patched_init(self, workflows_dir=None):
        original_init(self, workflows_dir=str(wf_dir))

    monkeypatch.setattr(wr_mod.WorkflowRunner, "__init__", patched_init)
    rc = main(["validate", "ghost"])
    _, err = capsys.readouterr()
    assert rc == 1
    assert "ghost" in err


def test_main_resolve_outputs_json(tmp_path, monkeypatch, capsys):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    wf = _minimal_workflow(inputs={"discussion_number": {"required": True}})
    _write_workflow(wf_dir, "res-wf", wf)
    import backend.workflow_runner as wr_mod
    original_init = wr_mod.WorkflowRunner.__init__

    def patched_init(self, workflows_dir=None):
        original_init(self, workflows_dir=str(wf_dir))

    monkeypatch.setattr(wr_mod.WorkflowRunner, "__init__", patched_init)
    rc = main(["resolve", "res-wf", "--input", "discussion_number=99"])
    out, _ = capsys.readouterr()
    assert rc == 0
    import json
    steps = json.loads(out)
    assert isinstance(steps, list)
    assert steps[0]["id"] == "step1"


def test_main_resolve_missing_input_exits_1(tmp_path, monkeypatch, capsys):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    wf = _minimal_workflow(inputs={"required_field": {"required": True}})
    _write_workflow(wf_dir, "req-wf", wf)
    import backend.workflow_runner as wr_mod
    original_init = wr_mod.WorkflowRunner.__init__

    def patched_init(self, workflows_dir=None):
        original_init(self, workflows_dir=str(wf_dir))

    monkeypatch.setattr(wr_mod.WorkflowRunner, "__init__", patched_init)
    rc = main(["resolve", "req-wf"])
    _, err = capsys.readouterr()
    assert rc == 1
    assert "required_field" in err
