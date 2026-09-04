"""
FastAPI router — /deps GET endpoint.

Migrates from api.py:2891-2922.
  GET /deps   — dependency graph (?format=json|dot|ascii, ?module=name)

When format=dot or format=ascii the response is plain text (text/plain).
When format=json (default) the response is JSON.

Requires auth + RBAC.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse, Response

from backend.dep_graph import get_cached_dep_graph
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["observability"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/deps",
    summary="Dependency graph",
    description=(
        "Returns the module dependency graph. "
        "Optional ?format=json|dot|ascii, ?module=name. "
        "format=dot and format=ascii return text/plain; format=json (default) returns JSON. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/deps"))],
    # response_class is dynamic (text or JSON) so we omit a fixed response_model
)
def deps(
    format: Optional[str] = Query(  # noqa: A002 — matches legacy param name
        default="json",
        description="Output format: json (default), dot, or ascii.",
    ),
    module: Optional[str] = Query(
        default=None,
        description="Module name for impact analysis or ascii subtree (optional).",
    ),
) -> Any:
    """Dependency graph — mirrors api.py:2891-2922."""
    dg = get_cached_dep_graph()
    fmt = format or "json"

    if fmt == "dot":
        dot_text = dg.to_dot()
        return PlainTextResponse(content=dot_text, status_code=200)

    if fmt == "ascii":
        ascii_text = dg.to_ascii(module or None)
        return PlainTextResponse(content=ascii_text, status_code=200)

    # JSON (default)
    if module:
        return dg.impact(module)
    return dg.to_json()
