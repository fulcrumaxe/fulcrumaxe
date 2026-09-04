"""Tests for backend/project_config.py — load, defaults_for, schema validation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.project_config import (  # noqa: E402
    ProjectConfig,
    defaults_for,
    load,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_VALID = {
    "project_name": "testproj",
    "repo": "example-org/testproj",
    "repo_path": "/tmp/testproj",
    "language": "rust",
    "state_dir": "/tmp/.testproj-state",
}

FULL_VALID = {
    **MINIMAL_VALID,
    "branch_pattern": "task-{epic}-{task}",
    "commit_pattern": "feat(epic-{epic}): task {task}",
    "hub_files": ["crates/server/src/routes/mod.rs"],
    "preflight": {
        "check": "cargo check --workspace",
        "lint": "cargo clippy",
        "test": "cargo test",
        "build": "cargo build --release",
    },
    "toolchain": {
        "cargo_target_dir": "/tmp/.testproj-state/cargo-target",
        "sccache": True,
    },
    "concurrency_cap": 2,
    "executor_token_cap": 80000,
    "mcp_servers": ["projectb-devtools"],
    "task_source": {
        "type": "github_discussions",
        "imported_from": "epic_files",
    },
    "project_claude_md": "CLAUDE.md",
    "extra_user_field": "preserved",
}


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir()
    return tmp_path


def write_project_json(project_dir: Path, data: dict) -> Path:
    config_path = project_dir / ".autonomous-team" / "project.json"
    config_path.write_text(json.dumps(data))
    return config_path


# ---------------------------------------------------------------------------
# load() tests
# ---------------------------------------------------------------------------


def test_load_minimal(project_dir: Path) -> None:
    write_project_json(project_dir, MINIMAL_VALID)
    cfg = load(project_dir)

    assert cfg.project_name == "testproj"
    assert cfg.repo == "example-org/testproj"
    assert cfg.repo_path == Path("/tmp/testproj")
    assert cfg.language == "rust"
    assert cfg.state_dir == Path("/tmp/.testproj-state")


def test_load_full(project_dir: Path) -> None:
    write_project_json(project_dir, FULL_VALID)
    cfg = load(project_dir)

    assert cfg.hub_files == ["crates/server/src/routes/mod.rs"]
    assert cfg.concurrency_cap == 2
    assert cfg.executor_token_cap == 80000
    assert cfg.mcp_servers == ["projectb-devtools"]
    assert cfg.toolchain["sccache"] is True


def test_load_preserves_extra_fields(project_dir: Path) -> None:
    write_project_json(project_dir, FULL_VALID)
    cfg = load(project_dir)

    assert "extra_user_field" in cfg.extra
    assert cfg.extra["extra_user_field"] == "preserved"


def test_load_defaults_for_missing_optional_fields(project_dir: Path) -> None:
    write_project_json(project_dir, MINIMAL_VALID)
    cfg = load(project_dir)

    assert cfg.branch_pattern == "task-{epic}-{task}"
    assert cfg.concurrency_cap == 2
    assert cfg.executor_token_cap == 60000
    assert cfg.mcp_servers == []
    assert cfg.hub_files == []


def test_load_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="project.json not found"):
        load(tmp_path)


def test_load_raises_for_missing_required_field(project_dir: Path) -> None:
    data = dict(MINIMAL_VALID)
    del data["language"]
    write_project_json(project_dir, data)

    with pytest.raises(ValueError, match="language"):
        load(project_dir)


def test_load_raises_for_invalid_json(project_dir: Path) -> None:
    config_path = project_dir / ".autonomous-team" / "project.json"
    config_path.write_text("{ not valid json }")

    with pytest.raises(Exception):  # json.JSONDecodeError or ValueError
        load(project_dir)


def test_load_as_dict_roundtrip(project_dir: Path) -> None:
    write_project_json(project_dir, FULL_VALID)
    cfg = load(project_dir)
    d = cfg.as_dict()

    assert d["project_name"] == "testproj"
    assert d["repo_path"] == "/tmp/testproj"
    assert d["state_dir"] == "/tmp/.testproj-state"
    # Extra field is preserved in round-trip
    assert d["extra_user_field"] == "preserved"


# ---------------------------------------------------------------------------
# defaults_for() tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["rust", "python", "typescript", "polyglot"])
def test_defaults_for_returns_dict(language: str) -> None:
    d = defaults_for(language)
    assert isinstance(d, dict)
    assert "preflight" in d
    assert "concurrency_cap" in d
    assert "executor_token_cap" in d


def test_defaults_for_rust(language: str = "rust") -> None:
    d = defaults_for("rust")
    assert d["concurrency_cap"] == 2
    assert d["executor_token_cap"] == 80000
    assert "cargo check" in d["preflight"]["check"]
    assert "clippy" in d["preflight"]["lint"]
    # build command must be generic — no project-specific -p flag
    assert d["preflight"]["build"] == "cargo build --release"
    assert "-p " not in d["preflight"]["build"]
    assert "toolchain" in d
    assert d["toolchain"].get("sccache") is True
    # cargo_target_dir must be a template placeholder, not a resolved path
    assert d["toolchain"]["cargo_target_dir"] == "{state_dir}/cargo-target"


def test_defaults_for_python() -> None:
    d = defaults_for("python")
    assert d["concurrency_cap"] == 4
    assert d["executor_token_cap"] == 50000
    assert "py_compile" in d["preflight"]["check"]
    assert "ruff" in d["preflight"]["lint"]


def test_defaults_for_typescript() -> None:
    d = defaults_for("typescript")
    assert d["concurrency_cap"] == 4
    assert d["executor_token_cap"] == 50000
    assert "typecheck" in d["preflight"]["check"]


def test_defaults_for_polyglot() -> None:
    d = defaults_for("polyglot")
    assert d["concurrency_cap"] == 2
    assert d["executor_token_cap"] == 60000
    # Polyglot has empty placeholders — user must configure
    assert d["preflight"]["check"] == ""
    assert d["preflight"]["lint"] == ""


def test_defaults_for_unknown_language_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported language"):
        defaults_for("cobol")


# ---------------------------------------------------------------------------
# Schema validation edge cases
# ---------------------------------------------------------------------------


def test_load_coerces_string_numbers(project_dir: Path) -> None:
    data = dict(MINIMAL_VALID)
    data["concurrency_cap"] = "3"      # string instead of int
    data["executor_token_cap"] = "90000"
    write_project_json(project_dir, data)

    cfg = load(project_dir)
    assert cfg.concurrency_cap == 3
    assert cfg.executor_token_cap == 90000


def test_load_with_all_languages(project_dir: Path) -> None:
    for lang in ("rust", "python", "typescript", "polyglot"):
        data = {**MINIMAL_VALID, "language": lang}
        write_project_json(project_dir, data)
        cfg = load(project_dir)
        assert cfg.language == lang


# ---------------------------------------------------------------------------
# pr_categories tests
# ---------------------------------------------------------------------------


def test_pr_categories_defaults_to_empty_list(project_dir: Path) -> None:
    write_project_json(project_dir, MINIMAL_VALID)
    cfg = load(project_dir)
    assert cfg.pr_categories == []


def test_pr_categories_loaded_from_project_json(project_dir: Path) -> None:
    data = {**MINIMAL_VALID, "pr_categories": ["bugfix", "feature", "security"]}
    write_project_json(project_dir, data)
    cfg = load(project_dir)
    assert cfg.pr_categories == ["bugfix", "feature", "security"]


def test_pr_categories_survives_roundtrip(project_dir: Path) -> None:
    data = {**MINIMAL_VALID, "pr_categories": ["feature", "docs"]}
    write_project_json(project_dir, data)
    cfg = load(project_dir)
    d = cfg.as_dict()
    assert d["pr_categories"] == ["feature", "docs"]


def test_pr_categories_not_in_extra(project_dir: Path) -> None:
    data = {**MINIMAL_VALID, "pr_categories": ["infra"]}
    write_project_json(project_dir, data)
    cfg = load(project_dir)
    # pr_categories is a known field, so it must NOT land in extra
    assert "pr_categories" not in cfg.extra
