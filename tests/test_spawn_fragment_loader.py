"""Tests for the fragment loader, compute_manifest, and spawn-agent.sh reorder.

Gate 1 — Spec-vs-Fixture (synthetic):
  - Fragment load/expand works for all 6 fragments
  - Missing fragment raises ValueError (not silent empty)
  - executor + code-reviewer render successfully with fragment includes
  - compute_manifest returns non-empty manifest and fragments dicts
  - PARTS order in spawn-agent.sh: stable prefix before volatile suffix
  - Section labels (## BRIEF, ## ROLE_BODY, etc.) present in migrated templates
"""

import json
import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRAGMENTS_DIR = REPO_ROOT / "backend" / "spawn_templates" / "fragments"
TEMPLATES_DIR = REPO_ROOT / "backend" / "spawn_templates"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_role(role: str) -> str:
    """Render a role with ignore_unknown=True and return the body."""
    sys.path.insert(0, str(REPO_ROOT))
    from backend.spawn_templates import render_body
    return render_body(role, {}, ignore_unknown=True)


# ---------------------------------------------------------------------------
# Fragment file existence
# ---------------------------------------------------------------------------

EXPECTED_FRAGMENTS = [
    "bash-discipline",
    "two-gate-protocol",
    "rate-limit-policy",
    "archive-protocol",
    "repo-scope",
    "agent-output-envelope",
]


@pytest.mark.parametrize("name", EXPECTED_FRAGMENTS)
def test_fragment_file_exists(name):
    frag = FRAGMENTS_DIR / f"{name}.md"
    assert frag.exists(), f"Fragment file missing: {frag}"


@pytest.mark.parametrize("name", EXPECTED_FRAGMENTS)
def test_fragment_file_non_empty(name):
    frag = FRAGMENTS_DIR / f"{name}.md"
    content = frag.read_text(encoding="utf-8").strip()
    assert content, f"Fragment file is empty: {frag}"


# ---------------------------------------------------------------------------
# Fragment loader: _expand_includes
# ---------------------------------------------------------------------------


def test_expand_includes_replaces_directive():
    """{{include:bash-discipline}} should be replaced with the file content."""
    from backend.spawn_templates import _expand_includes
    template = "before\n{{include:bash-discipline}}\nafter"
    expanded, names = _expand_includes(template)
    assert "{{include:bash-discipline}}" not in expanded
    assert "bash-discipline" in names
    # Content from the fragment should be present
    frag_content = (FRAGMENTS_DIR / "bash-discipline.md").read_text(encoding="utf-8").strip()
    # At least the section heading should be present
    assert "BASH_DISCIPLINE" in expanded or "No-sleep" in expanded


def test_expand_includes_missing_fragment_raises():
    """{{include:nonexistent-fragment}} must raise ValueError, not silently empty."""
    from backend.spawn_templates import _expand_includes
    template = "before\n{{include:nonexistent-frag-99999}}\nafter"
    with pytest.raises(ValueError, match="Missing fragment"):
        _expand_includes(template)


def test_expand_includes_no_directives_unchanged():
    """A template with no {{include:...}} returns the same text."""
    from backend.spawn_templates import _expand_includes
    template = "hello {{world}} there"
    expanded, names = _expand_includes(template)
    assert expanded == template
    assert names == []


def test_expand_includes_multiple_fragments():
    """Multiple {{include:...}} directives in one template all expand."""
    from backend.spawn_templates import _expand_includes
    template = "{{include:bash-discipline}}\n\n{{include:rate-limit-policy}}"
    expanded, names = _expand_includes(template)
    assert "bash-discipline" in names
    assert "rate-limit-policy" in names
    assert "{{include:" not in expanded


# ---------------------------------------------------------------------------
# compute_manifest
# ---------------------------------------------------------------------------


def test_compute_manifest_structure():
    """compute_manifest returns a dict with 'manifest' and 'fragments' keys."""
    from backend.spawn_templates import compute_manifest
    manifest = compute_manifest("executor", ["bash-discipline", "rate-limit-policy"])
    assert "manifest" in manifest
    assert "fragments" in manifest
    assert manifest["manifest"].startswith("executor.tmpl@")
    assert "bash-discipline" in manifest["fragments"]
    assert "rate-limit-policy" in manifest["fragments"]


