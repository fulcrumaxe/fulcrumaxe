"""A project name and a repo slug both reach the filesystem / `gh` argv with
no character check (D#2358).

Two independent gaps, one shape:

1. ``state_paths.for_project(name)`` built ``home / f".{name}-state"``
   straight from *name*. ``name="./../../etc"`` resolved outside ``$HOME``
   (CWE-22-shaped path traversal).

2. ``project_repo_slug.resolve_project_repo_slug()`` returned a project's
   ``repo`` field whenever it merely contained one ``"/"``. Both of its
   callers (``backend/rpc/stats_weekly_velocity.py`` and
   ``backend/rpc/stats_cost_per_outcome.py``) hand that value straight to
   ``gh ... --repo <value>`` as a list-form subprocess arg — an unvalidated
   value becomes a `gh` flag (CWE-88-shaped flag injection), e.g. a slug of
   ``"--template=/x/y"``.

Both are closed by a fail-closed charset check (``[A-Za-z0-9._-]``) in the
one resolver each gap's callers all go through, rather than at each call
site.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import state_paths  # noqa: E402
from backend.project_repo_slug import resolve_project_repo_slug  # noqa: E402


# ---------------------------------------------------------------------------
# 1. for_project() — path-traversal via an unvalidated project name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_name",
    ["./../../etc", "../escape", "a/b", "a b", "a\tb", "", "a\x00b"],
)
def test_for_project_rejects_unsafe_names(bad_name, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    with pytest.raises(state_paths.InvalidProjectNameError):
        state_paths.for_project(bad_name)


def test_for_project_traversal_does_not_escape_home(monkeypatch, tmp_path):
    """The measured repro from the Discussion: name="./../../etc" used to
    resolve state_dir to "/etc-state", outside $HOME. Now it must raise
    instead of returning a path at all.
    """
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    with pytest.raises(state_paths.InvalidProjectNameError):
        state_paths.for_project("./../../etc")


@pytest.mark.parametrize(
    "good_name",
    ["projectb", "autonomous-forever", "my.project_v2", "a"],
)
def test_for_project_still_accepts_real_project_names(good_name, monkeypatch, tmp_path):
    """Fail-closed, not over-broad: every project name actually used in this
    codebase's own fixtures/tests must keep working unchanged.
    """
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    paths = state_paths.for_project(good_name)
    assert paths.name == good_name


# ---------------------------------------------------------------------------
# 2. resolve_project_repo_slug() — flag injection via an unvalidated slug
# ---------------------------------------------------------------------------

def _plant_repo(tmp_home: Path, project: str, repo_value: str) -> None:
    state_dir = tmp_home / f".{project}-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "dashboard-runtime.json").write_text(json.dumps({"repo": repo_value}))


@pytest.mark.parametrize(
    "malicious_slug",
    ["--template=/x/y", "owner/name/extra", "owner//name", "owner/ name"],
)
def test_resolve_project_repo_slug_rejects_flag_shaped_values(
    malicious_slug, monkeypatch, tmp_path
):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "unrelated-state"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _plant_repo(tmp_path, "victim", malicious_slug)

    assert resolve_project_repo_slug("victim") is None


def test_resolve_project_repo_slug_still_returns_a_real_slug(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "unrelated-state"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _plant_repo(tmp_path, "victim", "autonomous-agent-7/fulcrumaxe")

    assert resolve_project_repo_slug("victim") == "autonomous-agent-7/fulcrumaxe"
