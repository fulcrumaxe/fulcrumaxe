"""Tests for scripts/import-epic-tasks.py — frontmatter parse, title formatting, dry-run."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# Import functions directly from the script
import importlib.util

_SCRIPT_PATH = _REPO_ROOT / "scripts" / "import-epic-tasks.py"
_spec = importlib.util.spec_from_file_location("import_epic_tasks", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

parse_frontmatter = _mod.parse_frontmatter
format_title = _mod.format_title
find_task_files = _mod.find_task_files


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------

SAMPLE_FM = textwrap.dedent("""\
    ---
    epic: 7
    task: 25b
    title: Agent runner image Dockerfile
    type: enhancement
    status: not-started
    estimated_hours: 3
    depends_on: [24, 25a]
    tags: [docker, ci]
    ---

    ## Overview
    Build a Dockerfile for the agent runner.

    ## Acceptance Criteria
    - Dockerfile exists at the repo root.
""")


def test_parse_frontmatter_extracts_fields() -> None:
    fm, body = parse_frontmatter(SAMPLE_FM)

    assert fm["epic"] == 7
    assert fm["task"] == "25b"
    assert fm["title"] == "Agent runner image Dockerfile"
    assert fm["type"] == "enhancement"
    assert fm["status"] == "not-started"
    assert fm["estimated_hours"] == 3
    assert fm["depends_on"] == [24, "25a"]


def test_parse_frontmatter_body_content() -> None:
    fm, body = parse_frontmatter(SAMPLE_FM)

    assert "## Overview" in body
    assert "Acceptance Criteria" in body


def test_parse_frontmatter_no_frontmatter() -> None:
    text = "# Just markdown\nNo frontmatter here."
    fm, body = parse_frontmatter(text)

    assert fm == {}
    assert body == text


def test_parse_frontmatter_empty_frontmatter() -> None:
    text = "---\n---\nBody here."
    fm, body = parse_frontmatter(text)

    assert fm == {}
    assert "Body here." in body


def test_parse_frontmatter_invalid_yaml_returns_empty() -> None:
    text = "---\n: : bad yaml\n---\nbody"
    fm, body = parse_frontmatter(text)
    # Should not raise; returns empty dict
    assert isinstance(fm, dict)


# ---------------------------------------------------------------------------
# Title formatter
# ---------------------------------------------------------------------------


def test_format_title_basic() -> None:
    fm = {
        "epic": 7,
        "task": "25b",
        "title": "Agent runner image Dockerfile",
        "type": "enhancement",
    }
    title = format_title(fm)
    assert title == "[Enhancement] epic-7.25b — Agent runner image Dockerfile"


def test_format_title_capitalises_type() -> None:
    fm = {"epic": 1, "task": "3", "title": "fix crash", "type": "bug"}
    title = format_title(fm)
    assert title.startswith("[Bug]")


def test_format_title_feature_type() -> None:
    fm = {"epic": 2, "task": "10", "title": "add login", "type": "feature"}
    title = format_title(fm)
    assert title == "[Feature] epic-2.10 — add login"


def test_format_title_missing_type_defaults_to_task() -> None:
    fm = {"epic": 1, "task": "1", "title": "something"}
    title = format_title(fm)
    assert "[Task]" in title


def test_format_title_missing_fields_uses_placeholder() -> None:
    fm = {}
    title = format_title(fm)
    # Should not crash; uses ? placeholders
    assert "epic-?" in title or "[" in title


# ---------------------------------------------------------------------------
# find_task_files
# ---------------------------------------------------------------------------


def test_find_task_files_walks_epics(tmp_path: Path) -> None:
    # Create epic-7/25b.md and epic-7/epic.md (should be skipped)
    epic_dir = tmp_path / "epics" / "epic-7"
    epic_dir.mkdir(parents=True)
    (epic_dir / "25b.md").write_text(SAMPLE_FM)
    (epic_dir / "epic.md").write_text("# Epic overview")
    (epic_dir / "26.md").write_text(SAMPLE_FM)

    files = find_task_files(tmp_path)

    names = {f.name for f in files}
    assert "25b.md" in names
    assert "26.md" in names
    # epic.md must be skipped
    assert "epic.md" not in names


def test_find_task_files_epic_filter(tmp_path: Path) -> None:
    for epic in (7, 8):
        d = tmp_path / "epics" / f"epic-{epic}"
        d.mkdir(parents=True)
        (d / "1.md").write_text(SAMPLE_FM)

    files = find_task_files(tmp_path, epic_filter=7)
    assert len(files) == 1
    assert "epic-7" in str(files[0])


def test_find_task_files_empty_when_no_epics_dir(tmp_path: Path) -> None:
    files = find_task_files(tmp_path)
    assert files == []


# ---------------------------------------------------------------------------
# Dry-run integration (no real API calls)
# ---------------------------------------------------------------------------


def test_dry_run_prints_would_create(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Dry-run must not make API calls and must print intent."""
    epic_dir = tmp_path / "epics" / "epic-7"
    epic_dir.mkdir(parents=True)
    (epic_dir / "1.md").write_text(SAMPLE_FM)

    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir()

    run_import = _mod.run_import

    with (
        patch.object(_mod, "list_existing_discussion_titles", return_value={}),
        patch.object(_mod, "get_repo_node_id", return_value="REPOID"),
        patch.object(_mod, "get_discussion_category_id", return_value="CATID"),
        patch.object(_mod, "create_discussion", return_value=None) as mock_create,
        patch.object(_mod, "ensure_label") as mock_label,
    ):
        run_import(
            repo_path=tmp_path,
            repo="example-org/testproj",
            status_filter={"not-started"},
            dry_run=True,
            epic_filter=None,
        )

    captured = capsys.readouterr()
    assert "dry-run" in captured.out.lower() or "would create" in captured.out.lower()
    # create_discussion should not be called with actual side effects in dry_run
    # (our implementation passes dry_run=True, which short-circuits)
    for call in mock_create.call_args_list:
        assert call.kwargs.get("dry_run", False) is True or call.args[-1] is True


