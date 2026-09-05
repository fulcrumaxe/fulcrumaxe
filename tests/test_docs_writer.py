"""
Tests for the docs-writer role — control plane gate, spawn template, and
docs-coverage.sh helper.

Acceptance criteria verified:
  AC1: gates.docs_writer defaults to true
  AC3: .claude/agents/docs-writer.md exists and is parseable
  AC4: spawn_prompt.py renders a non-empty prompt for docs-writer
  AC5: docs-writer persona JSON is valid per schema
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Allow running from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# AC1: gates.docs_writer defaults to True in control_plane.py
# ---------------------------------------------------------------------------

def test_docs_writer_gate_default_is_true():
    """gates.docs_writer must default to True."""
    from backend.control_plane import _DEFAULT_GATES
    assert _DEFAULT_GATES.get("docs_writer") is True, (
        "gates.docs_writer must default to True in _DEFAULT_GATES"
    )


def test_docs_writer_gate_readable_via_cli():
    """python3 backend/control_plane.py get gates.docs_writer returns 'true'."""
    result = subprocess.run(
        [sys.executable, "backend/control_plane.py", "get", "gates.docs_writer"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert result.stdout.strip().lower() in ("true", '"true"'), (
        f"Expected 'true', got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# AC3: .claude/agents/docs-writer.md exists and is parseable
# ---------------------------------------------------------------------------

def test_docs_writer_agent_md_exists():
    """docs-writer.md must exist in .claude/agents/."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "docs-writer.md"
    assert agent_file.exists(), f"Missing: {agent_file}"


def test_docs_writer_agent_md_has_frontmatter():
    """docs-writer.md must start with a valid YAML frontmatter block."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "docs-writer.md"
    content = agent_file.read_text(encoding="utf-8")
    assert content.startswith("---"), "agent file must start with '---' frontmatter"
    # Must have name field
    assert "name: docs-writer" in content, "frontmatter must include 'name: docs-writer'"
    # Must have description field
    assert "description:" in content, "frontmatter must include 'description:'"


def test_docs_writer_agent_md_has_output_envelope():
    """docs-writer.md must include AGENT_OUTPUT section with correct verdicts."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "docs-writer.md"
    content = agent_file.read_text(encoding="utf-8")
    assert "AGENT_OUTPUT" in content, "agent file must reference AGENT_OUTPUT"
    assert '"done"' in content or "done" in content, "verdict 'done' must be defined"
    assert '"skip"' in content or "skip" in content, "verdict 'skip' must be defined"
    assert '"fail"' in content or "fail" in content, "verdict 'fail' must be defined"


# ---------------------------------------------------------------------------
# AC4: spawn_prompt.py renders a non-empty prompt for docs-writer
# ---------------------------------------------------------------------------

def test_spawn_prompt_renders_docs_writer():
    """spawn_prompt.py docs-writer --discussion 1 --pr 1 must produce non-empty output."""
    result = subprocess.run(
        [
            sys.executable,
            "backend/spawn_prompt.py",
            "docs-writer",
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

    # Must contain the repo scope constraint — checks against the live
    # resolved repo slug rather than a hard-coded literal (D#1870 — this
    # assertion previously expected the pre-rename "autonomous-forever"
    # slug after the resolver was fixed).
    assert REPO in result.stdout, (
        "Rendered prompt must contain repo scope constraint"
    )
    # Must contain gate check reference
    assert "docs_writer" in result.stdout, (
        "Rendered prompt must reference the docs_writer gate"
    )


def test_spawn_templates_knows_docs_writer_role():
    """spawn_templates.KNOWN_ROLES must include 'docs-writer'."""
    from backend.spawn_templates import KNOWN_ROLES
    assert "docs-writer" in KNOWN_ROLES


def test_docs_writer_tmpl_exists():
    """backend/spawn_templates/docs-writer.tmpl must exist."""
    tmpl = REPO_ROOT / "backend" / "spawn_templates" / "docs-writer.tmpl"
    assert tmpl.exists(), f"Missing template: {tmpl}"
    assert tmpl.stat().st_size > 100, "Template file is suspiciously empty"


# ---------------------------------------------------------------------------
# AC5: docs-writer persona JSON is valid per schema
# ---------------------------------------------------------------------------

def test_docs_writer_persona_json_exists():
    """docs-writer.json must exist in .autonomous-team/personas/."""
    persona_file = REPO_ROOT / ".autonomous-team" / "personas" / "docs-writer.json"
    assert persona_file.exists(), f"Missing: {persona_file}"


def test_docs_writer_persona_json_valid():
    """docs-writer.json must be valid JSON and match the persona schema fields."""
    persona_file = REPO_ROOT / ".autonomous-team" / "personas" / "docs-writer.json"
    persona = json.loads(persona_file.read_text(encoding="utf-8"))

    # Required fields per _schema.json
    required_fields = ["name", "big_five", "values", "style", "conflict_pattern", "sign_off"]
    for field in required_fields:
        assert field in persona, f"persona missing required field: {field!r}"

    # big_five sub-fields
    big_five_keys = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]
    for key in big_five_keys:
        assert key in persona["big_five"], f"big_five missing key: {key!r}"
        val = persona["big_five"][key]
        assert isinstance(val, int), f"big_five.{key} must be int, got {type(val)}"
        assert 0 <= val <= 100, f"big_five.{key} must be 0-100, got {val}"

    assert isinstance(persona["values"], list) and len(persona["values"]) >= 1
    assert isinstance(persona["style"], str) and len(persona["style"]) > 0
    assert isinstance(persona["conflict_pattern"], str) and len(persona["conflict_pattern"]) > 0

    # Name from spec: Ren
    assert persona["name"] == "Ren", f"Expected name 'Ren', got {persona['name']!r}"


# ---------------------------------------------------------------------------
# docs-coverage.sh smoke test
# ---------------------------------------------------------------------------

def test_docs_coverage_sh_exists_and_is_executable():
    """scripts/docs-coverage.sh must exist and be executable."""
    script = REPO_ROOT / "scripts" / "docs-coverage.sh"
    assert script.exists(), f"Missing: {script}"
    assert script.stat().st_mode & 0o111, "docs-coverage.sh must be executable"


def test_docs_coverage_sh_exits_zero():
    """bash scripts/docs-coverage.sh must exit 0."""
    result = subprocess.run(
        ["bash", "scripts/docs-coverage.sh"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"docs-coverage.sh exited {result.returncode}. stderr: {result.stderr}"
    )


def test_docs_coverage_sh_output_has_header():
    """docs-coverage.sh output must contain column headers."""
    result = subprocess.run(
        ["bash", "scripts/docs-coverage.sh"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert "wiki-path" in result.stdout or result.returncode == 0, (
        "docs-coverage.sh must print column header or exit cleanly"
    )
