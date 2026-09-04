"""
FastAPI router — /registry GET endpoint.

Migrates from api.py:2709-2735.
  GET /registry   — full registry data + stats (?project= optional)

Requires auth + RBAC.  CWE-22 path-traversal guard on ?project= param.

NOTE: /registry/stats is already migrated in P2 (backend/routers/stats.py).
      This file handles /registry only.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac
from backend.registry import DiscussionRegistry
from backend.state_paths import for_project as _state_for_project

# CWE-22: same regex as api.py:157 and stats.py
_VALID_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_project_name(name: str) -> bool:
    return bool(_VALID_PROJECT_NAME_RE.fullmatch(name))


router = APIRouter(
    tags=["observability"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/registry",
    summary="Discussion registry",
    description=(
        "Returns full discussion registry data plus inline stats. "
        "Optional ?project= param scopes to a named project. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/registry"))],
)
def registry(
    project: Optional[str] = Query(
        default=None,
        description="Project slug to scope registry to (optional).",
    ),
) -> Any:
    """Registry — mirrors api.py:2709-2735."""
    # CWE-22 guard (api.py:2717-2719)
    if project and not _validate_project_name(project):
        raise HTTPException(status_code=400, detail=f"invalid project name: {project!r}")

    if project:
        try:
            state_paths = _state_for_project(project)
            state_dir = state_paths.state_dir / ".autonomous-team"
            if not state_dir.exists():
                state_dir = state_paths.state_dir
            reg = DiscussionRegistry(state_dir=state_dir)
        except Exception:  # noqa: BLE001
            # CWE-209: on error, return empty data — not AF state (api.py:2729)
            return {
                "discussions": [],
                "stats": {"done": 0, "total": 0, "in_progress": 0, "spec_ready": 0},
            }
    else:
        reg = DiscussionRegistry()

    data = reg.show()
    data["stats"] = reg.stats()
    return data
