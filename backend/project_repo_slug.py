"""backend/project_repo_slug.py — the requested project's GitHub repo slug.

Why this exists (D#2327 PR-a)
------------------------------
``backend/rpc/stats_weekly_velocity.py`` and
``backend/rpc/stats_cost_per_outcome.py`` each carried a verbatim copy of
this function, one of them with a docstring saying it was a copy. Both
handlers shell out to ``gh`` against the returned slug, and both must
decline rather than fall back to the serving checkout's own repo — so two
copies of the rule deciding that is exactly the shape D#2327 exists to stop.

Scope, precisely: this is the ``str | None`` helper those two RPC handlers
share. It is **not** ``backend/server.py``'s ``_resolve_repo_for_project()``,
which looks similar and is deliberately different: that one returns an
``(owner, name)`` tuple, raises instead of returning ``None``, carries a
richer resolution order including ``state_paths._served_state_dir()``, and
avoids importing ``for_project()`` at all because a long-running server
process may hold a ``sys.modules`` cache predating that function. Those are
two different contracts for two different callers, not one function written
twice — collapsing them would be a behaviour change wearing a refactor's
clothes.
"""

from __future__ import annotations

import re

# Same charset GitHub itself allows in an owner or repo name, and the same
# one backend/_repo_remote.py already validates a derived slug against. A
# bare ``"/" in paths.repo`` check (the previous test here) accepts anything
# with one slash in it, including a slug like ``--template=/x/y`` planted in
# a project's own project.json — both callers below hand this value straight
# to ``gh --repo`` as a list-form subprocess arg, so an unvalidated slug
# becomes a `gh` flag (CWE-88, D#2358).
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def resolve_project_repo_slug(project: str | None) -> "str | None":
    """Return the GitHub ``owner/name`` slug for *project*, or ``None``.

    ``None`` means either "no project was named" (the caller's own default
    applies) or "this project declares no repo" — which now also covers a
    ``repo`` field that fails the slug charset check above; a malformed
    value is treated exactly like a missing one, never returned as-is.
    Callers must tell those apart themselves — both handlers here do it by
    checking ``project`` first, because a *named* project that resolves to
    nothing has to be declined rather than served the serving checkout's
    repo.

    Reads ``~/.<project>-state/dashboard-runtime.json`` (or ``project.json``)
    through :func:`backend.state_paths.for_project`. Never raises.
    """
    if not project:
        return None
    try:
        from backend.state_paths import for_project as _fp  # noqa: PLC0415
        paths = _fp(project)
        if paths.repo and _SLUG_RE.fullmatch(paths.repo):
            return paths.repo
    except Exception:
        pass
    return None
