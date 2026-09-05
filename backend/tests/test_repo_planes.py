"""Tests for backend/_repo_planes.py — the code plane and the Discussion plane.

Run with:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" \
        python -m pytest backend/tests/test_repo_planes.py -v

Two properties are under test here.

The first is inertness: with neither "code_repo" nor "discussion_repo" set —
the state of every tree today — CODE_REPO and DISCUSSION_REPO must both equal
REPO, so introducing the vocabulary retargets no call site.

The second is the asymmetry that is not inert. CODE_REPO keeps REPO's
origin-remote fallback, which is correct for a clone of the public repo: its
origin is its code repo. DISCUSSION_REPO is config-only and is legitimately
empty in a fork with no private twin — empty must be a valid answer rather than
an error, and must never fall back to a hard-coded slug (D#1870).

Every case runs against tmp_path except
test_live_module_constants_hold_in_any_checkout, which asserts against the real
import on purpose and is written to hold in a fork as well as here — see its
docstring for why that distinction is the whole point of this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend._repo_planes import (  # noqa: E402
    resolve_code_repo,
    resolve_discussion_repo,
)


def _tree(root: Path, project_json: dict | None) -> Path:
    """Make a repo root, optionally with .autonomous-team/project.json."""
    team = root / ".autonomous-team"
    team.mkdir(parents=True, exist_ok=True)
    if project_json is not None:
        (team / "project.json").write_text(json.dumps(project_json))
    return root


def _empty_state_dir(tmp_path: Path) -> Path:
    """A state dir with no project.json, so the repo-root file is what counts."""
    sd = tmp_path / "state"
    sd.mkdir(parents=True, exist_ok=True)
    return sd


@pytest.fixture(autouse=True)
def _no_env_repo(monkeypatch):
    """Clear AUTONOMOUS_TEAM_REPO unless a test sets it deliberately."""
    monkeypatch.delenv("AUTONOMOUS_TEAM_REPO", raising=False)


# --- The inert case ---------------------------------------------------------


def test_code_repo_is_the_identity_when_key_absent(tmp_path):
    root = _tree(tmp_path / "repo", {"repo": "owner/only"})
    assert (
        resolve_code_repo("owner/only", root, _empty_state_dir(tmp_path))
        == "owner/only"
    )


def test_code_repo_passes_through_an_origin_derived_slug(tmp_path):
    """A public clone ships no .autonomous-team/ and still gets its own slug.

    REPO resolves that from the origin remote (#2341); CODE_REPO must inherit
    it rather than re-deriving or dropping it.
    """
    root = tmp_path / "fork"
    root.mkdir()
    resolved = resolve_code_repo(
        "adopter/their-fork", root, _empty_state_dir(tmp_path)
    )
    assert resolved == "adopter/their-fork"


def test_discussion_repo_falls_back_to_repo_when_key_absent(tmp_path):
    root = _tree(tmp_path / "repo", {"repo": "owner/only"})
    assert (
        resolve_discussion_repo(root, _empty_state_dir(tmp_path)) == "owner/only"
    )


def test_both_planes_agree_when_neither_key_is_set(tmp_path):
    """The property the whole change rests on."""
    root = _tree(tmp_path / "repo", {"repo": "owner/only"})
    sd = _empty_state_dir(tmp_path)
    assert resolve_code_repo("owner/only", root, sd) == resolve_discussion_repo(
        root, sd
    )


def test_live_module_constants_hold_in_any_checkout():
    """The contract every importing module actually sees, in any checkout.

    Asserted against the real import rather than a fixture, because the live
    constants are what ship. That makes this the one test in the file with an
    environment dependency, so it has to state the invariant that holds
    everywhere rather than the one that happens to hold here.

    `assert DISCUSSION_REPO == REPO` was the wrong invariant. This file ships
    in the open-source export (open-source/MANIFEST.md:437) and
    .autonomous-team/ does not (MANIFEST.md:50,121), so an adopter runs it in a
    checkout whose Discussion plane is genuinely empty: it passed here and
    failed with `assert '' == 'adopter/their-fork'` for every fork. That is the
    D#2340 shape — the defect this whole vocabulary exists to prevent.

    Sharper than "it fails in a fork": it directly contradicted
    test_discussion_repo_empty_is_not_an_error a few cases above, which asserts
    an empty Discussion plane is legitimate. One test called empty valid, the
    other called it a failure, and *only the environment decided which one you
    saw*. Two tests in one file cannot disagree about the contract — if you
    find yourself tightening this one, check that one first.
    """
    from backend._repo import CODE_REPO, DISCUSSION_REPO, REPO

    # Holds in every checkout: nothing has cut over, so the code plane is REPO.
    assert CODE_REPO == REPO
    assert "/" in REPO

    if DISCUSSION_REPO:
        # A configured tree (ours): both planes still name the same repo.
        assert DISCUSSION_REPO == REPO
    else:
        # A fork with no private twin. Empty is the correct answer, and the
        # import above completing at all is the assertion that matters: an
        # absent Discussion plane must not raise.
        assert DISCUSSION_REPO == ""


# --- The configured case ----------------------------------------------------


def test_code_repo_key_wins(tmp_path):
    root = _tree(
        tmp_path / "repo", {"repo": "owner/private", "code_repo": "owner/public"}
    )
    assert (
        resolve_code_repo("owner/private", root, _empty_state_dir(tmp_path))
        == "owner/public"
    )


def test_discussion_repo_key_wins(tmp_path):
    root = _tree(
        tmp_path / "repo",
        {"repo": "owner/public", "discussion_repo": "owner/private"},
    )
    assert (
        resolve_discussion_repo(root, _empty_state_dir(tmp_path)) == "owner/private"
    )


def test_state_dir_project_json_outranks_repo_root(tmp_path):
    """Same precedence _load_repo uses for "repo" — state dir first."""
    root = _tree(tmp_path / "repo", {"code_repo": "owner/from-repo-root"})
    sd = _empty_state_dir(tmp_path)
    (sd / "project.json").write_text(json.dumps({"code_repo": "owner/from-state"}))
    assert resolve_code_repo("owner/x", root, sd) == "owner/from-state"


def test_planes_can_differ(tmp_path):
    root = _tree(
        tmp_path / "repo",
        {
            "repo": "owner/legacy",
            "code_repo": "owner/public",
            "discussion_repo": "owner/private",
        },
    )
    sd = _empty_state_dir(tmp_path)
    assert resolve_code_repo("owner/legacy", root, sd) == "owner/public"
    assert resolve_discussion_repo(root, sd) == "owner/private"


# --- The asymmetry ----------------------------------------------------------


def test_discussion_repo_empty_is_not_an_error(tmp_path):
    """A fork has no private twin. Return "", do not raise.

    Callers must be able to ask "is there a Discussion plane?" and get an
    answer instead of an exception — a fork that crashes on startup is as
    broken as one that silently reads our Discussions.
    """
    root = tmp_path / "fork"
    root.mkdir()
    assert resolve_discussion_repo(root, _empty_state_dir(tmp_path)) == ""


def test_discussion_repo_never_inherits_a_hardcoded_slug(tmp_path):
    """The D#1870 hazard, asserted rather than assumed.

    An unconfigured fork must not end up pointed at this project's own repo,
    and must not pick up the origin remote either — a fork's origin is its code
    repo, never its Discussion repo.
    """
    root = tmp_path / "fork"
    root.mkdir()
    # A well-formed origin remote that CODE_REPO would happily use.
    git_dir = root / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:adopter/their-fork.git\n'
    )
    resolved = resolve_discussion_repo(root, _empty_state_dir(tmp_path))
    assert resolved == ""
    assert "fulcrumaxe" not in resolved
    assert "adopter" not in resolved


def test_env_repo_still_reaches_the_discussion_plane(tmp_path, monkeypatch):
    """Config-only means "no origin remote", not "no env override"."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_REPO", "owner/from-env")
    root = tmp_path / "fork"
    root.mkdir()
    assert (
        resolve_discussion_repo(root, _empty_state_dir(tmp_path)) == "owner/from-env"
    )


