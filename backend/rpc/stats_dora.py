"""RPC handler: stats.dora

Returns DORA metrics (deploy frequency, lead time, change failure rate) and
KPI metrics (velocity, cycle time) by delegating to
analytics_engineer.compute_snapshot — no independent recomputation.

change_failure_rate_pct is passed through verbatim as a string (e.g. "n/a"
when no bug data is available) rather than coerced to a number.

Project scoping (D#2327 PR-a): this handler is UNSCOPABLE, and `params` is
ignored because there is nothing a per-request override could reach. Every
source compute_snapshot() reads is bound to the serving checkout at import:
analytics_engineer._RELEASES_DIR and kpi_engine.REGISTRY are module
constants built from Path(__file__).resolve().parent.parent, and
_compute_cfr() shells `gh api graphql` at analytics_engineer's module-level
REPO. `dispatch_scoped` now refuses a cross-project call here rather than
answering it with the serving checkout's DORA numbers. Fixing this means
de-anchoring analytics_engineer, release_manager and kpi_engine, which is
not in scope for the audit that found it.
"""
from __future__ import annotations


def handle(params: dict) -> dict:  # noqa: ARG001 — UNSCOPABLE, see module docstring
    """Return DORA + KPI snapshot for the dashboard.

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
    """
    from backend.analytics_engineer import compute_snapshot  # noqa: PLC0415

    try:
        snap = compute_snapshot()
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
