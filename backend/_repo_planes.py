"""backend/_repo_planes.py — the code plane and the Discussion plane.

Code, PRs and CI are moving to a public repo while Discussions and Issues stay
in the private one. Two names, one value: until the cutover both planes resolve
to the same slug, so introducing the vocabulary here changes no behaviour. Call
sites get reclassified one subsystem at a time afterwards.

Two optional keys drive this, read from the same project.json files
``backend/_repo.py`` already reads for ``repo``:

    "code_repo"        the repo that holds commits, PRs and CI.
    "discussion_repo"  the repo that holds Discussions and Issues.

Neither key is set in this tree. With both absent ``CODE_REPO`` equals ``REPO``
and ``DISCUSSION_REPO`` equals ``REPO``, which is what makes adding them inert.

The asymmetry between the two resolvers is the part that is not a no-op, and it
is deliberate:

``resolve_code_repo`` falls back to the fully-resolved ``REPO``, which includes
the origin-remote step added by #2341. That step is exactly right here: a clone
of the public repo ships no ``.autonomous-team/`` and still resolves its own
slug correctly, because its origin *is* its code repo.

``resolve_discussion_repo`` deliberately does **not** use the origin-remote
step, and returns the empty string when nothing is configured. A fork has no
private twin, so "no Discussion plane" is a legitimate state rather than an
error — callers must branch on the empty string and skip Discussion work, not
raise. It must also never fall back to a hard-coded slug: a fork inheriting our
Discussion repo is the precise hazard ``backend/_repo.py``'s docstring guards
against under D#1870, and the reason shipping a default ``project.json`` was
rejected during D#2340. Empty is the safe answer in both directions.

Note the split this does not paper over: the three resolvers in this repo read
different files. ``scripts/lib/repo-resolve.sh`` and
``ts-backend/src/config/repo.ts`` read ``.autonomous-team/config.json``; this
one reads ``project.json``. Each resolver here reads the new keys from its own
existing source rather than reaching across, so precedence stays internally
consistent within each — which means the cutover has to set the key in **both**
files. That is a pre-existing divergence, surfaced rather than fixed, and it is
the sharp edge for whoever performs the cutover: setting the key in
``config.json`` alone moves bash and TypeScript but not Python, which is a
silent two-thirds retarget rather than a loud failure.

The same "obey your own resolver" rule settles precedence, and the three
resolvers genuinely differ there:

* ``backend/_repo.py`` documents ``AUTONOMOUS_TEAM_REPO`` as "highest priority —
  explicit override always wins", so both accessors here check it **before** the
  new config keys. Setting it points both of *this module's* planes at one repo.
* ``scripts/lib/repo-resolve.sh`` and ``ts-backend/src/config/repo.ts`` document
  the *opposite* order — ``config.json`` outranks the environment, and in the TS
  case that order is frozen under D#1632. Their accessors match their own files.
  ``repo.ts`` does not read ``AUTONOMOUS_TEAM_REPO`` at any precedence; it reads
  ``GH_REPO`` and ``_REPO``.

``AUTONOMOUS_TEAM_REPO`` is therefore **not** a system-wide revert, and must not
be documented as one. Against a full cutover it moves Python and leaves bash and
TypeScript where they were — measured, four of twelve resolved values. An
emergency lever that silently does part of its job is worse than no lever,
because the operator stops looking. The whole-system revert is to revert the
cutover config change in **both** ``config.json`` and ``project.json``.

So the accessors are not uniform across languages, deliberately: each matches
the resolver it lives in. Making them uniform would mean breaking one of the two
documented contracts, and silently changing which lever an operator can trust is
worse than the asymmetry. If a per-plane environment override is ever wanted,
add ``AUTONOMOUS_TEAM_CODE_REPO`` / ``AUTONOMOUS_TEAM_DISCUSSION_REPO`` then —
one variable cannot name two repos, and there is no call site needing it yet.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_PROJECT_JSON = "project.json"
_DEFAULT_STATE_DIR = ".autonomous-forever-state"


def _state_dir() -> Path:
    """The runtime state directory, resolved the same way ``_repo`` does.

    Read from the environment directly rather than through
    ``backend.state_paths``: that module raises under pytest when the variable
    is unset, and repo-slug resolution must stay importable everywhere.
    """
    return Path(
        os.environ.get(
            "AUTONOMOUS_TEAM_STATE_DIR", str(Path.home() / _DEFAULT_STATE_DIR)
        )
    )


def _read_field(path: Path, key: str) -> str | None:
    """Return *key* from the JSON object at *path*, or None.

    A missing file, unreadable file, malformed JSON, missing key, non-string
    value and empty string all collapse to None — every one of them means "not
    configured", which is a normal state here, not a failure.
    """
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _project_json_field(
    key: str, repo_root: Path, state_dir: Path | None = None
) -> str | None:
    """Read *key* from the state-dir project.json, then the repo-root one.

    Same two files and same order ``backend._repo._load_repo`` uses for the
    ``repo`` field, so a new key cannot end up with a different precedence than
    the one it falls back to.
    """
    sd = _state_dir() if state_dir is None else state_dir
    for candidate in (
        sd / _PROJECT_JSON,
        repo_root / ".autonomous-team" / _PROJECT_JSON,
    ):
        value = _read_field(candidate, key)
        if value:
            return value
    return None


def resolve_code_repo(
    repo: str, repo_root: Path, state_dir: Path | None = None
) -> str:
    """The repo that holds commits, PRs and CI.

    *repo* is the fully-resolved slug from ``backend._repo.REPO``, origin-remote
    fallback included. Returns it unchanged unless ``code_repo`` is configured,
    so this is the identity function on every tree that has not cut over.
    """
    if os.environ.get("AUTONOMOUS_TEAM_REPO"):
        # Env wins, per _repo.py's documented precedence. REPO already *is* the
        # env value at this point, so returning it unchanged is the override.
        return repo
    return _project_json_field("code_repo", repo_root, state_dir) or repo


def resolve_discussion_repo(repo_root: Path, state_dir: Path | None = None) -> str:
    """The repo that holds Discussions and Issues, or "" when there is none.

    Config-only by design — no origin-remote step, no hard-coded default. An
    empty return is a legitimate answer meaning "this checkout has no Discussion
    plane", and callers must treat it as such rather than as an error.
    """
    env_repo = os.environ.get("AUTONOMOUS_TEAM_REPO")
    if env_repo:
        return env_repo

    configured = _project_json_field("discussion_repo", repo_root, state_dir)
    if configured:
        return configured

    # Same config-only chain ``repo`` uses, minus the origin-remote step.
    return _project_json_field("repo", repo_root, state_dir) or ""


__all__ = ["resolve_code_repo", "resolve_discussion_repo"]
