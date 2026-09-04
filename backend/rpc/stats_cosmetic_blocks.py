"""RPC handler: stats.cosmetic_blocks

Return cosmetic-retry block events: total in last 24h and hourly buckets
for the last 7 days, sourced from the daily cosmetic-blocks JSONL telemetry
written by hooks/cosmetic_retry_breaker.py.

Delegates to backend.stats.cosmetic_blocks — the canonical reader module.
"""
from __future__ import annotations

from backend.stats.cosmetic_blocks import blocks_per_hour, total_blocks_24h


def handle(params: dict, project: str | None = None) -> dict:
    """Return cosmetic-retry block counts.

    Response: {
        "total_24h": int,
        "hourly_7d": [{"hour_iso": str, "count": int}, ...]  -- oldest first
    }
    """
    return {
        "total_24h": total_blocks_24h(project=project),
        "hourly_7d": blocks_per_hour(project=project),
    }
