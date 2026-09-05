"""backend/loop_metrics_path.py — resolve a project's loop-metrics.jsonl.

Why this exists (D#2327 PR-a)
-----------------------------
``stats.loop_idle_ratio`` was classified ``SCOPED`` on the strength of being
wrapped in ``_with_project_stats_db()``. It *is* wrapped — but that wrapper
scopes by redirecting ``STATS_DB_PATH``, and
``backend.stats_writer.loop_idle_ratio_24h()`` opens no DuckDB connection at
all. It reads ``.autonomous-team/loop-metrics.jsonl`` resolved from
``Path(__file__).resolve().parent.parent`` — the *serving checkout*. So the
wrapper ran, scoped nothing, and a request for project A's idle ratio came
back with the serving checkout's numbers.

``loop.timeline`` and ``loop.iteration_detail`` (``backend/server.py``)
already had the correct resolution for the same file, written out twice.
Both now call this module instead, so ``loop-metrics.jsonl`` resolves in
exactly one place for all three consumers.

Scope, precisely: this covers ``loop-metrics.jsonl`` and nothing else.
``backend/server.py``'s ``_agent_feed_path()`` resolves ``agent-feed.jsonl``
with the same *shape* of two-candidate lookup and still has its own copy —
it is a different file with a different consumer set (``agents.tail``,
``loop.events``), and folding it in here would mean a module that resolves
two unrelated files because their lookup happens to rhyme. Read this as
"one resolver for this file", not as a general de-duplication of path
lookups in ``server.py``.

What is deliberately *not* shared is the not-found policy. Each caller
decides, which is why this returns a path-or-``None`` rather than a value:

* ``loop.timeline`` returns ``[]`` — a list of zero iterations is an honest
  answer that says what it means.
* ``loop.iteration_detail`` carries on with an empty metrics row, because
  its response also carries the run log.
* ``stats.loop_idle_ratio`` declines outright, because a *ratio* has no
  honest zero — "0% idle" and "this project's metrics aren't reachable from
  here" are different claims and must not render the same.

Resolution order for a named project mirrors what both handlers did:

1. ``<state_dir>/loop-metrics.jsonl`` — the state-dir convention.
2. ``<state_dir>.parent/<project>/.autonomous-team/loop-metrics.jsonl`` —
   projects that write metrics into their own repo checkout.

Neither found returns ``None`` — see the note above on why that decision is
left to the caller.
"""

from __future__ import annotations

from pathlib import Path

# Default serving-checkout root, used when a caller does not supply one.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def candidate_paths(project: str) -> list[Path]:
    """Return every path :func:`resolve_loop_metrics_path` would try for
    *project*, in order — so a caller declining can name what it looked for
    instead of just saying "not found".
    """
    from backend.state_paths import for_project  # noqa: PLC0415

    paths = for_project(project)
    return [
        paths.state_dir / "loop-metrics.jsonl",
        paths.state_dir.parent / project / ".autonomous-team" / "loop-metrics.jsonl",
    ]


def resolve_loop_metrics_path(
    project: "str | None",
    serving_checkout_root: "Path | None" = None,
) -> "Path | None":
    """Return the loop-metrics.jsonl path for *project*, or ``None``.

    With *project* falsy, returns the serving checkout's own file when it
    exists (existing behaviour for the engine's own dashboard). With a named
    project, returns the first candidate that exists, and ``None`` when the
    project has no reachable metrics file — never the serving checkout's.

    *serving_checkout_root* exists so the caller keeps ownership of what
    "this checkout" means, resolved at call time. ``backend/server.py``
    passes its own module-level ``_REPO_ROOT``, which its tests patch with
    ``patch.object(srv, "_REPO_ROOT", tmp_path)`` to redirect the no-project
    path at a fixture tree. Freezing that root here instead would silently
    remove that seam — the resolution would look identical and stop honouring
    the patch, which is the kind of behaviour change a refactor is least
    likely to be checked for.
    """
    if not project:
        root = serving_checkout_root if serving_checkout_root is not None else _REPO_ROOT
        serving = Path(root) / ".autonomous-team" / "loop-metrics.jsonl"
        return serving if serving.exists() else None

    for candidate in candidate_paths(project):
        if candidate.exists():
            return candidate
    return None