# --- Precedence: AUTONOMOUS_TEAM_REPO still outranks the new keys, in Python -
#
# backend/_repo.py documents AUTONOMOUS_TEAM_REPO as "highest priority —
# explicit override always wins". These pin that it still does once the new keys
# exist, which is the only point at which it could quietly stop being true.
#
# Scope, because it is narrower than it looks and the narrowness is the point:
# this covers backend/_repo.py and nothing else. scripts/lib/repo-resolve.sh is
# config-first by its own documented order, and ts-backend/src/config/repo.ts is
# config-first frozen under D#1632 and never reads AUTONOMOUS_TEAM_REPO at all —
# it reads GH_REPO / _REPO. So the env var is not a system-wide lever and must
# not be described as one. The other two halves of that claim are pinned
# executably, one per resolver:
#   bash — tests/test_repo_resolve_planes.py::test_config_keys_outrank_the_env_var_here
#   ts   — ts-backend/tests/config/repo-planes.test.ts,
#          "ignores AUTONOMOUS_TEAM_REPO entirely"


def test_env_override_outranks_code_repo_key(tmp_path, monkeypatch):
    """An operator setting the env var must retarget Python's code plane too.

    Otherwise the override covers one of Python's two planes and not the other,
    which is a worse failure than not covering either: it looks like it worked.
    """
    monkeypatch.setenv("AUTONOMOUS_TEAM_REPO", "owner/kill-switch")
    root = _tree(
        tmp_path / "repo", {"repo": "owner/private", "code_repo": "owner/public"}
    )
    # REPO is resolved env-first upstream, so this is what _repo.py hands us.
    assert (
        resolve_code_repo("owner/kill-switch", root, _empty_state_dir(tmp_path))
        == "owner/kill-switch"
    )


