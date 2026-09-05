"""
Tests for D#586 PR-b — PM spawn prompt includes Spec frontmatter instructions.

Acceptance criteria verified:
  AC1: project-manager.tmpl contains the frontmatter block format (estimated_hours,
       complexity_points with Fibonacci scale).
  AC2: spawn_templates.render("project-manager", ...) produces a prompt that includes
       the frontmatter instructions.
  AC3: The HARD RULE warning about 0% accuracy is present in the rendered prompt.
  AC4: spawn_templates.py REQUIRED_VARS comment explains why frontmatter fields are
       not render-time vars (they are runtime values written into Discussion bodies).
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TMPL_PATH = REPO_ROOT / "backend" / "spawn_templates" / "project-manager.tmpl"

# Minimum vars needed to render the project-manager template without errors.
_RENDER_VARS = {
    "discussion_number": "586",
    "discussion_title": "Test discussion",
    "discussion_url": "https://github.com/autonomous-agent-7/autonomous-forever/discussions/586",
    "task_brief": "Write a spec.",
    "project_context": "",
    "agent_memory": "",
    "gate_context": "",
    "working_principles": "",
    "self_observe_gate": "",
}


# ---------------------------------------------------------------------------
# AC1: template file contains the frontmatter block instructions
# ---------------------------------------------------------------------------

def test_tmpl_contains_estimated_hours():
    """project-manager.tmpl must reference estimated_hours."""
    content = TMPL_PATH.read_text(encoding="utf-8")
    assert "estimated_hours" in content, (
        "project-manager.tmpl must include 'estimated_hours' in its Spec instructions"
    )


def test_tmpl_contains_complexity_points():
    """project-manager.tmpl must reference complexity_points."""
    content = TMPL_PATH.read_text(encoding="utf-8")
    assert "complexity_points" in content, (
        "project-manager.tmpl must include 'complexity_points' in its Spec instructions"
    )


def test_tmpl_mentions_fibonacci_scale():
    """project-manager.tmpl must mention the Fibonacci point scale."""
    content = TMPL_PATH.read_text(encoding="utf-8")
    # The template should list: 1, 2, 3, 5, 8 (Fibonacci values)
    assert "1, 2, 3, 5" in content, (
        "project-manager.tmpl must mention the Fibonacci complexity_points scale"
    )


def test_tmpl_contains_yaml_fence():
    """project-manager.tmpl must show the --- YAML fence in its frontmatter example."""
    content = TMPL_PATH.read_text(encoding="utf-8")
    # The instructional block should show a YAML frontmatter fence
    assert content.count("---") >= 2, (
        "project-manager.tmpl must show at least one YAML frontmatter fence example"
    )


def test_tmpl_contains_range_1_to_40():
    """project-manager.tmpl must specify the 1–40 hour range for estimated_hours."""
    content = TMPL_PATH.read_text(encoding="utf-8")
    assert "40" in content, (
        "project-manager.tmpl must mention the upper bound (40) for estimated_hours"
    )


# ---------------------------------------------------------------------------
# AC2: rendered prompt includes the frontmatter instructions
# ---------------------------------------------------------------------------

def test_rendered_prompt_includes_estimated_hours():
    """Rendered PM prompt must contain the estimated_hours instruction."""
    from backend.spawn_templates import render
    prompt = render("project-manager", _RENDER_VARS)
    assert "estimated_hours" in prompt, (
        "Rendered project-manager prompt must include 'estimated_hours'"
    )


def test_rendered_prompt_includes_complexity_points():
    """Rendered PM prompt must contain the complexity_points instruction."""
    from backend.spawn_templates import render
    prompt = render("project-manager", _RENDER_VARS)
    assert "complexity_points" in prompt, (
        "Rendered project-manager prompt must include 'complexity_points'"
    )


def test_rendered_prompt_includes_spec_frontmatter_heading():
    """Rendered PM prompt must contain a heading about Spec Frontmatter."""
    from backend.spawn_templates import render
    prompt = render("project-manager", _RENDER_VARS)
    assert "Spec Frontmatter" in prompt or "frontmatter" in prompt.lower(), (
        "Rendered project-manager prompt must contain a Spec Frontmatter section"
    )


# ---------------------------------------------------------------------------
# AC3: HARD RULE warning present in rendered prompt
# ---------------------------------------------------------------------------

def test_rendered_prompt_contains_hard_rule_warning():
    """Rendered PM prompt must contain the 0% accuracy HARD RULE warning."""
    from backend.spawn_templates import render
    prompt = render("project-manager", _RENDER_VARS)
    assert "HARD RULE" in prompt, (
        "Rendered project-manager prompt must contain HARD RULE warning about frontmatter"
    )
    assert "0%" in prompt or "kpi_engine" in prompt, (
        "HARD RULE warning must reference the accuracy metric consequence"
    )


# ---------------------------------------------------------------------------
# AC4: spawn_templates.py REQUIRED_VARS has explanatory comment for project-manager
# ---------------------------------------------------------------------------

def test_spawn_templates_py_has_frontmatter_comment():
    """spawn_templates.py must have a comment explaining PM frontmatter fields."""
    spawn_templates_py = REPO_ROOT / "backend" / "spawn_templates.py"
    content = spawn_templates_py.read_text(encoding="utf-8")
    # The comment should explain why estimated_hours isn't in REQUIRED_VARS
    assert "estimated_hours" in content, (
        "spawn_templates.py must reference estimated_hours in a comment "
        "explaining the PM frontmatter runtime-vs-render-time distinction"
    )
    assert "kpi_engine" in content, (
        "spawn_templates.py comment should mention kpi_engine.py as the consumer"
    )


# ---------------------------------------------------------------------------
# Regression: render still works for all roles (no template syntax breakage)
# ---------------------------------------------------------------------------

def test_project_manager_render_does_not_error():
    """render('project-manager', ...) must succeed without ValueError."""
    from backend.spawn_templates import render
    prompt = render("project-manager", _RENDER_VARS)
    assert len(prompt) > 500, "Rendered prompt is suspiciously short"


def test_project_manager_render_contains_repo_scope():
    """Rendered PM prompt must still contain the mandatory repo scope constraint."""
    from backend.spawn_templates import render
    prompt = render("project-manager", _RENDER_VARS)
    assert "autonomous-agent-7/autonomous-forever" in prompt, (
        "Rendered prompt must include repo scope constraint"
    )
