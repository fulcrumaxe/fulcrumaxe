"""RPC handler: stats.cost_per_outcome

Return cost-per-merged-PR rows (Discussion #1401).

Project scoping (D#2327 PR-a)
------------------------------
``cost_per_outcome_rows()`` joins two sources: a ``gh pr list`` of merged PRs
and per-PR spend read from DuckDB. Only the second half followed
``_with_project_stats_db()``'s ``STATS_DB_PATH`` redirect. The first half
used ``backend._repo.REPO``, a module-level constant bound once at import
from the *serving* process's environment -- so a cross-project request
listed the serving checkout's PR numbers and priced them against the
requested project's spend records. PR numbers collide across repos, so that
is not a harmless empty result: project B's own PR #100 could be reported
under the engine's PR #100.

The repo slug is now resolved per request from the requested project, and a
project that resolves to no slug is declined rather than silently served the
serving checkout's PR list.
"""
from backend.cost_per_outcome import cost_per_outcome_rows as _rows
from backend.project_repo_slug import resolve_project_repo_slug as _resolve_repo


def handle(params: dict) -> dict:
    """Return cost-per-merged-PR data.

    Params (all optional):
        days    — look-back window in days (default 30)
        limit   — cap rows (default 0 = no cap)
        project — scope to this project's repo and spend records

    Response:
        {"rows": [{"pr", "usd", "total_tokens", "fix_rounds", "by_role"}, ...]}

    Rows are sorted by usd descending. PRs with no cost records are omitted.

    Raises UnresolvableProjectError when a named project resolves to no repo
    slug — surfaces as a JSON-RPC error the tile renders as text, rather
    than an empty rows list that reads as "this project cost nothing".
    """
    days = int(params.get("days", 30))
    limit = int(params.get("limit", 0))

    project = params.get("project") or None
    repo = _resolve_repo(project)
    if project and repo is None:
        from backend.rpc_project_scope import UnresolvableProjectError  # noqa: PLC0415

        raise UnresolvableProjectError(
            f"stats.cost_per_outcome: project {project!r} resolves to no "
            "GitHub repo slug (no 'repo' field in its dashboard-runtime.json "
            "or project.json) -- declining rather than listing the serving "
            "checkout's merged PRs against this project's spend records"
        )

    rows = _rows(days=days, repo=repo)
    if limit > 0:
        rows = rows[:limit]
    return {"rows": rows}
