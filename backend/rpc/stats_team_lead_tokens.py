"""RPC handler: stats.team_lead_tokens

Return avg / p50 / p95 of team_lead_tokens_per_iter over the last N hours.
"""
from backend.stats_writer import team_lead_tokens_percentiles as _percentiles


def handle(params: dict) -> dict:
    """Return team lead token percentiles.

    Params: since_hours (optional, default 24)
    Response: {"avg", "p50", "p95", "sample_size"}
    When sample_size < 5, avg/p50/p95 are null (UI renders "N/A").
    """
    since_hours = int(params.get("since_hours", 24))
    return _percentiles(since_hours=since_hours)
