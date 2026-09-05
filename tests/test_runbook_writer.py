"""
Tests for the runbook-writer role — control plane gate, spawn template, runbook
structure, and wiki/runbooks directory contents.

Acceptance criteria verified:
  AC1: wiki/runbooks/_template.md exists with 5 sections in order
  AC2: wiki/runbooks/README.md lists all current runbooks
  AC3: Three seed runbooks exist and each fills all 5 template sections
  AC4: .claude/agents/runbook-writer.md exists and parses
  AC6: runbook-writer AGENT_OUTPUT verdict is done or skip
  Gate: gates.runbook_writer defaults to true
  Render: spawn_prompt.py runbook-writer renders without unresolved placeholders
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

RUNBOOK_SECTIONS = ["Symptoms", "Dashboards", "Common causes", "Rollback", "Escalation"]

# The runbook *content* lives under wiki/, which is not present in every tree
# that runs this suite. The agent-card, gate and spawn-template tests further
# down don't touch wiki/ and must keep running, so this is scoped to the eight
# tests that read those files rather than applied to the module.
_NO_RUNBOOKS = pytest.mark.skipif(
    not (REPO_ROOT / "wiki" / "runbooks").is_dir(),
    reason="wiki/runbooks/ not present in this tree",
)
SEED_RUNBOOKS = ["backend-server.md", "backend-api.md", "cron-loop-trigger.md"]


# ---------------------------------------------------------------------------
# AC1: _template.md exists with 5 sections in correct order
# ---------------------------------------------------------------------------

@_NO_RUNBOOKS
def test_runbook_template_exists():
    """wiki/runbooks/_template.md must exist."""
    tmpl = REPO_ROOT / "wiki" / "runbooks" / "_template.md"
    assert tmpl.exists(), f"Missing: {tmpl}"


@_NO_RUNBOOKS
def test_runbook_template_has_five_sections_in_order():
    """_template.md must contain all 5 sections in Symptoms->Dashboards->Common causes->Rollback->Escalation order."""
    tmpl = REPO_ROOT / "wiki" / "runbooks" / "_template.md"
    content = tmpl.read_text(encoding="utf-8")
    positions = []
    for section in RUNBOOK_SECTIONS:
        pos = content.find(section)
        assert pos != -1, f"_template.md missing section: {section!r}"
        positions.append(pos)
    assert positions == sorted(positions), (
        f"Sections are out of order in _template.md. Expected: {RUNBOOK_SECTIONS}"
    )


# ---------------------------------------------------------------------------
# AC2: README.md lists all current runbooks
# ---------------------------------------------------------------------------

@_NO_RUNBOOKS
def test_runbook_readme_exists():
    """wiki/runbooks/README.md must exist."""
    readme = REPO_ROOT / "wiki" / "runbooks" / "README.md"
    assert readme.exists(), f"Missing: {readme}"


@_NO_RUNBOOKS
def test_runbook_readme_lists_seed_runbooks():
    """README.md must reference all 3 seed runbooks."""
    readme = REPO_ROOT / "wiki" / "runbooks" / "README.md"
    content = readme.read_text(encoding="utf-8")
    for rb in SEED_RUNBOOKS:
        assert rb in content, f"README.md missing reference to {rb!r}"


# ---------------------------------------------------------------------------
# AC3: Three seed runbooks exist and fill all 5 sections
# ---------------------------------------------------------------------------

@_NO_RUNBOOKS
@pytest.mark.parametrize("filename", SEED_RUNBOOKS)
def test_seed_runbook_exists(filename):
    """Each seed runbook must exist in wiki/runbooks/."""
    rb = REPO_ROOT / "wiki" / "runbooks" / filename
    assert rb.exists(), f"Missing seed runbook: {rb}"


@_NO_RUNBOOKS
@pytest.mark.parametrize("filename", SEED_RUNBOOKS)
def test_seed_runbook_has_all_five_sections(filename):
    """Each seed runbook must contain all 5 template sections."""
    rb = REPO_ROOT / "wiki" / "runbooks" / filename
    content = rb.read_text(encoding="utf-8")
    for section in RUNBOOK_SECTIONS:
        assert section in content, (
            f"{filename} missing section: {section!r}"
        )


@_NO_RUNBOOKS
@pytest.mark.parametrize("filename", SEED_RUNBOOKS)
def test_seed_runbook_sections_in_order(filename):
    """Each seed runbook must have sections in the required order."""
    rb = REPO_ROOT / "wiki" / "runbooks" / filename
    content = rb.read_text(encoding="utf-8")
    positions = [content.find(s) for s in RUNBOOK_SECTIONS]
    assert positions == sorted(positions), (
        f"{filename}: sections out of order. Expected: {RUNBOOK_SECTIONS}"
    )


@_NO_RUNBOOKS
@pytest.mark.parametrize("filename", SEED_RUNBOOKS)
def test_seed_runbook_not_empty(filename):
    """Each seed runbook must be at least 30 lines (not a stub)."""
    rb = REPO_ROOT / "wiki" / "runbooks" / filename
    lines = rb.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 30, (
        f"{filename} is only {len(lines)} lines — expected at least 30 for real content"
    )


# ---------------------------------------------------------------------------
# AC4: .claude/agents/runbook-writer.md exists and parses
# ---------------------------------------------------------------------------

def test_runbook_writer_agent_md_exists():
    """runbook-writer.md must exist in .claude/agents/."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "runbook-writer.md"
    assert agent_file.exists(), f"Missing: {agent_file}"


