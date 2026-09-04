"""RPC handler: stats.verdict_overturns

Return per-role verdict overturn rates over the last 24 hours (Discussion #1397).
"""
from backend.verdict_overturn import overturn_rate_by_role_24h as _rate


def handle(params: dict) -> dict:
    """Return per-role overturn rates.

    Response: {"rows": [{"role", "overturns", "total_pass", "overturn_rate", "sample_size"}, ...]}
    overturn_rate is null when sample_size < 5 (UI shows "N/A").
    """
    rows = _rate()
    return {"rows": rows}
