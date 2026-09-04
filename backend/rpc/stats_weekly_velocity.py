"""RPC handler: stats.weekly_velocity

Returns PRs merged in the last 7 days with a per-day sparkline and
trend vs the prior 7-day window.
"""
from __future__ import annotations

from backend.stats.weekly_velocity import weekly_velocity


def _resolve_repo(project: str | None) -> str | None:
    """Return the GitHub repo slug for *project*, or None for the AF default.

    Reads ~/.{project}-state/dashboard-runtime.json (or project.json) via
    backend.state_paths.for_project so we get the project's actual repo
    rather than falling back to the AF module-level constant.
    """
    if not project:
        return None
    try:
        from backend.state_paths import for_project as _fp  # noqa: PLC0415
        paths = _fp(project)
        if paths.repo and "/" in paths.repo:
            return paths.repo
    except Exception:
        pass
    return None


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
    return weekly_velocity(repo=repo)
