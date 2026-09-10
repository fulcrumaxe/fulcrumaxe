"""backend/_repo_remote.py — derive an OWNER/NAME repo slug from .git/config.

A fallback resolution step for backend/_repo.py (and backend/spawn_templates.py,
which carries its own copy of the same resolution order). It exists because
.autonomous-team/ deliberately never ships in the open-source export, so a
fresh clone of the export has no project.json to read and every backend module
that resolves the slug at import time raises (D#2340).

Deriving the slug from origin is not just convenient, it is the *right* answer
for a fork: a fork's origin is the adopter's own repo, so each clone resolves
to itself instead of inheriting ours. That strengthens the D#1870 property the
_repo.py docstring is protecting rather than weakening it.

D#2443: the original version of this module read ``.git/config`` directly,
which only works when ``.git`` is a directory — the main checkout, or a
plain ``git clone``. Every *linked* worktree (``git worktree add``, which is
how every agent worktree is created) has a ``.git`` that is a *file*
containing a single ``gitdir: <path>`` line, and that ``<path>`` is a
per-worktree admin directory (``.git/worktrees/<id>``) that holds no
``config`` of its own — the real config lives back in the main checkout's
``.git``. ``repo_slug_from_git_config`` now follows that indirection: the
same ``/.git/worktrees/`` marker that ``hooks/repo_root.py``'s
``derive_repo_root_from`` already uses to find a worktree's main root is
reused here (see that module for the reasoning) to locate the main ``.git``
directory and read config from there instead.

Two properties this module guarantees to its callers:

  * It never raises. A missing .git, a worktree whose .git is a file rather
    than a directory, a checkout with no origin, an unreadable or malformed
    config — all return None so the caller falls through to its own error.
  * It never shells out and never touches the network. .git/config is a plain
    INI file; configparser reads it directly.
"""

from __future__ import annotations

import re
from pathlib import Path

# OWNER and NAME as GitHub actually allows them: no "/" (that's the
# separator), no whitespace, no quoting or fragment punctuation. Anything
# outside this charset means the value isn't a clean OWNER/NAME and should
# fall through to None rather than being guessed at.
_VALID_SLUG_PART = re.compile(r"[A-Za-z0-9._-]+")

# Marker embedded in the ``gitdir:`` line of a linked worktree's ``.git``
# file — e.g. ``gitdir: /main/checkout/.git/worktrees/agent-abc123``. Same
# constant hooks/repo_root.py uses for the same purpose (finding the main
# checkout from a worktree copy); duplicated here rather than imported
# because backend/ must not import hooks/ (see that module's docstring).
_WORKTREE_GITDIR_MARKER = "/.git/worktrees/"


def _worktree_config_path(git_entry: Path) -> Path | None:
    """Return the main checkout's ``.git/config`` path for a worktree's
    ``.git`` *file*, or None if it doesn't look like a linked worktree.

    *git_entry* is the ``.git`` file itself (already confirmed to be a file,
    not a directory). Reads its single ``gitdir: <path>`` line, resolves a
    relative ``<path>`` against *git_entry*'s own parent directory (never
    against the process's cwd — a worktree can be entered from anywhere),
    and — only when the resolved path contains the
    ``/.git/worktrees/<id>`` marker git itself writes — splits on that
    marker to recover the main checkout root. A marker-less target (e.g. a
    submodule's ``.git`` file, which points at ``.git/modules/<name>``
    instead) is a layout this function doesn't know how to read config for;
    returning None here is a deliberate "no guess" rather than a wrong one.
    """
    try:
        text = git_entry.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("gitdir:"):
            continue
        gitdir = line[len("gitdir:") :].strip()
        if not gitdir:
            return None
        gitdir_path = Path(gitdir)
        if not gitdir_path.is_absolute():
            gitdir_path = git_entry.parent / gitdir_path
        gitdir_str = str(gitdir_path)
        if _WORKTREE_GITDIR_MARKER not in gitdir_str:
            return None
        main_root = Path(gitdir_str.split(_WORKTREE_GITDIR_MARKER, 1)[0])
        return main_root / ".git" / "config"

    # No "gitdir:" line found at all — malformed .git file.
    return None


def _slug_from_url(url: str) -> str | None:
    """Return OWNER/NAME from a remote URL, or None if it isn't one.

    Handles the two forms git writes for GitHub remotes —
    ``https://github.com/OWNER/NAME.git`` and ``git@github.com:OWNER/NAME.git``
    — plus the ``ssh://`` spelling of the latter. Anything else (a local path
    clone, a URL with a deeper path, a hostname on its own) returns None: a
    wrong slug is worse than no slug, since the caller's error message tells
    the operator exactly what to configure.
    """
    url = url.strip()
    if not url:
        return None

    if "://" in url:
        # scheme://[user@]host/OWNER/NAME[.git]
        rest = url.split("://", 1)[1]
        if "/" not in rest:
            return None
        path = rest.split("/", 1)[1]
    elif "@" in url and ":" in url:
        # scp-like: [user@]host:OWNER/NAME[.git]
        path = url.split(":", 1)[1]
    else:
        return None

    # Split first, strip ".git" only off the NAME half. Stripping a trailing
    # ".git" (or a trailing "/") off the whole path *before* splitting hides
    # a stray character sitting right after it — "OWNER/NAME.git/",
    # 'OWNER/NAME"', "OWNER/NAME#frag.git" all used to slip through that way.
    # Left alone, the count("/") check below now catches the trailing-slash
    # case on its own, and the charset check catches the rest.
    if path.count("/") != 1 or any(c.isspace() for c in path):
        return None
    owner, _, name = path.partition("/")
    if not owner or not name:
        return None

    if name.endswith(".git"):
        name = name[: -len(".git")]

    if not _VALID_SLUG_PART.fullmatch(owner) or not _VALID_SLUG_PART.fullmatch(name):
        return None
    return f"{owner}/{name}"


def repo_slug_from_git_config(repo_root: Path | str) -> str | None:
    """Return the OWNER/NAME slug of ``origin`` under *repo_root*, else None.

    Handles both shapes ``.git`` can take: a directory (the main checkout,
    or a plain ``git clone``) and a file (a linked worktree — see the
    module docstring and :func:`_worktree_config_path`).
    """
    # Imported here rather than at module scope: this step is unreachable in
    # any checkout that has a project.json (which includes this one), so the
    # import cost should only be paid by the clones that actually need it.
    import configparser  # noqa: PLC0415

    git_entry = Path(repo_root) / ".git"
    if git_entry.is_dir():
        config_path = git_entry / "config"
    elif git_entry.is_file():
        config_path = _worktree_config_path(git_entry)
        if config_path is None:
            return None
    else:
        return None

    try:
        raw = config_path.read_text()
    except (OSError, UnicodeDecodeError):
        return None

    # RawConfigParser: git config values may contain '%' (percent-encoded URLs),
    # which ConfigParser would try to interpolate. strict=False: git allows
    # duplicate keys in a section, e.g. two url= lines under one remote.
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read_string(raw)
        url = parser.get('remote "origin"', "url")
    except (configparser.Error, ValueError):
        return None

    return _slug_from_url(url)


__all__ = ["repo_slug_from_git_config"]
