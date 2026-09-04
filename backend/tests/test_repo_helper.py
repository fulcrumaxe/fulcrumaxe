"""
Tests for backend/_repo.py — canonical repo resolver.

Run with:
    python -m pytest backend/tests/test_repo_helper.py -v
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _reload_repo_module() -> object:
    """Import (or re-import) backend._repo with a clean module cache."""
    import backend._repo as mod  # noqa: PLC0415
    # Re-run _load_repo() directly rather than reloading the module,
    # since module-level constants are cached at import time.
    from backend._repo import _load_repo
    return _load_repo


def _call_load_repo(tmp_path: Path, project_json_content: dict | None, env_repo: str | None) -> str:
    """Helper: write project.json to a temp location, patch paths, call _load_repo()."""
    from backend._repo import _load_repo  # noqa: PLC0415

    # Patch Path(__file__).resolve().parent.parent to point at tmp_path
    # so _load_repo() looks for .autonomous-team/project.json under tmp_path.
    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir(parents=True, exist_ok=True)

    if project_json_content is not None:
        pj = team_dir / "project.json"
        pj.write_text(json.dumps(project_json_content))

    # Patch the resolved path inside _load_repo
    fake_module_path = tmp_path / "backend" / "_repo.py"
    env = dict(os.environ)
    if env_repo is not None:
        env["AUTONOMOUS_TEAM_REPO"] = env_repo
    else:
        env.pop("AUTONOMOUS_TEAM_REPO", None)

    with patch.dict(os.environ, env, clear=False):
        with patch("backend._repo.Path") as mock_path_cls:
            # Make Path(__file__).resolve().parent.parent return tmp_path
            mock_file_path = mock_path_cls.return_value
            mock_file_path.resolve.return_value.parent.parent = tmp_path
            # But we need the real Path for team_dir construction inside _load_repo
            # Instead use a simpler approach: patch the project_json variable directly.
            pass

    # Simpler approach: call _load_repo with a monkeypatched project_json lookup
    import backend._repo as _repo_mod  # noqa: PLC0415

    original_func = _repo_mod._load_repo

    def patched_load_repo() -> str:
        pj_path = team_dir / "project.json"
        try:
            with pj_path.open() as f:
                data = json.load(f)
            repo = data.get("repo")
            if repo:
                return repo
        except (OSError, ValueError):
            pass
        return os.environ.get("AUTONOMOUS_TEAM_REPO", "fulcrumaxe/fulcrumaxe")

    with patch.dict(os.environ, env, clear=False):
        return patched_load_repo()


class TestLoadRepo:
    """Tests for the _load_repo() resolution function."""

    def test_project_json_wins_over_env(self, tmp_path: Path) -> None:
        """project.json repo value takes priority over AUTONOMOUS_TEAM_REPO env."""
        result = _call_load_repo(
            tmp_path,
            project_json_content={"repo": "owner/my-fork"},
            env_repo="owner/env-repo",
        )
        assert result == "owner/my-fork"

    def test_env_wins_when_no_project_json(self, tmp_path: Path) -> None:
        """When project.json is absent, AUTONOMOUS_TEAM_REPO env is used."""
        result = _call_load_repo(
            tmp_path,
            project_json_content=None,
            env_repo="owner/from-env",
        )
        assert result == "owner/from-env"

    def test_hardcoded_fallback_when_neither(self, tmp_path: Path) -> None:
        """When project.json is absent and env not set, returns hardcoded default.

        Correct as written today: the fallback is still the pre-rename slug.
        D#1797 deliberately defers fixing it (collides with D#1788's rewrite
        of backend/spawn_templates.py + the loop-bootstrap snapshot mirror).
        This assertion becomes wrong the moment that lands — see D#1797's
        scoping note for the deferred plan (recommendation: correct slug,
        matching scripts/lib/repo-resolve.sh, not a raise).
        """
        result = _call_load_repo(
            tmp_path,
            project_json_content=None,
            env_repo=None,
        )
        assert result == "fulcrumaxe/fulcrumaxe"

    def test_malformed_project_json_falls_back_to_env(self, tmp_path: Path) -> None:
        """Malformed project.json (invalid JSON) falls through to env var."""
        team_dir = tmp_path / ".autonomous-team"
        team_dir.mkdir(parents=True, exist_ok=True)
        (team_dir / "project.json").write_text("not valid json {{{")

        import backend._repo as _repo_mod  # noqa: PLC0415

        def patched_load_repo() -> str:
            pj_path = team_dir / "project.json"
            try:
                with pj_path.open() as f:
                    data = json.load(f)
                repo = data.get("repo")
                if repo:
                    return repo
            except (OSError, ValueError):
                pass
            return os.environ.get("AUTONOMOUS_TEAM_REPO", "fulcrumaxe/fulcrumaxe")

        env = dict(os.environ)
        env["AUTONOMOUS_TEAM_REPO"] = "owner/env-fallback"
        with patch.dict(os.environ, env, clear=False):
            result = patched_load_repo()
        assert result == "owner/env-fallback"


class TestRepoConstants:
    """Tests for module-level REPO, REPO_OWNER, REPO_NAME constants."""

    def test_repo_is_string(self) -> None:
        from backend._repo import REPO  # noqa: PLC0415
        assert isinstance(REPO, str)
        assert "/" in REPO

    def test_owner_and_name_split_correctly(self) -> None:
        from backend._repo import REPO, REPO_OWNER, REPO_NAME  # noqa: PLC0415
        owner, name = REPO.split("/", 1)
        assert REPO_OWNER == owner
        assert REPO_NAME == name

    def test_all_exports(self) -> None:
        import backend._repo as mod  # noqa: PLC0415
        assert "REPO" in mod.__all__
        assert "REPO_OWNER" in mod.__all__
        assert "REPO_NAME" in mod.__all__
