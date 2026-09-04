"""
backend/project_config.py — loader and schema for per-project configuration.

Each project using the autonomous-team harness stores a project.json file at
<repo>/.autonomous-team/project.json.  This module reads that file and exposes
a typed ProjectConfig dataclass.

Usage::

    from pathlib import Path
    from backend.project_config import load, defaults_for

    cfg = load(Path("/path/to/repo"))
    print(cfg.language)          # "rust"
    print(cfg.concurrency_cap)   # 2

    d = defaults_for("rust")
    print(d["preflight"]["lint"])   # "cargo clippy ..."

CLI::

    python3 backend/project_config.py defaults <language>
    python3 backend/project_config.py show <repo-path>
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Language-default tables
# ---------------------------------------------------------------------------

_PREFLIGHT_DEFAULTS: dict[str, dict[str, str]] = {
    "rust": {
        "check": "cargo check --workspace",
        "lint": "cargo clippy --workspace --all-targets -- -D warnings",
        "test": "cargo test -p {crate}",
        "build": "cargo build --release",
    },
    "python": {
        "check": "python3 -m py_compile {changed}",
        "lint": "ruff check",
        "test": "pytest",
        "build": "",
    },
    "typescript": {
        "check": "npm run typecheck",
        "lint": "npm run lint",
        "test": "npm test",
        "build": "npm run build",
    },
    "polyglot": {
        "check": "",
        "lint": "",
        "test": "",
        "build": "",
    },
}

_CONCURRENCY_CAPS: dict[str, int] = {
    "rust": 2,
    "python": 4,
    "typescript": 4,
    "polyglot": 2,
}

_EXECUTOR_TOKEN_CAPS: dict[str, int] = {
    "rust": 80000,
    "python": 50000,
    "typescript": 50000,
    "polyglot": 60000,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def defaults_for(language: str) -> dict[str, Any]:
    """Return language-specific default values for project.json fields.

    Parameters
    ----------
    language:
        One of ``rust``, ``python``, ``typescript``, ``polyglot``.

    Returns
    -------
    dict
        Keys: ``preflight``, ``concurrency_cap``, ``executor_token_cap``,
        and ``toolchain`` (only for languages that have toolchain defaults).

    Raises
    ------
    ValueError
        If *language* is not in the supported set.
    """
    supported = {"rust", "python", "typescript", "polyglot"}
    if language not in supported:
        raise ValueError(
            f"Unsupported language {language!r}. Choose one of: {', '.join(sorted(supported))}"
        )

    result: dict[str, Any] = {
        "preflight": dict(_PREFLIGHT_DEFAULTS[language]),
        "concurrency_cap": _CONCURRENCY_CAPS[language],
        "executor_token_cap": _EXECUTOR_TOKEN_CAPS[language],
    }

    # Toolchain extras (only for languages with non-trivial toolchain config)
    if language == "rust":
        result["toolchain"] = {
            "cargo_target_dir": "{state_dir}/cargo-target",
            "sccache": True,
            "rust_toolchain_file": "rust-toolchain.toml",
        }
    elif language == "typescript":
        result["toolchain"] = {
            "node_version": "18",
        }
    else:
        result["toolchain"] = {}

    return result


@dataclass
class ProjectConfig:
    """Typed representation of .autonomous-team/project.json.

    Unknown keys in the JSON file are preserved in ``extra`` so that
    user-added fields survive a round-trip through load().
    """

    project_name: str
    repo: str
    repo_path: Path
    language: str
    state_dir: Path

    # Optional / defaulted fields
    branch_pattern: str = "task-{epic}-{task}"
    commit_pattern: str = "feat(epic-{epic}): complete task {task} — {title}"
    hub_files: list[str] = field(default_factory=list)
    preflight: dict[str, str] = field(default_factory=dict)
    toolchain: dict[str, Any] = field(default_factory=dict)
    concurrency_cap: int = 2
    executor_token_cap: int = 60000
    mcp_servers: list[str] = field(default_factory=list)
    task_source: dict[str, Any] = field(default_factory=dict)
    project_claude_md: str = "CLAUDE.md"
    pr_categories: list[str] = field(default_factory=list)

    # Catch-all for forward-compatible fields
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialise back to a plain dict (suitable for json.dumps)."""
        base: dict[str, Any] = {
            "project_name": self.project_name,
            "repo": self.repo,
            "repo_path": str(self.repo_path),
            "language": self.language,
            "state_dir": str(self.state_dir),
            "branch_pattern": self.branch_pattern,
            "commit_pattern": self.commit_pattern,
            "hub_files": self.hub_files,
            "preflight": self.preflight,
            "toolchain": self.toolchain,
            "concurrency_cap": self.concurrency_cap,
            "executor_token_cap": self.executor_token_cap,
            "mcp_servers": self.mcp_servers,
            "task_source": self.task_source,
            "project_claude_md": self.project_claude_md,
            "pr_categories": self.pr_categories,
        }
        base.update(self.extra)
        return base


