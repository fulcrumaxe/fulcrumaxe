"""
FastAPI router — POST /graphql endpoint.

Migrates from api.py:3965 (do_POST):
  POST /graphql — home-grown GraphQL (no external library)

Requires bearer auth + RBAC("POST", "/graphql").
Request body: {"query": "<graphql query string>"}
Missing/empty query → 400 "'query' is required" (exact legacy message).
Calls backend.graphql_api.execute(query) and returns the result dict.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac
import backend.graphql_api as _graphql

router = APIRouter(
    tags=["graphql"],
    dependencies=[Depends(require_auth)],
)


@router.post(
    "/graphql",
    summary="Execute a GraphQL query",
    description=(
        "Executes a home-grown GraphQL query against the fulcrumaxe data layer. "
        "Body: {\"query\": \"<graphql query string>\"}. "
        "Returns {\"data\": {...}} on success or {\"errors\": [...]} on failure. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/graphql"))],
)
async def post_graphql(request: Request) -> Any:
    """GraphQL execution — mirrors api.py:3965-3972."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    query_str = body.get("query")
    if not query_str:
        raise HTTPException(status_code=400, detail="'query' is required")

    result = _graphql.execute(query_str)
    return result
