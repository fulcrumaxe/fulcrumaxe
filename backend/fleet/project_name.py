"""backend/fleet/project_name.py — the one resolver for the fleet.db project-name key.

D#2314 finding D1: the read side (backend/api.py, via ``count_project()``)
queried fleet.db with ``projects.json``'s display name ("fulcrumaxe"), while
the write side (scripts/pre-spawn-check.sh) queried
``.autonomous-team/config.json`` for a ``project_name`` key that did not
exist, and silently fell back to a hard-coded ``"autonomous-forever"``.
Result: every real spawn registered under one name, every read queried
another, and ``activeAgents``/liveness read zero on a project working at
full tilt.

The fix is a single resolver, called by both sides, so they can never
disagree again. Bash callers (``pre-spawn-check.sh``, ``post-agent-hook.sh``)
invoke this module's CLI; ``backend/api.py`` imports ``resolve_project_name``
directly.

Resolution order:
  1. ``.autonomous-team/config.json``'s ``project_name`` key, if present.
  2. Derived from the same file's ``repo`` key (``"owner/name" -> "name"``),
     when ``project_name`` is absent.
  3. Derived from the origin remote (``"owner/name" -> "name"``), but only
     when config.json is missing entirely — which is the case in a clone of
     the open-source export, where ``.autonomous-team/`` is excluded and
     every spawn was hard-blocked at this call (D#2340). When the file is
     present its keys still decide, unchanged.
  4. Raise ``ProjectNameUnresolvable`` — a loud failure. A silent mis-key
     (the previous ``"autonomous-forever"`` fallback) is explicitly not
     acceptable per D#2314's Spec item 2: it is what caused the bug.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from backend._repo_remote import repo_slug_from_git_config


class ProjectNameUnresolvable(RuntimeError):
    """Raised when no project name can be resolved from config.json."""


def _default_repo_root() -> Path:
    # backend/fleet/project_name.py -> backend/fleet -> backend -> <repo root>
    return Path(__file__).resolve().parents[2]


def resolve_project_name(repo_root: str | Path | None = None) -> str:
    """Return the fleet.db ``project_name`` key for the project at *repo_root*.

    *repo_root* defaults to this checkout's own root. Raises
    ``ProjectNameUnresolvable`` (never returns a silently-wrong default) when
    ``.autonomous-team/config.json`` is not a JSON object, carries neither a
    ``project_name`` nor a usable ``repo`` key, or is missing with no usable
    origin remote to derive a name from.
    """
    root = Path(repo_root) if repo_root is not None else _default_repo_root()
    config_path = root / ".autonomous-team" / "config.json"

    try:
        raw = config_path.read_text()
    except OSError as exc:
        # No config.json at all — the open-source export ships none (D#2340).
        # Derive from origin instead of blocking the spawn. This resolver is
        # still the single one both sides call, so the read side
        # (backend/api.py) and the write side (scripts/pre-spawn-check.sh)
        # cannot disagree, which is the D#2314 property that matters.
        derived = repo_slug_from_git_config(root)
        if derived:
            return derived.rsplit("/", 1)[-1]
        raise ProjectNameUnresolvable(f"cannot read {config_path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectNameUnresolvable(f"{config_path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ProjectNameUnresolvable(f"{config_path} does not contain a JSON object")

    name = data.get("project_name")
    if isinstance(name, str) and name.strip():
        return name.strip()

    repo = data.get("repo")
    if isinstance(repo, str) and "/" in repo:
        derived = repo.rsplit("/", 1)[-1].strip()
        if derived:
            return derived

    raise ProjectNameUnresolvable(
        f"{config_path} has neither a usable 'project_name' nor a usable "
        "'repo' key — cannot resolve the fleet.db project name"
    )


def _main(argv: list[str]) -> int:
    repo_root = argv[0] if argv else None
    try:
        print(resolve_project_name(repo_root))
        return 0
    except ProjectNameUnresolvable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
