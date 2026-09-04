"""RPC handler: stats.cost_spike_history

Return recent cost spike events (Discussion #540 metric #22).
"""
from backend.stats_writer import cost_spike_history as _history


def handle(params: dict) -> dict:
    """Return cost spike history.

    Params: hours (optional, default 24)
    Response: {
        "spikes": [{"ts_iso", "value", "mu", "sigma"}, ...],
        "count": int,
        "last_spike_iso": str | null
    }
    """
    hours = int(params.get("hours", 24))
    spikes = _history(hours=hours)
    return {
        "spikes": spikes,
        "count": len(spikes),
        "last_spike_iso": spikes[0]["ts_iso"] if spikes else None,
    }