def test_runbook_writer_agent_md_has_frontmatter():
    """runbook-writer.md must start with a valid YAML frontmatter block."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "runbook-writer.md"
    content = agent_file.read_text(encoding="utf-8")
    assert content.startswith("---"), "agent file must start with '---' frontmatter"
    assert "name: runbook-writer" in content, "frontmatter must include 'name: runbook-writer'"
    assert "description:" in content, "frontmatter must include 'description:'"


def test_runbook_writer_agent_md_has_output_envelope():
    """runbook-writer.md must include AGENT_OUTPUT section with correct verdicts."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "runbook-writer.md"
    content = agent_file.read_text(encoding="utf-8")
    assert "AGENT_OUTPUT" in content, "agent file must reference AGENT_OUTPUT"
    assert "done" in content, "verdict 'done' must be defined"
    assert "skip" in content, "verdict 'skip' must be defined"
    assert "fail" in content, "verdict 'fail' must be defined"


# ---------------------------------------------------------------------------
# Gate: gates.runbook_writer defaults to True
# ---------------------------------------------------------------------------

def test_runbook_writer_gate_default_is_true():
    """gates.runbook_writer must default to True in _DEFAULT_GATES."""
    from backend.control_plane import _DEFAULT_GATES
    assert _DEFAULT_GATES.get("runbook_writer") is True, (
        "gates.runbook_writer must default to True in _DEFAULT_GATES"
    )


