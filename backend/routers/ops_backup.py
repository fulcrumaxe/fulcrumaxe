"""
FastAPI router — backup POST endpoints.

Migrates from api.py:
  POST /backup          (line 3861) — create a state-dir backup
  POST /backup/restore  (line 3866) — restore a named backup

Both require bearer auth + RBAC("POST", path).
Delegates to backend.backup (imported as _backup in the legacy handler).

CRITICAL: Tests MUST mock backend.backup.create_backup,
backend.backup.prune_backups, and backend.backup.restore_backup.
These functions operate on the real state directory — never call
them in tests without mocking.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

import backend.backup as _backup
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["ops-backup"],
    dependencies=[Depends(require_auth)],
)


@router.post(
    "/backup",
    summary="Create a state-dir backup",
    description=(
        "Creates a backup of the state directory and prunes old backups (keep=20). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/backup"))],
)
def ops_backup_create() -> Any:
    """Backup create — mirrors api.py:3861-3864."""
    info = _backup.create_backup()
    _backup.prune_backups(keep=20)
    return info


@router.post(
    "/backup/restore",
    summary="Restore a named backup",
    description=(
        "Restores the state directory from a named backup file. "
        "Body: {\"filename\": str}. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/backup/restore"))],
)
async def ops_backup_restore(request: Request) -> Any:
    """Backup restore — mirrors api.py:3866-3878."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    filename = body.get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail="'filename' is required")

    try:
        result = _backup.restore_backup(filename)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
