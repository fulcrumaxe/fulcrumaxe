"""
FastAPI router — ideas and spawn-blocks GET + POST endpoints.

Migrates from api.py:
  GET  /api/ideas        (line 2558) — pending ideas feed
  GET  /api/spawn-blocks (line 2570) — recent blocked-spawn events
  POST /api/ideas/{id}/upvote   (line 3606) — upvote an idea (auth + RBAC)
  POST /api/ideas/{id}/dismiss  (line 3619) — dismiss an idea (auth + RBAC)
  POST /api/ideas/{id}/promote  (line 3632) — promote an idea to Discussion (auth + RBAC)

GET routes sit before _check_auth in the legacy flow — no bearer token needed.
POST routes are mutations and require require_auth + RBAC("POST", path).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api import _load_ideas, _spawn_blocks_list, upvote_idea, dismiss_idea, promote_idea
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(tags=["api-ideas"])


@router.get(
    "/api/ideas",
    summary="Pending ideas feed",
    description=(
        "Returns the pending ideas list from the project-manager blackboard. "
        "Includes source_empty flag and fetched_at timestamp."
    ),
)
def api_ideas() -> Any:
    """Ideas feed — mirrors api.py:2558-2565."""
    ideas_list, source_empty = _load_ideas()
    return {
        "ideas": ideas_list,
        "source_empty": source_empty,
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@router.get(
    "/api/spawn-blocks",
    summary="Recent blocked-spawn events",
    description=(
        "Returns recent events where a spawn was blocked by the guard. "
        "?limit=N controls how many to return (1-100, default 10)."
    ),
)
def api_spawn_blocks(
    limit: int = Query(default=10, description="Max events to return (1-100)."),
) -> Any:
    """Spawn-blocks feed — mirrors api.py:2570-2578."""
    limit = max(1, min(limit, 100))
    return _spawn_blocks_list(limit=limit)


# ---------------------------------------------------------------------------
# POST mutation endpoints (require auth + RBAC)
# ---------------------------------------------------------------------------

@router.post(
    "/api/ideas/{idea_id}/upvote",
    summary="Upvote an idea",
    description="Increments the upvote counter for an idea. Requires auth.",
    dependencies=[
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/ideas")),
    ],
)
def api_idea_upvote(idea_id: str) -> Any:
    """Upvote an idea — mirrors api.py:3606-3617."""
    try:
        return upvote_idea(idea_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/ideas/{idea_id}/dismiss",
    summary="Dismiss an idea",
    description="Marks an idea as dismissed. Requires auth.",
    dependencies=[
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/ideas")),
    ],
)
def api_idea_dismiss(idea_id: str) -> Any:
    """Dismiss an idea — mirrors api.py:3619-3630."""
    try:
        return dismiss_idea(idea_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/ideas/{idea_id}/promote",
    summary="Promote an idea to Discussion",
    description="Promotes an idea to a GitHub Discussion. Requires auth.",
    dependencies=[
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/ideas")),
    ],
)
def api_idea_promote(idea_id: str) -> Any:
    """Promote an idea — mirrors api.py:3632-3643."""
    try:
        return promote_idea(idea_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
