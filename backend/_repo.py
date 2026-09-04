"""backend/_repo.py — canonical project repo resolver.

Single source of truth for the REPO, REPO_OWNER, REPO_NAME constants
used across backend modules that make GitHub API calls.

Resolution order:
  1. AUTONOMOUS_TEAM_REPO environment variable (highest priority)
  2. <state_dir>/project.json "repo" field, where state_dir is
     AUTONOMOUS_TEAM_STATE_DIR env var (default: ~/.fulcrumaxe-state)
  3. Repo-root .autonomous-team/project.json "repo" field
  4. The origin remote in <repo_root>/.git/config, parsed as INI (D#2340).
     A fork's origin is the adopter's own repo, so this resolves each clone
     to itself; it is how a fresh clone of the open-source export, which
     ships no .autonomous-team/, gets a slug at all.
  5. Fail loudly. There is deliberately no hard-coded slug fallback here:
     .autonomous-team/ never ships in the open-source export (see
     open-source/MANIFEST.md), so a forked adopter with none of the above
     configured gets an actionable error instead of silently inheriting
     this project's own repo slug. This repo provisions step 3 via a
     committed .autonomous-team/project.json, so REPO resolves cleanly here
     and steps 4 and 5 are never reached in our own runtime (D#1870).

Every backend module that needs the repo slug imports from here so it's
automatically portable to forked projects — 32 modules as of D#1879
(`grep -rl 'from backend._repo import' backend --include=*.py`, excluding
this file and backend/tests/). That count will drift as modules are added;
if you need the current number, re-run the grep rather than trust this
docstring — an earlier version of this line said 16, which was already
wrong by the time it was cited in a review.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from backend._repo_remote import repo_slug_from_git_config


def _read_project_json(path: Path) -> str | None:
    """Return the 'repo' field from *path* if readable, else None."""
    try:
        with path.open() as f:
            data = json.load(f)
        repo = data.get("repo")
        return repo if repo else None
    except (OSError, ValueError):
        return None


def _load_repo() -> str:
    """Read repo slug using env-first precedence.

    1. AUTONOMOUS_TEAM_REPO env var — explicit override always wins.
    2. <AUTONOMOUS_TEAM_STATE_DIR>/project.json → "repo" field — lets a
       per-project state dir (e.g. <home>/.projectb-state) declare its repo
       without touching the source tree.
    3. Repo-root .autonomous-team/project.json — backwards-compatible
       fallback for single-project setups.
    4. The origin remote in .git/config — the only step that works in a
       clone of the open-source export, which ships no .autonomous-team/.
    5. Nothing resolved — raise. See module docstring for why this doesn't
       default to a hard-coded slug.
    """
    # 1. Explicit env override — highest priority.
    env_repo = os.environ.get("AUTONOMOUS_TEAM_REPO")
    if env_repo:
        return env_repo

    # 2. State-dir project.json — supports AUTONOMOUS_TEAM_STATE_DIR override.
    state_dir = Path(
        os.environ.get(
            "AUTONOMOUS_TEAM_STATE_DIR",
            str(Path.home() / ".fulcrumaxe-state"),
        )
    )
    repo = _read_project_json(state_dir / "project.json")
    if repo:
        return repo

    # 3. Repo-root .autonomous-team/project.json — backwards compat.
    repo_root = Path(__file__).resolve().parent.parent
    repo = _read_project_json(repo_root / ".autonomous-team" / "project.json")
    if repo:
        return repo

    # 4. The origin remote. Never raises and never shells out; returns None
    # for anything that isn't a well-formed OWNER/NAME.
    repo = repo_slug_from_git_config(repo_root)
    if repo:
        return repo

    # 5. Nothing resolved — fail loudly rather than default to a repo the
    # caller may not own. See module docstring (D#1870).
    raise RuntimeError(
        "backend._repo: could not resolve a repo slug. Set the "
        "AUTONOMOUS_TEAM_REPO environment variable, or add a \"repo\" "
        "field to <AUTONOMOUS_TEAM_STATE_DIR>/project.json or "
        ".autonomous-team/project.json."
    )


REPO: str = _load_repo()
REPO_OWNER: str
REPO_NAME: str
REPO_OWNER, REPO_NAME = REPO.split("/", 1)


def _project_transcript_slug() -> str:
    """Claude Code encodes a project's absolute repo path as a directory slug
    under ~/.claude/projects/ by replacing every "/" with "-"
    (e.g. "/srv/checkouts/myrepo" -> "-srv-checkouts-myrepo").

    Computed from this file's actual location so it's correct for any clone,
    rather than hard-coding the slug for this specific checkout path.
    """
    repo_root = Path(__file__).resolve().parent.parent
    return str(repo_root).replace("/", "-")


PROJECT_TRANSCRIPT_SLUG: str = _project_transcript_slug()

__all__ = ["REPO", "REPO_OWNER", "REPO_NAME", "PROJECT_TRANSCRIPT_SLUG"]
