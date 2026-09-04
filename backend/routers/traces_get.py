"""
FastAPI router — GET /traces and GET /traces/{trace_id}.

Migrates two legacy read-only GET handlers from backend/api.py:

    GET /traces            (api.py:3351) — list recent traces, grouped by trace_id
    GET /traces/{trace_id} (api.py:3418) — single trace detail by trace_id

NOTE: GET /traces/stats is already native in backend/routers/stats.py — this
router deliberately omits it to avoid duplicate route registration.

Auth model: both routes sit after the _check_auth() + _check_rbac("GET", path)
gates in the legacy server (api.py:2671, 2674).  require_auth + make_require_rbac
are applied to mirror that exactly.

Response shapes are identical to the legacy handlers.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac
from backend.tracing import get_collector
from backend.trace_export import export_spans as _export_spans

router = APIRouter(
    tags=["traces"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/traces",
    summary="List recent traces",
    description=(
        "Returns the most recent traces as grouped span lists. "
        "Optional ?limit=N (default 50) controls how many traces to return. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/traces"))],
)
def list_traces(
    limit: Optional[int] = Query(
        default=50,
        ge=1,
        description="Maximum number of traces to return (default 50).",
    ),
) -> Any:
    """List traces — mirrors api.py:3351-3371.

    Peeks the in-memory span collector, groups spans by trace_id, and returns
    the most recent ``limit`` traces with their exported resourceSpans.
    """
    limit_n = limit if limit is not None else 50
    spans = get_collector().peek(limit_n * 20)
    traces_map: dict = {}
    for sp in spans:
        traces_map.setdefault(sp.trace_id, []).append(sp)
    trace_list = []
    for tid, trace_spans in list(traces_map.items())[-limit_n:]:
        trace_list.append({
            "trace_id": tid,
            "span_count": len(trace_spans),
            "resourceSpans": _export_spans(trace_spans)["resourceSpans"],
        })
    return {"traces": trace_list, "count": len(trace_list)}


@router.get(
    "/traces/{trace_id}",
    summary="Get a single trace by ID",
    description=(
        "Returns all spans for a specific trace_id. "
        "Returns 404 if the trace_id is not found in the in-memory collector. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/traces/{trace_id}"))],
)
def get_trace(trace_id: str) -> Any:
    """Single trace detail — mirrors api.py:3418-3433.

    Peeks all spans and returns those matching the given trace_id.
    Returns 404 if no spans match.
    """
    if not trace_id:
        raise HTTPException(status_code=400, detail="trace_id required")
    spans = get_collector().peek(10000)
    matched = [sp for sp in spans if sp.trace_id == trace_id]
    if not matched:
        raise HTTPException(status_code=404, detail=f"trace '{trace_id}' not found")
    return {
        "trace_id": trace_id,
        "span_count": len(matched),
        "resourceSpans": _export_spans(matched)["resourceSpans"],
    }