def test_compute_manifest_non_empty_shas():
    """Fragment SHAs must be non-empty strings (not 'missing' for existing fragments)."""
    from backend.spawn_templates import compute_manifest
    manifest = compute_manifest("executor", EXPECTED_FRAGMENTS)
    for name in EXPECTED_FRAGMENTS:
        sha = manifest["fragments"].get(name, "")
        assert sha and sha != "missing", (
            f"Fragment '{name}' has missing SHA in manifest"
        )


def test_compute_manifest_empty_fragment_list():
    """compute_manifest with empty fragment list returns a valid manifest with empty fragments."""
    from backend.spawn_templates import compute_manifest
    manifest = compute_manifest("executor", [])
    assert manifest["manifest"].startswith("executor.tmpl@")
    assert manifest["fragments"] == {}


# ---------------------------------------------------------------------------
# render_body with return_manifest
# ---------------------------------------------------------------------------


def test_render_body_return_manifest_tuple():
    """render_body with return_manifest=True returns (str, dict)."""
    from backend.spawn_templates import render_body
    result = render_body("executor", {}, ignore_unknown=True, return_manifest=True)
    assert isinstance(result, tuple)
    body, manifest = result
    assert isinstance(body, str)
    assert isinstance(manifest, dict)
    assert "manifest" in manifest
    assert "fragments" in manifest


def test_render_body_without_manifest_returns_str():
    """render_body without return_manifest returns a plain string."""
    from backend.spawn_templates import render_body
    result = render_body("executor", {}, ignore_unknown=True)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# executor.tmpl — section labels
# ---------------------------------------------------------------------------


def test_executor_tmpl_has_brief_section():
    """executor.tmpl must contain a ## BRIEF section label."""
    tmpl_text = (TEMPLATES_DIR / "executor.tmpl").read_text(encoding="utf-8")
    assert "## BRIEF" in tmpl_text, "executor.tmpl missing ## BRIEF section label"


def test_executor_tmpl_has_role_body_section():
    """executor.tmpl must contain a ## ROLE_BODY section label."""
    tmpl_text = (TEMPLATES_DIR / "executor.tmpl").read_text(encoding="utf-8")
    assert "## ROLE_BODY" in tmpl_text, "executor.tmpl missing ## ROLE_BODY section label"


def test_executor_tmpl_has_gates_section():
    """executor.tmpl must contain a ## GATES section label."""
    tmpl_text = (TEMPLATES_DIR / "executor.tmpl").read_text(encoding="utf-8")
    assert "## GATES" in tmpl_text, "executor.tmpl missing ## GATES section label"


def test_executor_tmpl_uses_fragment_includes():
    """executor.tmpl must use at least one {{include:...}} directive."""
    tmpl_text = (TEMPLATES_DIR / "executor.tmpl").read_text(encoding="utf-8")
    assert "{{include:" in tmpl_text, "executor.tmpl does not use any {{include:...}} directives"


def test_code_reviewer_tmpl_has_section_labels():
    """code-reviewer.tmpl must contain BRIEF and ROLE_BODY section labels."""
    tmpl_text = (TEMPLATES_DIR / "code-reviewer.tmpl").read_text(encoding="utf-8")
    assert "## BRIEF" in tmpl_text, "code-reviewer.tmpl missing ## BRIEF label"
    assert "## ROLE_BODY" in tmpl_text, "code-reviewer.tmpl missing ## ROLE_BODY label"


def test_code_reviewer_tmpl_uses_fragment_includes():
    """code-reviewer.tmpl must use at least one {{include:...}} directive."""
    tmpl_text = (TEMPLATES_DIR / "code-reviewer.tmpl").read_text(encoding="utf-8")
    assert "{{include:" in tmpl_text, "code-reviewer.tmpl does not use any {{include:...}} directives"


# ---------------------------------------------------------------------------
# render succeeds for migrated templates (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["executor", "code-reviewer"])
def test_migrated_template_renders_without_error(role):
    """executor and code-reviewer render without ValueError (fragment expansion OK)."""
    from backend.spawn_templates import render_body
    result = render_body(role, {}, ignore_unknown=True)
    assert result, f"render_body returned empty output for role '{role}'"


