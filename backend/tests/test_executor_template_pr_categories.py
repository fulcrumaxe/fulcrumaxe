"""Tests for pr_categories label injection in executor.tmpl.

Verifies that:
- When project.json has a non-empty pr_categories, the executor template
  instructs the agent to read it and append --label to gh pr create.
- The template contains the Python one-liner that extracts pr_categories.
- The category guide (bugfix, feature, security, etc.) is present.
- When pr_categories is empty the instruction says to skip label addition.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TMPL_PATH = _REPO_ROOT / "backend" / "spawn_templates" / "executor.tmpl"
_SNAPSHOT_TMPL_PATH = (
    _REPO_ROOT
    / "loop-bootstrap"
    / "backend-snapshot"
    / "spawn_templates"
    / "executor.tmpl"
)


def _load_template(path: Path) -> str:
    return path.read_text()


# ---------------------------------------------------------------------------
# Template content assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tmpl_path",
    [_TMPL_PATH, _SNAPSHOT_TMPL_PATH],
    ids=["backend", "loop-bootstrap-snapshot"],
)
def test_template_contains_pr_categories_python_oneliner(tmpl_path: Path) -> None:
    """The template must contain the Python snippet that reads pr_categories."""
    content = _load_template(tmpl_path)
    assert "pr_categories" in content, "Template missing pr_categories reference"
    # The one-liner must read .autonomous-team/project.json
    assert ".autonomous-team/project.json" in content


@pytest.mark.parametrize(
    "tmpl_path",
    [_TMPL_PATH, _SNAPSHOT_TMPL_PATH],
    ids=["backend", "loop-bootstrap-snapshot"],
)
def test_template_contains_label_flag_instruction(tmpl_path: Path) -> None:
    """The template must instruct the agent to append --label to gh pr create."""
    content = _load_template(tmpl_path)
    assert "--label" in content


@pytest.mark.parametrize(
    "tmpl_path",
    [_TMPL_PATH, _SNAPSHOT_TMPL_PATH],
    ids=["backend", "loop-bootstrap-snapshot"],
)
def test_template_contains_category_guide(tmpl_path: Path) -> None:
    """The template must list the category labels the agent can choose from."""
    content = _load_template(tmpl_path)
    for category in ("bugfix", "feature", "security", "breaking", "perf", "infra", "docs", "cosmetic"):
        assert category in content, f"Category '{category}' missing from template"


@pytest.mark.parametrize(
    "tmpl_path",
    [_TMPL_PATH, _SNAPSHOT_TMPL_PATH],
    ids=["backend", "loop-bootstrap-snapshot"],
)
def test_template_says_skip_when_empty(tmpl_path: Path) -> None:
    """The template must tell the agent to skip labelling when pr_categories is absent."""
    content = _load_template(tmpl_path)
    # Must mention skipping when the list is empty or absent
    assert "skip" in content.lower() or "absent" in content.lower() or "no label" in content.lower()


# ---------------------------------------------------------------------------
# project.json read-and-render simulation
# ---------------------------------------------------------------------------


def test_pr_categories_extraction_python_snippet(tmp_path: Path) -> None:
    """Simulate the Python one-liner from the template with a real project.json."""
    project_json = tmp_path / ".autonomous-team" / "project.json"
    project_json.parent.mkdir(parents=True)
    project_json.write_text(
        json.dumps({"pr_categories": ["bugfix", "feature", "security"]})
    )

    # Replicate the exact snippet from the template
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; print(' '.join(json.load(open('.autonomous-team/project.json')).get('pr_categories', [])))",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "bugfix feature security"


def test_pr_categories_extraction_empty_when_field_absent(tmp_path: Path) -> None:
    """Snippet returns empty string when pr_categories is not in project.json."""
    project_json = tmp_path / ".autonomous-team" / "project.json"
    project_json.parent.mkdir(parents=True)
    project_json.write_text(json.dumps({"project_name": "test"}))

    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; print(' '.join(json.load(open('.autonomous-team/project.json')).get('pr_categories', [])))",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_templates_are_in_sync() -> None:
    """backend and loop-bootstrap-snapshot executor templates must have identical pr_categories blocks."""
    backend_content = _load_template(_TMPL_PATH)
    snapshot_content = _load_template(_SNAPSHOT_TMPL_PATH)

    # Extract the block containing pr_categories from each template
    def _extract_pr_block(content: str) -> str:
        lines = content.splitlines()
        in_block = False
        block_lines: list[str] = []
        for line in lines:
            if "pr_categories" in line or (in_block and block_lines):
                in_block = True
                block_lines.append(line)
                # End of block: next step marker or empty line after content
                if in_block and line.strip().startswith("5b."):
                    break
        return "\n".join(block_lines)

    backend_block = _extract_pr_block(backend_content)
    snapshot_block = _extract_pr_block(snapshot_content)
    assert backend_block == snapshot_block, (
        "executor.tmpl pr_categories block differs between backend/ and loop-bootstrap-snapshot/\n"
        f"backend:\n{backend_block}\n\nsnapshot:\n{snapshot_block}"
    )
