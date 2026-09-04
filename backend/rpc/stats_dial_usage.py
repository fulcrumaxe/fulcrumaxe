"""RPC handler: stats.dial_usage

Returns current dial levels for all 13 classes plus 24h activity counters.
"""
from __future__ import annotations

from backend.stats.dial_usage import read_dial_usage


def handle(params: dict) -> dict:
    """Return dial-usage telemetry for the requested project.

    Params: {"project_name": str}  (omit or None for AF default)

    Response shape matches read_dial_usage() return value:
        {
            "current_dials":   [...],
            "last_24h": {
                "accepted": int,
                "rejected_by_reason": {...},
                "ceiling_violations": int,
                "last_ceiling_exceeded": {...} | None,
            },
        }
    """
    project = params.get("project_name") or None
    state_dir = None
    if project:
        try:
            from backend.state_paths import for_project as _fp  # noqa: PLC0415
            state_dir = _fp(project).state_dir
        except Exception:
            pass
    return read_dial_usage(state_dir=state_dir)