@pytest.mark.parametrize("role", ["executor", "code-reviewer"])
def test_migrated_template_full_render(role):
    """Full render() for executor and code-reviewer succeeds."""
    from backend.spawn_templates import _REPO, render
    result = render(role, {}, ignore_unknown=True)
    assert result
    assert "AGENT_OUTPUT" in result
    # Checks against the module's own resolved repo slug rather than a
    # hard-coded literal (D#1870 — this assertion previously expected the
    # pre-rename "autonomous-forever" slug after the resolver was fixed).
    assert _REPO in result


@pytest.mark.parametrize("role", ["executor", "code-reviewer"])
def test_migrated_template_bash_discipline_present(role):
    """Bash discipline content should be present after fragment expansion."""
    body = _render_role(role)
    # The fragment content should be expanded
    assert "No-sleep rate-limit policy" in body or "BASH_DISCIPLINE" in body, (
        f"bash-discipline fragment content not found in rendered {role} template"
    )


# ---------------------------------------------------------------------------
# cache-boundary marker in templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["executor", "code-reviewer"])
def test_cache_boundary_marker_in_template(role):
    """Templates must contain a cache_control: ephemeral comment as volatile boundary."""
    tmpl_text = (TEMPLATES_DIR / f"{role}.tmpl").read_text(encoding="utf-8")
    assert "cache_control: ephemeral" in tmpl_text, (
        f"{role}.tmpl missing cache_control: ephemeral marker"
    )


# ---------------------------------------------------------------------------
# Prompt assembly order: stable prefix before volatile suffix
# (these invariants are now enforced in prompt_builder.py, not spawn-agent.sh)
# ---------------------------------------------------------------------------


def test_spawn_agent_parts_order():
    """Rendered prompt must place stable template body before VOLATILE_BOUNDARY,
    and volatile content (task_prompt) after it.

    The PARTS assembly was refactored from spawn-agent.sh into prompt_builder.py.
    We verify the invariant via a rendered prompt rather than the shell script.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from backend.prompt_builder import SpawnPrompt

    prompt = SpawnPrompt(
        role="executor",
        task_prompt="test task prompt content",
        hook_event_id="executor-1-1234567890",
        _template_body_override="STABLE_TEMPLATE_BODY_CONTENT",
        _checklist_block_override="",
    ).render()

    volatile_pos = prompt.find("VOLATILE_BOUNDARY")
    template_pos = prompt.find("STABLE_TEMPLATE_BODY_CONTENT")
    task_pos = prompt.find("test task prompt content")

    assert volatile_pos != -1, "VOLATILE_BOUNDARY marker not found in rendered prompt"
    assert template_pos != -1, "Template body not found in rendered prompt"
    assert task_pos != -1, "task_prompt not found in rendered prompt"

    assert template_pos < volatile_pos, (
        "Template body must appear before VOLATILE_BOUNDARY in rendered prompt"
    )
    assert task_pos > volatile_pos, (
        "task_prompt must appear after VOLATILE_BOUNDARY in rendered prompt"
    )


def test_spawn_agent_hook_event_id_after_volatile():
    """hook_event_id must appear after the VOLATILE_BOUNDARY marker in rendered prompt.

    The PARTS assembly was refactored from spawn-agent.sh into prompt_builder.py.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from backend.prompt_builder import SpawnPrompt

    prompt = SpawnPrompt(
        role="executor",
        task_prompt="task",
        hook_event_id="executor-42-9876543210",
        _template_body_override="TEMPLATE_BODY",
        _checklist_block_override="",
    ).render()

    volatile_pos = prompt.find("VOLATILE_BOUNDARY")
    # Split so this source line never carries the tag prefix immediately
    # adjacent to a canonical-shaped id (D#1807) — the concatenated literal
    # is byte-identical to the un-split string at runtime.
    hook_event_pos = prompt.find("hook_event_" "id=executor-42-9876543210")

    assert volatile_pos != -1, "VOLATILE_BOUNDARY not found in rendered prompt"
    assert hook_event_pos != -1, "hook_event_id not found in rendered prompt"
    assert hook_event_pos > volatile_pos, (
        "hook_event_id must appear after VOLATILE_BOUNDARY in rendered prompt"
    )


