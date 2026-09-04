"""RPC handler: stats.role_success_rate

Return per-role success rates over the last 24 hours (Discussion #540).
"""
from backend.stats_writer import role_success_rate_24h as _rate


def handle(params: dict) -> dict:
    """Return per-role success rates.

    Response: {"rows": [{"role", "success_rate", "sample_size"}, ...]}
    success_rate is null when sample_size < 5 (UI shows "N/A").
    """
    rows = _rate()
    return {"rows": rows}
