"""
FastAPI router — memory GET routes.

Migrates from api.py:
  GET /memory/lessons   (line 3270) — query agent lessons
  GET /memory/context   (line 3292) — get context block for files

NOTE: /memory/stats is already migrated in P2 (backend/routers/stats.py).
      This file handles /memory/lessons and /memory/context only.

Both require bearer auth + RBAC("GET", path).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["info-memory"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/memory/lessons",
    summary="Query agent lessons",
    description=(
        "Returns agent lessons filtered by role, tags, and limit. "
        "Query params: role (str), tags (comma-separated str), limit (int, default 20). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/memory/lessons"))],
)
def memory_lessons(
    role: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    limit: int = Query(20),
) -> Any:
    """Memory lessons — mirrors api.py:3270-3285."""
    from backend.agent_memory import AgentMemory  # noqa: PLC0415 — deferred import same as legacy
    tags_filter = [t for t in (tags or "").split(",") if t] or None
    mem = AgentMemory()
    lessons = mem.query_lessons(
        tags=tags_filter,
        role=role,
        limit=limit,
        cross_session=True,
    )
    return {"lessons": lessons}


@router.get(
    "/memory/context",
    summary="Get context block for files",
    description=(
        "Returns a context block for the given files. "
        "Query param: files (comma-separated list, required). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/memory/context"))],
)
def memory_context(
    files: Optional[str] = Query(None),
) -> Any:
    """Memory context — mirrors api.py:3292-3304."""
    files_list = [f for f in (files or "").split(",") if f]
    if not files_list:
        raise HTTPException(status_code=400, detail="query param 'files' is required")
    from backend.agent_memory import AgentMemory  # noqa: PLC0415 — deferred import same as legacy
    mem = AgentMemory()
    block = mem.get_context_block(files=files_list)
    return {"context": block}
