"""RPC handler: stats.loop_idle_ratio

Return fraction of /loop iterations in last 24h where agents_spawned == 0.
"""
from backend.stats_writer import loop_idle_ratio_24h as _idle_ratio


def handle(params: dict) -> dict:
    """Return loop idle ratio.

    Response: {"ratio": float|null, "idle_count": int, "sample_size": int}
    ratio is null when sample_size < 5 (UI shows "N/A").
    """
    return _idle_ratio()
