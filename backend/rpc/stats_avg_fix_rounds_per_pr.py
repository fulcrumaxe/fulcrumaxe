"""RPC handler: stats.avg_fix_rounds_per_pr

Return avg fix rounds per merged PR over the last 24 hours (Discussion #540 Phase 3).
"""
from backend.stats_writer import avg_fix_rounds_24h as _avg


def handle(params: dict) -> dict:
    """Return average fix rounds per PR.

    Response: {
        "avg_last_24h": float | null,  -- null when sample_size < 5
        "sample_size": int,
        "distribution": {"0": N, "1": N, ...}  -- rounds -> count
    }
    """
    return _avg()
