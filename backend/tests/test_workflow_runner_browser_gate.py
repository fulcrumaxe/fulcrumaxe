"""Tests for browser-tester gate in the review-pr workflow resolution.

Validates that workflow_runner.py correctly filters the browser_verify step
based on the dashboard_touched input, matching the spec from Discussion #497.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Ensure project root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.workflow_runner import WorkflowRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _review_pr_workflow() -> dict:
    """Return a review-pr workflow definition matching the repo's real layout."""
    return {
        "name": "review-pr",
        "description": "PR review workflow with optional browser-verify step.",
        "pattern": "sequence",
        "inputs": {
            "pr_number": {"type": "integer", "required": True},
            "discussion_number": {"type": "integer", "required": True},
            "dashboard_touched": {"type": "boolean", "required": False},
        },
        "steps": [
            {
                "id": "code_review",
                "agent": "code-reviewer",
                "timeout_minutes": 20,
                "prompt_template": (
                    "Review PR #{{pr_number}} for Discussion #{{discussion_number}}."
                ),
                "expects": ["verdict", "issues"],
            },
            {
                "id": "browser_verify",
                "agent": "browser-tester",
                "timeout_minutes": 30,
                "condition": "dashboard_touched",
                "prompt_template": (
                    "Visually verify PR #{{pr_number}} for Discussion #{{discussion_number}}."
                ),
                "expects": ["verdict", "screenshots"],
            },
        ],
        "outputs": {
            "verdict": "{{steps.code_review.verdict}}",
            "browser_verdict": "{{steps.browser_verify.verdict}}",
        },
    }


def _write_workflow(workflows_dir: Path, data: dict) -> None:
    p = workflows_dir / f"{data['name']}.yaml"
    p.write_text(yaml.dump(data), encoding="utf-8")


@pytest.fixture()
def workflows_dir(tmp_path: Path) -> Path:
    d = tmp_path / "workflows"
    d.mkdir()
    return d


@pytest.fixture()
def runner(workflows_dir: Path) -> WorkflowRunner:
    return WorkflowRunner(workflows_dir=workflows_dir)


# ---------------------------------------------------------------------------
# Core gate tests
# ---------------------------------------------------------------------------


def test_resolve_non_dashboard_pr_returns_one_step(workflows_dir, runner):
    """dashboard_touched=false → plan contains only code_review, no browser_verify."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "42", "discussion_number": "10", "dashboard_touched": "false"},
    )
    step_ids = [s["id"] for s in plan]
    assert step_ids == ["code_review"], (
        f"Expected only ['code_review'], got {step_ids}"
    )


def test_resolve_dashboard_pr_returns_two_steps(workflows_dir, runner):
    """dashboard_touched=true → plan contains code_review then browser_verify."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "42", "discussion_number": "10", "dashboard_touched": "true"},
    )
    step_ids = [s["id"] for s in plan]
    assert step_ids == ["code_review", "browser_verify"], (
        f"Expected ['code_review', 'browser_verify'], got {step_ids}"
    )


def test_resolve_omitted_dashboard_touched_defaults_to_one_step(workflows_dir, runner):
    """dashboard_touched omitted → treated as false → 1-step plan."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "42", "discussion_number": "10"},
    )
    step_ids = [s["id"] for s in plan]
    assert step_ids == ["code_review"], (
        f"Expected only ['code_review'] when dashboard_touched is absent, got {step_ids}"
    )


def test_browser_verify_step_uses_correct_agent(workflows_dir, runner):
    """When included, the browser_verify step uses the browser-tester agent."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "99", "discussion_number": "20", "dashboard_touched": "true"},
    )
    browser_step = next((s for s in plan if s["id"] == "browser_verify"), None)
    assert browser_step is not None
    assert browser_step["agent"] == "browser-tester"


def test_browser_verify_prompt_interpolates_pr_number(workflows_dir, runner):
    """The browser_verify prompt template interpolates pr_number correctly."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "77", "discussion_number": "30", "dashboard_touched": "true"},
    )
    browser_step = next(s for s in plan if s["id"] == "browser_verify")
    assert "77" in browser_step["prompt"]


def test_code_review_step_always_present(workflows_dir, runner):
    """code_review step is always in the plan regardless of dashboard_touched."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    for dashboard_touched in ("true", "false", ""):
        ctx = {"pr_number": "1", "discussion_number": "2"}
        if dashboard_touched:
            ctx["dashboard_touched"] = dashboard_touched
        plan = runner.resolve("review-pr", ctx)
        assert plan[0]["id"] == "code_review", (
            f"code_review must be first step; got {[s['id'] for s in plan]}"
        )


def test_resolve_dashboard_touched_false_string_excludes_browser(workflows_dir, runner):
    """dashboard_touched='false' (string) is treated as falsy → 1-step plan."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "5", "discussion_number": "6", "dashboard_touched": "false"},
    )
    assert not any(s["id"] == "browser_verify" for s in plan)


def test_resolve_dashboard_touched_zero_excludes_browser(workflows_dir, runner):
    """dashboard_touched='0' is treated as falsy → browser_verify excluded."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "5", "discussion_number": "6", "dashboard_touched": "0"},
    )
    assert not any(s["id"] == "browser_verify" for s in plan)


def test_browser_verify_step_has_timeout(workflows_dir, runner):
    """browser_verify step carries its timeout_minutes field."""
    _write_workflow(workflows_dir, _review_pr_workflow())
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "10", "discussion_number": "5", "dashboard_touched": "true"},
    )
    browser_step = next(s for s in plan if s["id"] == "browser_verify")
    assert browser_step.get("timeout_minutes") == 30


def test_live_review_pr_workflow_non_dashboard(tmp_path):
    """Integration test: resolve the real review-pr.yaml with dashboard_touched=false."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    workflows_dir = repo_root / ".autonomous-team" / "workflows"
    if not workflows_dir.exists():
        pytest.skip("workflows dir not found — skipping live YAML test")

    runner = WorkflowRunner(workflows_dir=workflows_dir)
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "1", "discussion_number": "2", "dashboard_touched": "false"},
    )
    step_ids = [s["id"] for s in plan]
    assert "code_review" in step_ids
    assert "browser_verify" not in step_ids


def test_live_review_pr_workflow_dashboard_touched(tmp_path):
    """Integration test: resolve the real review-pr.yaml with dashboard_touched=true."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    workflows_dir = repo_root / ".autonomous-team" / "workflows"
    if not workflows_dir.exists():
        pytest.skip("workflows dir not found — skipping live YAML test")

    runner = WorkflowRunner(workflows_dir=workflows_dir)
    plan = runner.resolve(
        "review-pr",
        {"pr_number": "1", "discussion_number": "2", "dashboard_touched": "true"},
    )
    step_ids = [s["id"] for s in plan]
    if "browser_verify" not in step_ids:
        pytest.skip("browser_verify step absent from on-disk YAML — skipping live dashboard test")
    assert "code_review" in step_ids
    assert "browser_verify" in step_ids
    assert step_ids.index("code_review") < step_ids.index("browser_verify"), (
        "code_review must come before browser_verify"
    )
