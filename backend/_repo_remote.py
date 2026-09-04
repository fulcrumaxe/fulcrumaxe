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

Two properties this module guarantees to its callers:

  * It never raises. A missing .git, a worktree whose .git is a file rather
    than a directory, a checkout with no origin, an unreadable or malformed
    config — all return None so the caller falls through to its own error.
  * It never shells out and never touches the network. .git/config is a plain
    INI file; configparser reads it directly.
"""

from __future__ import annotations

from pathlib import Path


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

    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")

    if path.count("/") != 1 or any(c.isspace() for c in path):
        return None
    owner, _, name = path.partition("/")
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def repo_slug_from_git_config(repo_root: Path | str) -> str | None:
    """Return the OWNER/NAME slug of ``origin`` under *repo_root*, else None."""
    # Imported here rather than at module scope: this step is unreachable in
    # any checkout that has a project.json (which includes this one), so the
    # import cost should only be paid by the clones that actually need it.
    import configparser  # noqa: PLC0415

    config_path = Path(repo_root) / ".git" / "config"
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
