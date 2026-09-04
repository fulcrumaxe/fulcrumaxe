"""
FastAPI router — informational GET routes: /rbac/whoami, /backups,
/notifications/history, /validate.

Migrates from api.py:
  GET /rbac/whoami              (line 2685) — caller's role/permissions
  GET /backups                  (line 3197) — list state-dir backups
  GET /notifications/history    (line 3200) — last 50 notification dispatches
  GET /validate                 (line 2924) — validate all known config files

All four require bearer auth + RBAC("GET", path).
"""

from __future__ import annotations

from typing import Any

import backend.backup as _backup
from backend.deps.auth import require_auth
from backend.deps.rbac import _rbac_manager, make_require_rbac, _get_bearer
from fastapi import APIRouter, Depends, Request

router = APIRouter(
    tags=["info"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/rbac/whoami",
    summary="Caller's RBAC role and permissions",
    description=(
        "Returns the authenticated caller's role name, label, and permission list. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/rbac/whoami"))],
)
def rbac_whoami(request: Request) -> Any:
    """Whoami — mirrors api.py:2685-2695."""
    token = _get_bearer(request) or ""
    role_name = _rbac_manager.get_role_for_token(token)
    if role_name is None and not _rbac_manager.enabled:
        role_name = "unrestricted"
    role_info = _rbac_manager.get_role_info(role_name or "") or {}
    return {
        "role": role_name,
        "label": role_info.get("label", role_name),
        "permissions": role_info.get("allow", []),
    }


@router.get(
    "/backups",
    summary="List state-dir backups",
    description=(
        "Returns a list of available state-directory backups. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/backups"))],
)
def backups_list() -> Any:
    """Backups list — mirrors api.py:3197-3198."""
    return {"backups": _backup.list_backups()}


@router.get(
    "/notifications/history",
    summary="Notification dispatch history",
    description=(
        "Returns the last 50 notification dispatch records. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/notifications/history"))],
)
def notifications_history() -> Any:
    """Notification history — mirrors api.py:3200-3203."""
    from backend.notifier import get_notifier  # noqa: PLC0415 — deferred import same as legacy
    records = get_notifier().get_history(50)
    return {"notifications": records}


@router.get(
    "/validate",
    summary="Validate all known config files",
    description=(
        "Validates all known configuration files against their schemas. "
        "Returns per-file error lists and an overall valid flag. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/validate"))],
)
def validate_configs() -> Any:
    """Config validation — mirrors api.py:2924-2928."""
    from backend.schema_validator import SchemaValidator  # noqa: PLC0415 — deferred import same as legacy
    sv = SchemaValidator()
    results = sv.validate_all()
    all_valid = all(len(errors) == 0 for errors in results.values())
    return {"valid": all_valid, "files": results}
