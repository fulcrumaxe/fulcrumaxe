"""RPC handler: stats.dial_rejections

Returns 24h counts of rejected directives and sandbox-block events.

Pure read — no writes, no spawns, no counters incremented.
"""
from __future__ import annotations

from backend.stats.dial_rejections import read_dial_rejections


def handle(params: dict) -> dict:
    """Return dial-rejection telemetry for the requested project.

    Params: {"project_name": str}  (omit or None for AF default)

    Response shape matches read_dial_rejections() return value:
        {
            "rejected_directives_24h": {
                "total": int,
                "by_reason": {<reason>: int},
                "last_at": str | None,
            },
            "sandbox_blocks_24h": {
                "total": int,
                "by_kind": {
                    "sandbox_block_agent_spawn":     int,
                    "sandbox_block_gh_api_mutation": int,
                    "sandbox_block_untrusted_cwd":   int,
                },
                "last_at": str | None,
            },
            "last_rejection": {
                "kind": str,
                "reason_or_class": str,
                "timestamp": str,
                "cwd": str | None,
            } | None,
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
    return read_dial_rejections(state_dir=state_dir)
