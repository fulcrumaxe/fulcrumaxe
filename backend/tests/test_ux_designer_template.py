"""Tests for the ux-designer role: template registration, gate, and artifact format."""

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spawn_templates import render, KNOWN_ROLES  # noqa: E402
from backend.tests.test_spawn_templates import (  # noqa: E402
    GATE_KEY_BY_ROLE,
    REPO_SCOPE_PHRASE,
)

_STUB_VARS = {
    "discussion_number": "1381",
    "discussion_title": "UX Designer test",
    "discussion_url": "https://github.com/fulcrumaxe/fulcrumaxe/discussions/1381",
    "task_brief": "Design a dashboard tile",
    "project_context": "[project context stub]",
    "agent_memory": "[agent memory stub]",
    "gate_context": '{"gates": {}}',
    # Extra vars used by secondary roles (not needed for ux-designer but kept for safety)
    "pr_number": "55",
    "pr_branch": "feature/stub-branch",
    "pr_url": "https://github.com/fulcrumaxe/fulcrumaxe/pull/55",
    "trigger_type": "circuit_breaker",
    "evidence_json": "{}",
    "release_id": "v0.0.1",
}

_ROLE = "ux-designer"


def _rendered() -> str:
    return render(_ROLE, _STUB_VARS)


# ---------------------------------------------------------------------------
# Registration checks
# ---------------------------------------------------------------------------

def test_ux_designer_in_known_roles() -> None:
    """Template file auto-registers the role via glob discovery."""
    assert _ROLE in KNOWN_ROLES, f"'{_ROLE}' not found in KNOWN_ROLES"


def test_ux_designer_in_gate_key_map() -> None:
    """GATE_KEY_BY_ROLE must have an entry for ux-designer (prevents KeyError in parametrized tests)."""
    assert _ROLE in GATE_KEY_BY_ROLE, f"'{_ROLE}' missing from GATE_KEY_BY_ROLE"


def test_gate_key_present_in_rendered_prompt() -> None:
    """The gate key string must appear in the rendered template."""
    key = GATE_KEY_BY_ROLE[_ROLE]
    result = _rendered()
    assert key in result, f"Gate key '{key}' not found in rendered ux-designer prompt"


# ---------------------------------------------------------------------------
# Artifact path / format checks
# ---------------------------------------------------------------------------

def test_design_note_path_referenced() -> None:
    """Rendered prompt must reference the canonical artifact path wiki/design-notes/."""
    result = _rendered()
    assert "wiki/design-notes/" in result, (
        "Rendered prompt does not reference 'wiki/design-notes/' artifact path"
    )


def test_four_sections_referenced() -> None:
    """Rendered prompt must instruct the agent to produce all four sections."""
    result = _rendered()
    result_lower = result.lower()
    assert "pitch" in result_lower, "Section 'Pitch' not referenced in template"
    assert "wireframe" in result_lower, "Section 'Wireframe' not referenced in template"
    assert "interaction flow" in result_lower, "Section 'Interaction Flow' not referenced in template"
    assert "a11y" in result_lower or "checklist" in result_lower, (
        "Section 'A11y Checklist' not referenced in template"
    )


def test_a11y_four_dimensions_referenced() -> None:
    """A11y checklist must cover contrast, keyboard, ARIA, and focus."""
    result = _rendered().lower()
    assert "contrast" in result, "A11y dimension 'contrast' not referenced"
    assert "keyboard" in result, "A11y dimension 'keyboard' not referenced"
    assert "aria" in result, "A11y dimension 'aria' not referenced"
    assert "focus" in result, "A11y dimension 'focus' not referenced"


# ---------------------------------------------------------------------------
# Producer-only check: no value-judgment prose in the role mandate
# ---------------------------------------------------------------------------

def test_template_forbids_value_judgment() -> None:
    """Template must explicitly forbid value/should-we-build judgments (producer-only mandate)."""
    result = _rendered().lower()
    # The template must say this is product-owner's lane, not the ux-designer's
    assert "product-owner" in result or "product owner" in result, (
        "Template must explicitly state that value judgment belongs to product-owner"
    )
    # Must contain a prohibition on value claims (e.g. "no value claims", "not a value-voice")
    assert "value" in result, (
        "Template must reference 'value' in the context of forbidding value judgments"
    )


def test_role_def_references_product_owner_scope() -> None:
    """Role definition file must explicitly name product-owner as the value-judgment owner."""
    role_def = _REPO_ROOT / ".claude" / "agents" / "ux-designer.md"
    assert role_def.exists(), f"Role definition file not found: {role_def}"
    content = role_def.read_text().lower()
    assert "product-owner" in content or "product owner" in content, (
        "ux-designer.md must reference product-owner to scope out value judgment"
    )
    # Must also mention value or should-we-build explicitly
    assert "value" in content or "should-we-build" in content, (
        "ux-designer.md must mention 'value' or 'should-we-build' to clarify scope"
    )


# ---------------------------------------------------------------------------
# Mandatory appendix checks (repo scope, envelope, archive)
# ---------------------------------------------------------------------------

def test_render_contains_repo_scope() -> None:
    # Derived from the production resolver via REPO_SCOPE_PHRASE — see D#1797.
    result = _rendered()
    assert REPO_SCOPE_PHRASE in result


def test_render_contains_agent_output_marker() -> None:
    result = _rendered()
    assert "<!-- AGENT_OUTPUT -->" in result


def test_render_contains_archive_protocol() -> None:
    result = _rendered()
    assert "archive/" in result


# ---------------------------------------------------------------------------
# Read-only check: no spawn/state references in role files
# ---------------------------------------------------------------------------

def test_role_def_no_spawn_invocations() -> None:
    """Role def must not contain spawn invocations (producer-only: no sub-agent spawning)."""
    role_def = _REPO_ROOT / ".claude" / "agents" / "ux-designer.md"
    content = role_def.read_text()
    # These patterns indicate actual invocations (not prohibition prose)
    for forbidden in ("Agent()", "spawn-agent.sh"):
        assert forbidden not in content, (
            f"ux-designer.md must not invoke '{forbidden}' (producer-only role)"
        )


def test_template_no_spawn_invocations() -> None:
    """Spawn template must not contain spawn invocations."""
    tmpl = _REPO_ROOT / "backend" / "spawn_templates" / "ux-designer.tmpl"
    assert tmpl.exists(), f"Template file not found: {tmpl}"
    content = tmpl.read_text()
    for forbidden in ("Agent()", "spawn-agent.sh"):
        assert forbidden not in content, (
            f"ux-designer.tmpl must not invoke '{forbidden}' (producer-only role)"
        )
