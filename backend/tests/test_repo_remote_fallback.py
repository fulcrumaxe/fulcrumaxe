"""Tests for the origin-remote fallback in repo-slug resolution (D#2340).

The bug this guards: .autonomous-team/ is excluded from the open-source
export, so a fresh clone of the export had no project.json, backend/_repo.py
raised at import, and 40 backend modules failed to import. The guard is
invisible in this checkout — the engine ships a committed project.json, so
step 3 always resolves here. These tests therefore build a tree that has no
.autonomous-team/ at all and point the resolver at it.

Run with:
    python -m pytest backend/tests/test_repo_remote_fallback.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend._repo_remote import _slug_from_url, repo_slug_from_git_config  # noqa: E402


def _write_git_config(root: Path, body: str) -> None:
    git_dir = root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text(body)


def _origin_config(url: str) -> str:
    return (
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        '[remote "origin"]\n'
        f"\turl = {url}\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
    )


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/adopter/theirfork.git", "adopter/theirfork"),
        ("https://github.com/adopter/theirfork", "adopter/theirfork"),
        ("git@github.com:adopter/theirfork.git", "adopter/theirfork"),
        ("ssh://git@github.com/adopter/theirfork.git", "adopter/theirfork"),
        ("https://user@github.com/adopter/theirfork.git", "adopter/theirfork"),
        ("  https://github.com/adopter/theirfork.git  ", "adopter/theirfork"),
    ],
)
def test_slug_from_url_accepts_real_remote_forms(url: str, expected: str) -> None:
    assert _slug_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        # A clone from a local path: not an OWNER/NAME remote, and guessing
        # one from the last two path segments would be a wrong answer.
        "/home/someone/checkouts/fulcrumaxe",
        "file:///home/someone/checkouts/fulcrumaxe",
        "https://github.com/adopter",
        "https://github.com/adopter/nested/deeper.git",
        "https://github.com//theirfork.git",
        "git@github.com:adopter/their fork.git",
    ],
)
def test_slug_from_url_rejects_anything_not_owner_name(url: str) -> None:
    assert _slug_from_url(url) is None


# ---------------------------------------------------------------------------
# .git/config reading — never raises, whatever it finds
# ---------------------------------------------------------------------------


def test_reads_origin_from_git_config(tmp_path: Path) -> None:
    _write_git_config(tmp_path, _origin_config("https://github.com/adopter/theirfork.git"))
    assert repo_slug_from_git_config(tmp_path) == "adopter/theirfork"


def test_no_git_directory_returns_none(tmp_path: Path) -> None:
    assert repo_slug_from_git_config(tmp_path) is None


def test_git_is_a_file_returns_none(tmp_path: Path) -> None:
    # What a linked worktree looks like: .git is a file, not a directory,
    # so .git/config is not a readable path.
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    assert repo_slug_from_git_config(tmp_path) is None


def test_config_without_origin_returns_none(tmp_path: Path) -> None:
    _write_git_config(tmp_path, "[core]\n\tbare = false\n")
    assert repo_slug_from_git_config(tmp_path) is None


def test_malformed_config_returns_none(tmp_path: Path) -> None:
    _write_git_config(tmp_path, "this is not ini\n= = =\n")
    assert repo_slug_from_git_config(tmp_path) is None


def test_percent_in_url_does_not_raise(tmp_path: Path) -> None:
    # RawConfigParser, not ConfigParser: a '%' in a value must not be treated
    # as interpolation syntax.
    _write_git_config(tmp_path, _origin_config("https://github.com/adopter/their%20fork.git"))
    assert repo_slug_from_git_config(tmp_path) == "adopter/their%20fork"


def test_duplicate_url_keys_do_not_raise(tmp_path: Path) -> None:
    _write_git_config(
        tmp_path,
        '[remote "origin"]\n'
        "\turl = https://github.com/adopter/theirfork.git\n"
        "\turl = https://github.com/adopter/mirror.git\n",
    )
    assert repo_slug_from_git_config(tmp_path) == "adopter/mirror"


# ---------------------------------------------------------------------------
# The actual regression: _load_repo() with steps 1-3 all unavailable
# ---------------------------------------------------------------------------


def _isolate_load_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point _load_repo()'s three existing steps at nothing.

    _load_repo derives the repo root from the module global __file__, so
    reassigning it relocates both the project.json lookup and the new
    .git/config lookup onto *tmp_path*.
    """
    import backend._repo as repo_mod

    monkeypatch.delenv("AUTONOMOUS_TEAM_REPO", raising=False)
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "empty-state"))
    monkeypatch.setattr(repo_mod, "__file__", str(tmp_path / "backend" / "_repo.py"))


def test_load_repo_falls_back_to_origin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from backend._repo import _load_repo

    _isolate_load_repo(monkeypatch, tmp_path)
    _write_git_config(tmp_path, _origin_config("git@github.com:adopter/theirfork.git"))

    assert _load_repo() == "adopter/theirfork"


def test_load_repo_still_raises_without_a_usable_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend._repo import _load_repo

    _isolate_load_repo(monkeypatch, tmp_path)
    _write_git_config(tmp_path, _origin_config("/home/someone/checkouts/fulcrumaxe"))

    with pytest.raises(RuntimeError, match="could not resolve a repo slug"):
        _load_repo()


def test_project_json_still_wins_over_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The new step is a fallback, not an override — step 3 still decides."""
    from backend._repo import _load_repo

    _isolate_load_repo(monkeypatch, tmp_path)
    _write_git_config(tmp_path, _origin_config("https://github.com/adopter/theirfork.git"))
    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "project.json").write_text('{"repo": "configured/slug"}')

    assert _load_repo() == "configured/slug"


# ---------------------------------------------------------------------------
# Same fallback for the fleet project name (the export's last CI failure)
# ---------------------------------------------------------------------------


def test_project_name_derives_from_origin_when_config_missing(tmp_path: Path) -> None:
    from backend.fleet.project_name import resolve_project_name

    _write_git_config(tmp_path, _origin_config("https://github.com/adopter/theirfork.git"))
    assert resolve_project_name(tmp_path) == "theirfork"


def test_project_name_config_still_wins_when_present(tmp_path: Path) -> None:
    from backend.fleet.project_name import resolve_project_name

    _write_git_config(tmp_path, _origin_config("https://github.com/adopter/theirfork.git"))
    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "config.json").write_text('{"project_name": "from-config"}')

    assert resolve_project_name(tmp_path) == "from-config"


def test_project_name_still_raises_with_no_config_and_no_remote(tmp_path: Path) -> None:
    from backend.fleet.project_name import ProjectNameUnresolvable, resolve_project_name

    with pytest.raises(ProjectNameUnresolvable, match="cannot read"):
        resolve_project_name(tmp_path)