_REQUIRED_FIELDS = {"project_name", "repo", "repo_path", "language", "state_dir"}


def load(project_root: Path) -> ProjectConfig:
    """Read and validate .autonomous-team/project.json from *project_root*.

    Parameters
    ----------
    project_root:
        Absolute path to the repository root (the directory that contains
        ``.autonomous-team/project.json``).

    Returns
    -------
    ProjectConfig

    Raises
    ------
    FileNotFoundError
        If project.json does not exist.
    ValueError
        If required fields are missing or have wrong types.
    json.JSONDecodeError
        If the file is not valid JSON.
    """
    config_path = project_root / ".autonomous-team" / "project.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"project.json not found at {config_path}. "
            "Run: bash scripts/coldstart-project.sh <repo-path> <project-name>"
        )

    raw: dict[str, Any] = json.loads(config_path.read_text())

    # Validate required fields
    missing = _REQUIRED_FIELDS - raw.keys()
    if missing:
        raise ValueError(
            f"project.json is missing required fields: {', '.join(sorted(missing))}"
        )

    # Extract known fields; put the rest in extra
    known = {
        "project_name", "repo", "repo_path", "language", "state_dir",
        "branch_pattern", "commit_pattern", "hub_files", "preflight",
        "toolchain", "concurrency_cap", "executor_token_cap",
        "mcp_servers", "task_source", "project_claude_md", "pr_categories",
    }
    extra = {k: v for k, v in raw.items() if k not in known}

    return ProjectConfig(
        project_name=str(raw["project_name"]),
        repo=str(raw["repo"]),
        repo_path=Path(str(raw["repo_path"])),
        language=str(raw["language"]),
        state_dir=Path(str(raw["state_dir"])),
        branch_pattern=str(raw.get("branch_pattern", "task-{epic}-{task}")),
        commit_pattern=str(
            raw.get("commit_pattern", "feat(epic-{epic}): complete task {task} — {title}")
        ),
        hub_files=list(raw.get("hub_files", [])),
        preflight=dict(raw.get("preflight", {})),
        toolchain=dict(raw.get("toolchain", {})),
        concurrency_cap=int(raw.get("concurrency_cap", 2)),
        executor_token_cap=int(raw.get("executor_token_cap", 60000)),
        mcp_servers=list(raw.get("mcp_servers", [])),
        task_source=dict(raw.get("task_source", {})),
        project_claude_md=str(raw.get("project_claude_md", "CLAUDE.md")),
        pr_categories=list(raw.get("pr_categories", [])),
        extra=extra,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cmd_defaults(language: str) -> None:
    """Print language defaults as JSON (used by coldstart-project.sh)."""
    try:
        d = defaults_for(language)
        print(json.dumps(d, indent=2))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)


def _cmd_show(repo_path: str) -> None:
    """Print the loaded ProjectConfig for a repo."""
    try:
        cfg = load(Path(repo_path))
        print(json.dumps(cfg.as_dict(), indent=2))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage:\n"
            "  python3 backend/project_config.py defaults <language>\n"
            "  python3 backend/project_config.py show <repo-path>",
            file=sys.stderr,
        )
        sys.exit(1)

    command = sys.argv[1]
    arg = sys.argv[2]

    if command == "defaults":
        _cmd_defaults(arg)
    elif command == "show":
        _cmd_show(arg)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)
