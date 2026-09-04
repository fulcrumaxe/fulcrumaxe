"""RPC handler: stats.role_retry_rate

Return per-role retry rates over the last 24 hours (Discussion #540).
"""
from backend.stats_writer import role_retry_rate_24h as _rate


def handle(params: dict) -> dict:
    """Return per-role retry rates.

    Response: {"rows": [{"role", "retry_rate", "sample_size"}, ...]}
    retry_rate is null when sample_size < 5 (UI shows "N/A").
    retry_rate = count(needs-fix|fail) / count(all verdicts) per role.
    """
    rows = _rate()
    return {"rows": rows}
