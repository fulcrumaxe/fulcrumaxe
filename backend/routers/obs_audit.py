"""
FastAPI router — /audit GET endpoint.

Migrates from api.py:2783-2797.
  GET /audit  — query the audit trail with optional filters

Query params:
  source, action, actor, since  — filter strings (optional)
  limit                         — integer, default 50

NOTE: /audit/stats is already migrated in P2 (backend/routers/stats.py).
      This file handles /audit only.

Requires auth + RBAC.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from backend.audit_trail import get_audit_trail
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["observability"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/audit",
    summary="Audit trail query",
    description=(
        "Returns audit trail entries with optional source/action/actor/since/limit filters. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/audit"))],
)
def audit(
    source: Optional[str] = Query(default=None, description="Filter by source"),
    action: Optional[str] = Query(default=None, description="Filter by action"),
    actor: Optional[str] = Query(default=None, description="Filter by actor"),
    since: Optional[str] = Query(default=None, description="Filter by timestamp (ISO8601)"),
    limit: int = Query(default=50, description="Maximum number of entries to return"),
) -> Any:
    """Audit trail query — mirrors api.py:2783-2797."""
    at = get_audit_trail()
    return at.query(source=source, action=action, actor=actor, since=since, limit=limit)