def test_runbook_writer_gate_readable_via_cli():
    """python3 backend/control_plane.py get gates.runbook_writer returns 'true'."""
    result = subprocess.run(
        [sys.executable, "backend/control_plane.py", "get", "gates.runbook_writer"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert result.stdout.strip().lower() in ("true", '"true"'), (
        f"Expected 'true', got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Render: spawn_prompt.py renders without unresolved placeholders
# ---------------------------------------------------------------------------

def test_spawn_prompt_renders_runbook_writer():
    """spawn_prompt.py runbook-writer --discussion 1 --pr 1 must produce non-empty output."""
    result = subprocess.run(
        [
            sys.executable,
            "backend/spawn_prompt.py",
            "runbook-writer",
            "--discussion", "1",
            "--pr", "1",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"spawn_prompt.py exited {result.returncode}. stderr: {result.stderr}"
    )
    assert len(result.stdout.strip()) > 100, (
        "Rendered prompt is suspiciously short — template may be empty or broken"
    )
    from backend._repo import REPO

    # Checks against the live resolved repo slug rather than a hard-coded
    # literal (D#1870 — this assertion previously expected the pre-rename
    # "autonomous-forever" slug after the resolver was fixed).
    assert REPO in result.stdout, (
        "Rendered prompt must contain repo scope constraint"
    )
    assert "runbook_writer" in result.stdout, (
        "Rendered prompt must reference the runbook_writer gate"
    )


def test_spawn_templates_knows_runbook_writer_role():
    """spawn_templates.KNOWN_ROLES must include 'runbook-writer'."""
    from backend.spawn_templates import KNOWN_ROLES
    assert "runbook-writer" in KNOWN_ROLES


def test_runbook_writer_tmpl_exists():
    """backend/spawn_templates/runbook-writer.tmpl must exist."""
    tmpl = REPO_ROOT / "backend" / "spawn_templates" / "runbook-writer.tmpl"
    assert tmpl.exists(), f"Missing template: {tmpl}"
    assert tmpl.stat().st_size > 100, "Template file is suspiciously empty"


def test_rendered_prompt_has_no_unresolved_placeholders():
    """Rendered prompt (with defaults) must not contain {{ }} placeholders (except gate_context)."""
    result = subprocess.run(
        [
            sys.executable,
            "backend/spawn_prompt.py",
            "runbook-writer",
            "--discussion", "553",
            "--pr", "42",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"Render failed: {result.stderr}"
    import re
    # Find any remaining {{...}} placeholders that are NOT gate_context
    placeholders = re.findall(r'\{\{(\w+)\}\}', result.stdout)
    remaining = [p for p in placeholders if p != "gate_context"]
    assert not remaining, (
        f"Unresolved placeholders in rendered prompt: {remaining}"
    )


# ---------------------------------------------------------------------------
# Persona JSON is valid per schema
# ---------------------------------------------------------------------------

def test_runbook_writer_persona_json_exists():
    """runbook-writer.json must exist in .autonomous-team/personas/."""
    persona_file = REPO_ROOT / ".autonomous-team" / "personas" / "runbook-writer.json"
    assert persona_file.exists(), f"Missing: {persona_file}"


def test_runbook_writer_persona_json_valid():
    """runbook-writer.json must be valid JSON and match the persona schema fields."""
    persona_file = REPO_ROOT / ".autonomous-team" / "personas" / "runbook-writer.json"
    persona = json.loads(persona_file.read_text(encoding="utf-8"))

    required_fields = ["name", "big_five", "values", "style", "conflict_pattern", "sign_off"]
    for field in required_fields:
        assert field in persona, f"persona missing required field: {field!r}"

    big_five_keys = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]
    for key in big_five_keys:
        assert key in persona["big_five"], f"big_five missing key: {key!r}"
        val = persona["big_five"][key]
        assert isinstance(val, int), f"big_five.{key} must be int, got {type(val)}"
        assert 0 <= val <= 100, f"big_five.{key} must be 0-100, got {val}"

    assert isinstance(persona["values"], list) and len(persona["values"]) >= 1
    assert isinstance(persona["style"], str) and len(persona["style"]) > 0
    assert isinstance(persona["conflict_pattern"], str) and len(persona["conflict_pattern"]) > 0

    # Name from spec: Sable
    assert persona["name"] == "Sable", f"Expected name 'Sable', got {persona['name']!r}"


# ---------------------------------------------------------------------------
# AC6: AGENT_OUTPUT envelope has correct verdict values
# ---------------------------------------------------------------------------

def test_runbook_writer_envelope_has_correct_verdicts():
    """spawn_templates._ENVELOPE_BY_ROLE['runbook-writer'] must include done/skip/fail verdicts."""
    from backend.spawn_templates import _ENVELOPE_BY_ROLE
    assert "runbook-writer" in _ENVELOPE_BY_ROLE, (
        "runbook-writer missing from _ENVELOPE_BY_ROLE"
    )
    envelope = _ENVELOPE_BY_ROLE["runbook-writer"]
    assert "done" in envelope, "envelope must reference verdict 'done'"
    assert "skip" in envelope, "envelope must reference verdict 'skip'"
    assert "fail" in envelope, "envelope must reference verdict 'fail'"
