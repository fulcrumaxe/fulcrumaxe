"""
FastAPI router — project mutation endpoints (P5c).

Migrates from api.py:
  DELETE /api/projects/{name}  (line 4063) — delete a project

The /api/projects/{name} GET route and POST create/budget-reset/loop-run are
already in backend/routers/api_projects.py (migrated in P5b).  This module
adds the DELETE method only, following the module-per-feature rule.

Requires bearer auth.  No explicit RBAC call in the legacy do_DELETE handler
(it only calls _check_auth, not _check_rbac) — this is preserved: auth is
checked, RBAC is not separately enforced for DELETE in the legacy handler.

The autonomous-forever project itself is protected from deletion (legacy 403).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.api import _delete_project
from backend.deps.auth import require_auth

router = APIRouter(
    tags=["ops-projects"],
    dependencies=[Depends(require_auth)],
)


@router.delete(
    "/api/projects/{pid}",
    summary="Delete a project",
    description=(
        "Deletes a project by id. "
        "The autonomous-forever project cannot be deleted (returns 403). "
        "Returns 404 if project not found. "
        "Requires authentication."
    ),
)
def ops_project_delete(pid: str) -> Any:
    """Project delete — mirrors api.py:4063-4087."""
    if pid == "autonomous-forever":
        raise HTTPException(
            status_code=403,
            detail="cannot delete the autonomous-forever project",
        )
    if not _delete_project(pid):
        raise HTTPException(status_code=404, detail=f"project {pid!r} not found")
    return {"ok": True, "id": pid}
