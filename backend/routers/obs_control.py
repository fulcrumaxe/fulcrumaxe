"""
FastAPI router — control plane read-only GET routes.

Migrates from api.py:
  GET /control        (line 2763) — full gates + policies summary
  GET /control/gates  (line 2773) — gate list only
  GET /control/audit  (line 2778) — control-plane audit log

NOTE: POST /control/set is already migrated in P5c (backend/routers/ops_control.py).
      This file handles the three read-only GET endpoints.

All three require auth + RBAC (they sit after _check_auth()/_check_rbac() at
api.py:2671/2674).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend.control_plane import ControlPlane
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["observability"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/control",
    summary="Control plane summary",
    description=(
        "Returns all gates and per-role policies. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/control"))],
)
def control() -> Any:
    """Control plane summary — mirrors api.py:2763-2771."""
    cp = ControlPlane()
    cp.load()
    policies = {
        role: cp.get_policy(role)
        for role in ("executor", "code-reviewer", "security-reviewer", "project-manager")
    }
    return {"gates": cp.list_gates(), "policies": policies}


@router.get(
    "/control/gates",
    summary="Control plane gates",
    description="Returns the list of all configured gates. Requires authentication.",
    dependencies=[Depends(make_require_rbac("GET", "/control/gates"))],
)
def control_gates() -> Any:
    """Control gates — mirrors api.py:2773-2776."""
    cp = ControlPlane()
    cp.load()
    return cp.list_gates()


@router.get(
    "/control/audit",
    summary="Control plane audit log",
    description="Returns the control-plane audit log. Requires authentication.",
    dependencies=[Depends(make_require_rbac("GET", "/control/audit"))],
)
def control_audit() -> Any:
    """Control audit — mirrors api.py:2778-2781."""
    cp = ControlPlane()
    cp.load()
    return cp.get_audit_log()
