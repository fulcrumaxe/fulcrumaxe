"""RPC handler: stats.weekly_velocity

Returns PRs merged in the last 7 days with a per-day sparkline and
trend vs the prior 7-day window.
"""
from __future__ import annotations

from backend.project_repo_slug import resolve_project_repo_slug as _resolve_repo
from backend.stats.weekly_velocity import weekly_velocity


def handle(params: dict) -> dict:
    """Return weekly velocity data for the requested project.

    Params: {"project": str}  (omit or None for AF default)

    Response shape:
        {
            "applicable":   bool,  # False → project has no PRs in 14d; show empty-state
            "total":        int,
            "by_day":       [{"date": "YYYY-MM-DD", "count": int}, ...7 entries],
            "window_start": "YYYY-MM-DDTHH:MM:SSZ",
            "window_end":   "YYYY-MM-DDTHH:MM:SSZ",
            "prev_total":   int,
            "trend_pct":    int,
        }
    """
    project = params.get("project") or None
    repo = _resolve_repo(project)
    if project and repo is None:
        # D#2327 PR-a: weekly_velocity() falls back to the module-level
        # _REPO constant when repo is None -- the serving checkout's own
        # repo. Passing None here for a *named* project served that repo's
        # merged-PR counts under the requested project's name. Decline.
        from backend.rpc_project_scope import UnresolvableProjectError  # noqa: PLC0415

        raise UnresolvableProjectError(
            f"stats.weekly_velocity: project {project!r} resolves to no "
            "GitHub repo slug (no 'repo' field in its dashboard-runtime.json "
            "or project.json) -- declining rather than counting the serving "
            "checkout's merged PRs under this project's name"
        )
    return weekly_velocity(repo=repo)
