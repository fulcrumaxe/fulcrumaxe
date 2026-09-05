"""tests/test_backend_repo_py.py

Tests for backend/_repo.py resolution order:
  1. AUTONOMOUS_TEAM_REPO env var wins (highest priority)
  2. <AUTONOMOUS_TEAM_STATE_DIR>/project.json "repo" field
  3. Repo-root .autonomous-team/project.json "repo" field — this repo
     commits that file (see .autonomous-team/project.json), so this step
     always resolves in our own runtime.
  4. Nothing resolved — raise. No hard-coded slug fallback (D#1870): a
     forked adopter with none of the above configured gets an actionable
     error instead of silently inheriting this project's own repo slug.

Steps 2/3 both go through backend._repo._read_project_json(), so tests that
need to reach step 4 monkeypatch that function directly rather than trying
to hide the real, committed .autonomous-team/project.json (its path is
derived from Path(__file__), not overridable via env).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _reload_repo_module(monkeypatch, env: dict[str, str | None]) -> object:
    """Reload backend._repo with the given env vars applied."""
    # Apply env overrides (None value means delete the var)
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)

    # Force fresh import so module-level REPO is re-evaluated
    for mod_name in list(sys.modules):
        if "_repo" in mod_name and "backend" in mod_name:
            del sys.modules[mod_name]

    import backend._repo as repo_mod
    importlib.reload(repo_mod)
    return repo_mod


class TestRepoEnvVarWins:
    """AUTONOMOUS_TEAM_REPO env var takes priority over everything."""

    def test_env_var_beats_state_dir_project_json(self, tmp_path, monkeypatch):
        """AUTONOMOUS_TEAM_REPO wins even when state-dir project.json exists."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "project.json").write_text(json.dumps({"repo": "state-dir/repo"}))

        mod = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": "env/wins",
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
        })
        assert mod.REPO == "env/wins"

    def test_env_var_beats_fallback(self, monkeypatch):
        """AUTONOMOUS_TEAM_REPO wins when no project.json exists anywhere."""
        mod = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": "org/from-env",
            "AUTONOMOUS_TEAM_STATE_DIR": "/nonexistent-path-no-json",
        })
        assert mod.REPO == "org/from-env"

    def test_repo_owner_and_name_split(self, monkeypatch):
        """REPO_OWNER / REPO_NAME are split correctly when env var is used."""
        mod = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": "myorg/myrepo",
            "AUTONOMOUS_TEAM_STATE_DIR": "/nonexistent-path-no-json",
        })
        assert mod.REPO_OWNER == "myorg"
        assert mod.REPO_NAME == "myrepo"


class TestStateDirProjectJson:
    """AUTONOMOUS_TEAM_STATE_DIR/project.json is consulted when env var is absent."""

    def test_state_dir_project_json_used(self, tmp_path, monkeypatch):
        """project.json in state-dir is read when AUTONOMOUS_TEAM_REPO is absent."""
        state_dir = tmp_path / "projectb-state"
        state_dir.mkdir()
        (state_dir / "project.json").write_text(
            json.dumps({"repo": "autonomous-agent-7/projectb"})
        )

        mod = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": None,
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
        })
        assert mod.REPO == "autonomous-agent-7/projectb"

    def test_state_dir_env_changes_lookup_path(self, tmp_path, monkeypatch):
        """Changing AUTONOMOUS_TEAM_STATE_DIR changes which project.json is read."""
        state_a = tmp_path / "state-a"
        state_a.mkdir()
        (state_a / "project.json").write_text(json.dumps({"repo": "org/project-a"}))

        state_b = tmp_path / "state-b"
        state_b.mkdir()
        (state_b / "project.json").write_text(json.dumps({"repo": "org/project-b"}))

        mod_a = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": None,
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_a),
        })
        assert mod_a.REPO == "org/project-a"

        mod_b = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": None,
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_b),
        })
        assert mod_b.REPO == "org/project-b"

    def test_empty_repo_field_falls_through(self, tmp_path, monkeypatch):
        """Empty 'repo' field in state-dir project.json falls through to step 3."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "project.json").write_text(json.dumps({"repo": ""}))

        # No env var, empty state-dir repo field — falls through to step 3
        # (repo-root .autonomous-team/project.json, committed in this repo).
        mod = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": None,
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
        })
        assert mod.REPO == "autonomous-agent-7/fulcrumaxe"

    def test_missing_repo_key_falls_through(self, tmp_path, monkeypatch):
        """project.json without 'repo' key falls through to step 3."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "project.json").write_text(json.dumps({"project_name": "no-key"}))

        mod = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": None,
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
        })
        assert mod.REPO == "autonomous-agent-7/fulcrumaxe"

    def test_malformed_json_falls_through(self, tmp_path, monkeypatch):
        """Malformed project.json is skipped gracefully, falls through to step 3."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "project.json").write_text("NOT JSON {{")

        mod = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": None,
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
        })
        assert mod.REPO == "autonomous-agent-7/fulcrumaxe"


class TestRepoRootProjectJson:
    """Step 3: repo-root .autonomous-team/project.json, committed in this repo.

    Its path is derived from Path(__file__), not overridable via env, so
    this exercises the real, committed file rather than a fixture.
    """

    def test_repo_root_project_json_resolves(self, monkeypatch):
        """With no env var and no state-dir project.json, step 3 resolves
        to this repo's own committed .autonomous-team/project.json — the
        current, post-rename slug (not a hard-coded pre-rename literal)."""
        mod = _reload_repo_module(monkeypatch, {
            "AUTONOMOUS_TEAM_REPO": None,
            "AUTONOMOUS_TEAM_STATE_DIR": "/nonexistent-no-json-here",
        })
        assert mod.REPO == "autonomous-agent-7/fulcrumaxe"
        assert mod.REPO_OWNER == "autonomous-agent-7"
        assert mod.REPO_NAME == "fulcrumaxe"


class TestNoResolutionRaises:
    """Step 4: nothing resolved anywhere — raise rather than default to a
    hard-coded slug (D#1870). Reaching this branch requires bypassing the
    real repo-root project.json, which isn't overridable via env, so these
    tests monkeypatch _read_project_json directly."""

    def test_raises_when_nothing_resolves(self, monkeypatch):
        for key in ("AUTONOMOUS_TEAM_REPO",):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", "/nonexistent-no-json-here")

        for mod_name in list(sys.modules):
            if "_repo" in mod_name and "backend" in mod_name:
                del sys.modules[mod_name]

        import backend._repo as repo_mod
        importlib.reload(repo_mod)

        # Bypass steps 2 and 3 (both call _read_project_json) so step 4 fires.
        monkeypatch.setattr(repo_mod, "_read_project_json", lambda path: None)

        with pytest.raises(RuntimeError, match="could not resolve a repo slug"):
            repo_mod._load_repo()
