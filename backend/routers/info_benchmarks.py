"""
FastAPI router — benchmark GET routes.

Migrates from api.py:
  GET /benchmarks                     (line 3306) — all stats for a window
  GET /benchmarks/history             (line 3319) — time-series history
  GET /benchmarks/{category}          (line 3334) — stats for a category
  GET /benchmarks/{category}/{operation} (line 3334) — stats for category+op

All require bearer auth + RBAC("GET", path).

IMPORTANT: /benchmarks/history is a fixed path and MUST be registered
before the parameterised /benchmarks/{category} route so FastAPI matches
it first.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Query

from backend.benchmarks import _stats_to_dict, get_recorder
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["info-benchmarks"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/benchmarks",
    summary="All benchmark stats for a window",
    description=(
        "Returns aggregate stats for all categories. "
        "Query param: window (int seconds, default 300). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/benchmarks"))],
)
def benchmarks_all(window: int = Query(300)) -> Any:
    """All benchmark stats — mirrors api.py:3306-3317."""
    rec = get_recorder()
    all_stats = rec.get_all_stats(window_seconds=window)
    return {
        "window_seconds": window,
        "stats": [_stats_to_dict(s) for s in all_stats],
    }


@router.get(
    "/benchmarks/history",
    summary="Benchmark time-series history",
    description=(
        "Returns time-series history for a category/operation. "
        "Query params: category (str, default 'http'), "
        "operation (str, optional), points (int, default 60). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/benchmarks/history"))],
)
def benchmarks_history(
    category: str = Query("http"),
    operation: Optional[str] = Query(None),
    points: int = Query(60),
) -> Any:
    """Benchmark history — mirrors api.py:3319-3332."""
    rec = get_recorder()
    history = rec.get_history(category=category, operation=operation, points=points)
    return {"category": category, "operation": operation, "history": history}


@router.get(
    "/benchmarks/history/{extra:path}",
    summary="Benchmark time-series history (sub-path alias)",
    description=(
        "Alias for /benchmarks/history — any sub-path is accepted and ignored. "
        "Query params: category, operation, points (same as /benchmarks/history). "
        "Exists to provide native coverage for the legacy startswith('/benchmarks/history') "
        "handler. Requires authentication."
    ),
    include_in_schema=False,
    dependencies=[Depends(make_require_rbac("GET", "/benchmarks/history"))],
)
def benchmarks_history_subpath(
    extra: str,  # noqa: ARG001 — consumed from path but not used
    category: str = Query("http"),
    operation: Optional[str] = Query(None),
    points: int = Query(60),
) -> Any:
    """Benchmarks history sub-path alias — covers api.py:3319 startswith branch."""
    rec = get_recorder()
    history = rec.get_history(category=category, operation=operation, points=points)
    return {"category": category, "operation": operation, "history": history}


@router.get(
    "/benchmarks/{category}",
    summary="Benchmark stats for a category",
    description=(
        "Returns stats for the given category (optionally a sub-path "
        "category/operation). "
        "Query param: window (int seconds, default 300). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/benchmarks/{category}"))],
)
def benchmarks_category(
    category: str,
    window: int = Query(300),
) -> Any:
    """Benchmark by category — mirrors api.py:3334-3349 (category only)."""
    # URL-decode the path segment (legacy uses urllib.parse.unquote)
    category_decoded = unquote(category)
    rec = get_recorder()
    stats = rec.compute_stats(
        category=category_decoded,
        operation=None,
        window_seconds=window,
    )
    return _stats_to_dict(stats)


@router.get(
    "/benchmarks/{category}/{operation}",
    summary="Benchmark stats for a category and operation",
    description=(
        "Returns stats for the given category and operation. "
        "Query param: window (int seconds, default 300). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/benchmarks/{category}/{operation}"))],
)
def benchmarks_category_operation(
    category: str,
    operation: str,
    window: int = Query(300),
) -> Any:
    """Benchmark by category+operation — mirrors api.py:3334-3349 (both parts)."""
    category_decoded = unquote(category)
    operation_decoded = unquote(operation)
    rec = get_recorder()
    stats = rec.compute_stats(
        category=category_decoded,
        operation=operation_decoded,
        window_seconds=window,
    )
    return _stats_to_dict(stats)
