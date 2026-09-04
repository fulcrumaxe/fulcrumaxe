"""
Tests for _parse_repo_slug, _read_slug_from_json, and _resolve_repo_for_project
in backend/server.py.

These helpers were extracted to reduce cyclomatic complexity of the
project-scoped repo resolution path.

Run with:
    python -m pytest backend/test_server.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.server import (  # noqa: E402
    _parse_repo_slug,
    _read_slug_from_json,
    _resolve_repo_for_project,
    _REPO_OWNER,
    _REPO_NAME,
)


# ---------------------------------------------------------------------------
# _parse_repo_slug
# ---------------------------------------------------------------------------


def test_parse_repo_slug_valid():
    assert _parse_repo_slug("owner/repo") == ("owner", "repo")


def test_parse_repo_slug_no_slash():
    assert _parse_repo_slug("noslash") is None


def test_parse_repo_slug_empty():
    assert _parse_repo_slug("") is None


def test_parse_repo_slug_missing_owner():
    assert _parse_repo_slug("/repo") is None


def test_parse_repo_slug_missing_name():
    assert _parse_repo_slug("owner/") is None


# ---------------------------------------------------------------------------
# _read_slug_from_json
# ---------------------------------------------------------------------------


def test_read_slug_from_json_first_key(tmp_path):
    f = tmp_path / "runtime.json"
    f.write_text(json.dumps({"repo": "acme/myapp", "project_repo": "other/thing"}))
    assert _read_slug_from_json(f, "repo", "project_repo") == ("acme", "myapp")


def test_read_slug_from_json_fallback_key(tmp_path):
    f = tmp_path / "runtime.json"
    f.write_text(json.dumps({"project_repo": "acme/fallback"}))
    assert _read_slug_from_json(f, "repo", "project_repo") == ("acme", "fallback")


def test_read_slug_from_json_no_matching_key(tmp_path):
    f = tmp_path / "runtime.json"
    f.write_text(json.dumps({"other": "value"}))
    assert _read_slug_from_json(f, "repo") is None


def test_read_slug_from_json_corrupt_file(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not valid json{{")
    assert _read_slug_from_json(f, "repo") is None


# ---------------------------------------------------------------------------
# _resolve_repo_for_project
# ---------------------------------------------------------------------------


def test_resolve_repo_no_project():
    assert _resolve_repo_for_project(None) == (_REPO_OWNER, _REPO_NAME)


def test_resolve_repo_empty_string():
    assert _resolve_repo_for_project("") == (_REPO_OWNER, _REPO_NAME)


def test_resolve_repo_reads_runtime_json(tmp_path, monkeypatch):
    """Reads repo from dashboard-runtime.json in the state dir."""
    state_dir = tmp_path / ".myproject-state"
    state_dir.mkdir()
    (state_dir / "dashboard-runtime.json").write_text(
        json.dumps({"repo": "myorg/myproject"})
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    owner, name = _resolve_repo_for_project("myproject")
    assert owner == "myorg"
    assert name == "myproject"


def test_resolve_repo_fallback_to_project_json(tmp_path, monkeypatch):
    """Falls back to project.json when runtime.json has no repo field."""
    state_dir = tmp_path / ".myproject-state"
    state_dir.mkdir()
    (state_dir / "dashboard-runtime.json").write_text(json.dumps({"rpcBaseUrl": "http://x"}))
    (state_dir / "project.json").write_text(json.dumps({"repo": "myorg/via-project"}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    owner, name = _resolve_repo_for_project("myproject")
    assert owner == "myorg"
    assert name == "via-project"


def test_resolve_repo_raises_when_no_state_files(tmp_path, monkeypatch):
    """A named project whose state dir has neither dashboard-runtime.json nor
    project.json raises, not falls back. (An empty state-dir directory with no
    config file in it is treated the same as no state dir — there's nothing
    to point the operator at either way.)
    """
    from backend.rpc_project_scope import UnresolvableProjectError

    state_dir = tmp_path / ".unknown-state"
    state_dir.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    with pytest.raises(UnresolvableProjectError, match="unknown"):
        _resolve_repo_for_project("unknown")
