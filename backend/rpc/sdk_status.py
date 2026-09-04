"""RPC handler: stats.sdk_lane

Returns the full SDK orchestrator status report — dispatcher readiness,
backend-would-select (credential PRESENCE only, never values), credit state,
and routing counts from the routed_via column.

Read-only: no state writes, no counters, no spawns, no network.

Response shape::

    {
      "generated_at": "2026-05-20T12:00:00Z",
      "readiness": {
        "dispatcher_live": false,
        "ROUTE_VIA_DISPATCHER": "(not set)",
        "SHADOW_MODE": "alternate",
        "SDK_BACKEND": "(not set)"
      },
      "backend_selection": {
        "would_select": "none",
        "reason": "no SDK credential — routes to Claude Code path",
        "CLAUDE_CODE_OAUTH_TOKEN": "absent",
        "ANTHROPIC_API_KEY": "absent"
      },
      "credit": {
        "remaining_usd": null,
        "used_usd": null,
        "soft_cap_breached": null,
        "exhausted": null,
        "billing_regime": null,
        "regime_note": null,
        "error": "..."
      },
      "routing_counts": {
        "total_runs_all_time": 0,
        "total_runs_last_30d": 0,
        "sdk_runs": 0,
        "cc_runs": 0,
        "null_route_runs": 0,
        "sdk_runs_estimate": "0 SDK runs (no credit consumed; dispatcher likely off)",
        "db_available": false,
        "note": "stats.duckdb not found — no telemetry yet"
      }
    }
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

# Allow running standalone (script mode) while keeping importable as a module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def handle(params: dict) -> dict:
    """Return the full SDK orchestrator status report dict.

    Purely read-only — calls sdk_status() which reads env vars and DuckDB
    (read_only=True). No state mutations, no spawns, no network calls.
    """
    from backend.orchestrator.sdk_status import sdk_status  # noqa: PLC0415
    return sdk_status()