def test_env_override_outranks_discussion_repo_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TEAM_REPO", "owner/kill-switch")
    root = _tree(
        tmp_path / "repo",
        {"repo": "owner/public", "discussion_repo": "owner/private"},
    )
    assert (
        resolve_discussion_repo(root, _empty_state_dir(tmp_path))
        == "owner/kill-switch"
    )


def test_env_override_collapses_pythons_two_planes(tmp_path, monkeypatch):
    """Setting the env var points *Python's* code and Discussion planes at it.

    Read the scope note above before reaching for this in an incident. This is
    not a whole-system revert, and an earlier version of this docstring said it
    was. Measured against a full cutover (keys set in both config files) plus
    AUTONOMOUS_TEAM_REPO: Python's three values move, bash does not move, and
    TypeScript does not move and never could. Four of twelve resolved values.

    An emergency lever that silently does a third of its job is worse than no
    lever, because the operator stops looking — the same half-succeeds-quietly
    shape as a half cutover, and as the empty-Discussion-plane bug this file
    was fixed for.

    The actual whole-system revert is to revert the cutover config change in
    both .autonomous-team/config.json and .autonomous-team/project.json, which
    is where the two planes are named in the first place.
    """
    monkeypatch.setenv("AUTONOMOUS_TEAM_REPO", "owner/kill-switch")
    root = _tree(
        tmp_path / "repo",
        {
            "repo": "owner/legacy",
            "code_repo": "owner/public",
            "discussion_repo": "owner/private",
        },
    )
    sd = _empty_state_dir(tmp_path)
    assert resolve_code_repo("owner/kill-switch", root, sd) == resolve_discussion_repo(
        root, sd
    )


# --- Unusable values are "not configured", not a crash ----------------------


@pytest.mark.parametrize(
    "value", ["", 42, None, {"nested": "no"}, ["a"]], ids=lambda v: repr(v)[:12]
)
def test_unusable_code_repo_value_falls_back(tmp_path, value):
    root = _tree(tmp_path / "repo", {"repo": "owner/r", "code_repo": value})
    assert resolve_code_repo("owner/r", root, _empty_state_dir(tmp_path)) == "owner/r"


def test_malformed_project_json_falls_back(tmp_path):
    root = tmp_path / "repo"
    (root / ".autonomous-team").mkdir(parents=True)
    (root / ".autonomous-team" / "project.json").write_text("{not json")
    sd = _empty_state_dir(tmp_path)
    assert resolve_code_repo("owner/r", root, sd) == "owner/r"
    assert resolve_discussion_repo(root, sd) == ""


def test_project_json_that_is_not_an_object_falls_back(tmp_path):
    root = tmp_path / "repo"
    (root / ".autonomous-team").mkdir(parents=True)
    (root / ".autonomous-team" / "project.json").write_text('["a", "b"]')
    assert (
        resolve_code_repo("owner/r", root, _empty_state_dir(tmp_path)) == "owner/r"
    )