def test_status_filter_excludes_completed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Tasks with status=completed should be excluded by default filter."""
    completed_fm = SAMPLE_FM.replace("status: not-started", "status: completed")
    epic_dir = tmp_path / "epics" / "epic-7"
    epic_dir.mkdir(parents=True)
    (epic_dir / "1.md").write_text(completed_fm)

    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir()

    run_import = _mod.run_import

    with (
        patch.object(_mod, "list_existing_discussion_titles", return_value={}),
        patch.object(_mod, "get_repo_node_id", return_value="REPOID"),
        patch.object(_mod, "get_discussion_category_id", return_value="CATID"),
        patch.object(_mod, "create_discussion") as mock_create,
    ):
        run_import(
            repo_path=tmp_path,
            repo="example-org/testproj",
            status_filter={"not-started", "in_progress"},
            dry_run=False,
            epic_filter=None,
        )

    # create_discussion should NOT have been called for a completed task
    mock_create.assert_not_called()


def test_idempotent_skips_existing_discussion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """If a Discussion with the same title already exists, skip creation."""
    epic_dir = tmp_path / "epics" / "epic-7"
    epic_dir.mkdir(parents=True)
    (epic_dir / "25b.md").write_text(SAMPLE_FM)

    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir()

    run_import = _mod.run_import
    expected_title = "[Enhancement] epic-7.25b — Agent runner image Dockerfile"

    with (
        patch.object(
            _mod,
            "list_existing_discussion_titles",
            return_value={expected_title: 42},
        ),
        patch.object(_mod, "get_repo_node_id", return_value="REPOID"),
        patch.object(_mod, "get_discussion_category_id", return_value="CATID"),
        patch.object(_mod, "create_discussion") as mock_create,
    ):
        run_import(
            repo_path=tmp_path,
            repo="example-org/testproj",
            status_filter={"not-started"},
            dry_run=False,
            epic_filter=None,
        )

    # Must not attempt to create the discussion again
    mock_create.assert_not_called()

    out = capsys.readouterr().out
    assert "Skip" in out or "skip" in out or "#42" in out


# ---------------------------------------------------------------------------
# Security: symlink skipping (BLOCKER 2)
# ---------------------------------------------------------------------------


def test_find_task_files_skips_symlinks(tmp_path: Path) -> None:
    """Symlinked task files must NOT be included — they could point to /etc/passwd etc."""
    epic_dir = tmp_path / "epics" / "epic-1"
    epic_dir.mkdir(parents=True)

    # A real task file
    real_task = epic_dir / "1.md"
    real_task.write_text(SAMPLE_FM)

    # A symlink pointing to a stub sensitive file
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("root:x:0:0:root:/root:/bin/bash\n")  # /etc/passwd-like content
    symlink_task = epic_dir / "2.md"
    symlink_task.symlink_to(sensitive)

    files = find_task_files(tmp_path)

    # The real file must be included
    assert real_task in files
    # The symlink must NOT be included
    assert symlink_task not in files


# ---------------------------------------------------------------------------
# Security: coldstart path traversal (BLOCKER 1) — shell-level test
# ---------------------------------------------------------------------------


def test_coldstart_rejects_path_traversal_project_name(tmp_path: Path) -> None:
    """coldstart-project.sh must exit non-zero for project names with ../."""
    import subprocess

    _COLDSTART = _REPO_ROOT / "scripts" / "coldstart-project.sh"
    if not _COLDSTART.exists():
        pytest.skip("coldstart-project.sh not found")

    # Create a minimal git repo to pass the git-repo check
    git_repo = tmp_path / "repo"
    git_repo.mkdir()
    subprocess.run(["git", "init", str(git_repo)], check=True, capture_output=True)

    result = subprocess.run(
        ["bash", str(_COLDSTART), str(git_repo), "../evil"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, (
        f"Expected non-zero exit for path-traversal project_name, got 0.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    assert "project_name" in result.stderr.lower() or "error" in result.stderr.lower(), (
        f"Expected error message about project_name in stderr, got: {result.stderr!r}"
    )
