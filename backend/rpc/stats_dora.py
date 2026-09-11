"""RPC handler: stats.dora

Returns DORA metrics (deploy frequency, lead time, change failure rate) and
KPI metrics (velocity, cycle time) by delegating to
analytics_engineer.compute_snapshot — no independent recomputation.

change_failure_rate_pct is passed through verbatim as a string (e.g. "n/a"
when no bug data is available) rather than coerced to a number.

Project scoping (D#2518)
-------------------------
This handler was UNSCOPABLE: every source compute_snapshot() read was bound
to the serving checkout at import (analytics_engineer._RELEASES_DIR and
kpi_engine.REGISTRY, both module constants built from
Path(__file__).resolve().parent.parent, and analytics_engineer's
module-level REPO, shelled straight to `gh api graphql`). A per-request
STATS_DB_PATH override reached none of it.

Now SCOPED: analytics_engineer.compute_snapshot() takes an explicit
project_root (releases dir + registry.json resolve under it) and repo
(threaded into release_manager.compute_dora_snapshot() for lead time and
into analytics_engineer._compute_cfr() for change-failure-rate), both
resolved here, per request, from the requested project. The repo slug comes
from backend.project_repo_slug.resolve_project_repo_slug(), matching
stats.weekly_velocity and stats.cost_per_outcome. When a named project
declares no repo, this handler declines with UnresolvableProjectError
rather than answering with the serving checkout's DORA/CFR numbers under
that project's name.
"""
from __future__ import annotations


def handle(params: dict) -> dict:
    """Return DORA + KPI snapshot for the dashboard.

    Params: {"project": str}  (omit or None for the serving checkout)

    Response shape:
        {
            "applicable":                bool,  # False → no release/KPI data yet
            "deploy_frequency_per_day":  float,
            "lead_time_minutes_p50":     float,
            "change_failure_rate_pct":   str,   # "n/a" or numeric string like "3.2"
            "velocity_all_time_per_day": float,
            "cycle_time_median_hours":   float | None,
            "window_start":              str,   # ISO8601 date string (UTC today)
        }

    Raises UnresolvableProjectError when a named project resolves to no
    GitHub repo slug -- declining rather than reporting the serving
    checkout's DORA/CFR numbers under that project's name. Surfaces as a
    JSON-RPC error, distinguishable from a normal (possibly empty) response.
    """
    project = params.get("project") or None

    project_root = None
    repo = None
    if project:
        from backend.project_repo_slug import resolve_project_repo_slug  # noqa: PLC0415

        repo = resolve_project_repo_slug(project)
        if repo is None:
            from backend.rpc_project_scope import UnresolvableProjectError  # noqa: PLC0415

            raise UnresolvableProjectError(
                f"stats.dora: project {project!r} resolves to no GitHub repo "
                "slug (no 'repo' field in its dashboard-runtime.json or "
                "project.json) -- declining rather than reporting the "
                "serving checkout's DORA/CFR numbers under this project's "
                "name"
            )

        from backend.state_paths import for_project as _fp  # noqa: PLC0415

        project_root = _fp(project).state_dir.parent / project

    from backend.analytics_engineer import compute_snapshot  # noqa: PLC0415

    try:
        snap = compute_snapshot(project_root=project_root, repo=repo)
    except Exception:  # noqa: BLE001
        return {"applicable": False}

    deploy_freq = snap.get("deploy_frequency_per_day", 0.0)
    lead_time = snap.get("lead_time_minutes_p50", -1.0)
    velocity = snap.get("velocity_all_time_per_day", 0.0)
    cycle_time = snap.get("cycle_time_median_hours")
    cfr = snap.get("change_failure_rate_pct", "n/a")

    # applicable=False when there is no meaningful data at all
    has_data = deploy_freq > 0 or (isinstance(lead_time, (int, float)) and lead_time >= 0)
    applicable = bool(has_data)

    return {
        "applicable": applicable,
        "deploy_frequency_per_day": deploy_freq,
        "lead_time_minutes_p50": lead_time,
        "change_failure_rate_pct": cfr,  # verbatim — may be "n/a"
        "velocity_all_time_per_day": velocity,
        "cycle_time_median_hours": cycle_time,
        "window_start": snap.get("date", ""),
    }