def test_spawn_agent_prompt_manifest_injection():
    """prompt_manifest must appear in rendered prompt when provided.

    The manifest was moved from spawn-agent.sh PARTS to prompt_builder.py.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from backend.prompt_builder import SpawnPrompt

    manifest = {"manifest": "executor.tmpl@abc123", "bash-discipline": "frag@def456"}
    prompt = SpawnPrompt(
        role="executor",
        task_prompt="task",
        hook_event_id="executor-1-1234567890",
        prompt_manifest=manifest,
        _template_body_override="TEMPLATE_BODY",
        _checklist_block_override="",
    ).render()

    assert "prompt_manifest=" in prompt, "prompt_manifest not found in rendered prompt"
    assert "abc123" in prompt, "manifest sha not found in rendered prompt"


def test_spawn_agent_consolidated_python3_extraction():
    """spawn-agent.sh must not use old standalone python3 -c calls for persona/principles.

    After the prompt_builder refactor, persona_voice, working_principles, and
    self_observe_gate are passed as JSON fields — not extracted by inline python3 -c calls.
    """
    agent_sh = REPO_ROOT / "scripts" / "spawn-agent.sh"
    content = agent_sh.read_text(encoding="utf-8")

    # These old standalone extraction patterns should not exist any more.
    assert 'print(d.get(\'persona_voice\',\'\'))' not in content, (
        "Old standalone persona_voice python3 -c extraction still present in spawn-agent.sh"
    )
    assert 'print(d.get(\'working_principles\',\'\'))' not in content, (
        "Old standalone working_principles python3 -c extraction still present in spawn-agent.sh"
    )
    assert 'print(d.get(\'self_observe_gate\',\'\'))' not in content, (
        "Old standalone self_observe_gate python3 -c extraction still present in spawn-agent.sh"
    )


# ---------------------------------------------------------------------------
# lint-spawn-prompt.sh
# ---------------------------------------------------------------------------


def test_lint_spawn_prompt_script_exists():
    """scripts/lint-spawn-prompt.sh must exist."""
    script = REPO_ROOT / "scripts" / "lint-spawn-prompt.sh"
    assert script.exists(), "scripts/lint-spawn-prompt.sh not found"


def test_lint_spawn_prompt_executor_passes():
    """lint-spawn-prompt.sh must exit 0 for executor (BRIEF should be within caps)."""
    script = REPO_ROOT / "scripts" / "lint-spawn-prompt.sh"
    result = subprocess.run(
        ["bash", str(script), "--role", "executor"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"lint-spawn-prompt.sh failed for executor:\n{result.stdout}\n{result.stderr}"
    )


def test_lint_spawn_prompt_fails_on_long_brief():
    """lint-spawn-prompt.sh must exit 1 when BRIEF exceeds 250 lines."""
    script = REPO_ROOT / "scripts" / "lint-spawn-prompt.sh"
    long_brief = "\n".join(f"line {i}" for i in range(300))
    result = subprocess.run(
        ["bash", str(script), "--role", "executor", "--brief-text", long_brief],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, (
        f"lint-spawn-prompt.sh should fail (exit 1) for 300-line BRIEF, got {result.returncode}"
    )


# ---------------------------------------------------------------------------
# measure-cache-hit.sh
# ---------------------------------------------------------------------------


def test_measure_cache_hit_script_exists():
    """scripts/measure-cache-hit.sh must exist."""
    script = REPO_ROOT / "scripts" / "measure-cache-hit.sh"
    assert script.exists(), "scripts/measure-cache-hit.sh not found"


def test_measure_cache_hit_emits_cache_unavailable():
    """measure-cache-hit.sh emits cache_unavailable when there are no runs."""
    script = REPO_ROOT / "scripts" / "measure-cache-hit.sh"
    result = subprocess.run(
        ["bash", str(script), "--role", "executor-test-role-nonexistent"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Should exit 0 (cache_unavailable) or 2 (not enough runs) — never exit 1 (cache miss)
    assert result.returncode in (0, 2), (
        f"measure-cache-hit.sh unexpected exit {result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "cache_unavailable" in combined or "fewer than 2" in combined, (
        f"Expected cache_unavailable or 'fewer than 2' message:\n{combined}"
    )
