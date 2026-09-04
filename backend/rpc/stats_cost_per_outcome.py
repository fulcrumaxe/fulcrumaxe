"""RPC handler: stats.cost_per_outcome

Return cost-per-merged-PR rows (Discussion #1401).
"""
from backend.cost_per_outcome import cost_per_outcome_rows as _rows


def handle(params: dict) -> dict:
    """Return cost-per-merged-PR data.

    Params (all optional):
        days  — look-back window in days (default 30)
        limit — cap rows (default 0 = no cap)

    Response:
        {"rows": [{"pr", "usd", "total_tokens", "fix_rounds", "by_role"}, ...]}

    Rows are sorted by usd descending. PRs with no cost records are omitted.
    """
    days = int(params.get("days", 30))
    limit = int(params.get("limit", 0))
    rows = _rows(days=days)
    if limit > 0:
        rows = rows[:limit]
    return {"rows": rows}
