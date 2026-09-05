"""RPC handler: stats.loop_idle_ratio

Return fraction of /loop iterations in last 24h where agents_spawned == 0.

Project scoping (D#2327 PR-a)
------------------------------
``loop_idle_ratio_24h()`` reads ``.autonomous-team/loop-metrics.jsonl``, not
DuckDB — so ``_with_project_stats_db()``'s ``STATS_DB_PATH`` redirect never
reached this handler's data. It was wrapped and unscoped: a request for
project A's idle ratio returned the serving checkout's numbers.

This resolves the requested project's own metrics file
(``backend.loop_metrics_path``) and passes it explicitly. When the project
has no reachable metrics file, it declines — rather than falling back to the
serving checkout, and rather than reporting sample_size 0, which reads on the
tile as "this project ran no iterations" when the truth is "we cannot see
this project's iterations from here".
"""
from backend.stats_writer import loop_idle_ratio_24h as _idle_ratio


def handle(params: dict) -> dict:
    """Return loop idle ratio.

    Params: project (optional)
    Response: {"ratio": float|null, "idle_count": int, "sample_size": int}
    ratio is null when sample_size < 5 (UI shows "N/A").

    Raises UnresolvableProjectError when a named project has no reachable
    loop-metrics.jsonl — surfaces as a JSON-RPC error the tile renders as
    text (TileBackendError), not as a zero.
    """
    from backend.loop_metrics_path import (  # noqa: PLC0415
        candidate_paths,
        resolve_loop_metrics_path,
    )

    project = params.get("project") or None
    if not project:
        return _idle_ratio()

    metrics_path = resolve_loop_metrics_path(project)
    if metrics_path is None:
        from backend.rpc_project_scope import UnresolvableProjectError  # noqa: PLC0415

        tried = ", ".join(str(p) for p in candidate_paths(project))
        raise UnresolvableProjectError(
            f"stats.loop_idle_ratio: no loop-metrics.jsonl found for project "
            f"{project!r} (tried: {tried}) — declining rather than reporting "
            "the serving checkout's idle ratio, or a zero sample that reads "
            "as 'this project ran no iterations'"
        )

    return _idle_ratio(str(metrics_path))
