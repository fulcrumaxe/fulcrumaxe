"""
FastAPI router — control-plane POST + PATCH endpoints.

Migrates from api.py:
  POST  /control/set                        (line 3833) — set a dial/gate value
  PATCH /api/projects/{name}/control        (line 4003) — bulk-update ControlSettings

Both require bearer auth + RBAC.

IMPORTANT: ControlPlane.set() behaviour is preserved EXACTLY — the router
delegates all logic to the same module the legacy handler uses.  No
reimplementation.

Tests MUST mock ControlPlane.set() / ControlPlane.load() / ControlPlane._path
to avoid touching .autonomous-team/config.json.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.control_plane import ControlPlane
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["ops-control"],
    dependencies=[Depends(require_auth)],
)

# ---------------------------------------------------------------------------
# POST /control/set
# ---------------------------------------------------------------------------

@router.post(
    "/control/set",
    summary="Set a control-plane dial or gate",
    description=(
        "Sets a single control-plane key to the given value. "
        "Body: {\"key\": str, \"value\": any}. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/control/set"))],
)
async def ops_control_set(request: Request) -> Any:
    """Control-plane set — mirrors api.py:3833-3846."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    key = body.get("key")
    if key is None:
        raise HTTPException(status_code=400, detail="'key' is required")
    if "value" not in body:
        raise HTTPException(status_code=400, detail="'value' is required")

    value = body.get("value")
    cp = ControlPlane()
    cp.load()
    cp.set(key, value)
    return {"ok": True, "key": key, "value": value}


# ---------------------------------------------------------------------------
# PATCH /api/projects/{name}/control
# ---------------------------------------------------------------------------

# Maps ControlSettings dashboard field names → (control_plane dot-key, coerce_type).
# This mapping is defined at module level, mirroring api.py:4008-4015.
_SETTINGS_MAP: dict[str, tuple[str, type]] = {
    "autoMerge":             ("gates.auto_merge",                          bool),
    "requireSecurityReview": ("gates.security_review",                     bool),
    "maxConcurrentAgents":   ("policies.executor.max_concurrent",           int),
    "budgetAlertEnabled":    ("gates.budget_check",                        bool),
    "qualityGateThreshold":  ("policies.code_reviewer.quality_threshold",  float),
    "loopIntervalMinutes":   ("loop_interval_minutes",                     int),
}


@router.patch(
    "/api/projects/{name}/control",
    summary="Update project ControlSettings",
    description=(
        "Maps ControlSettings dashboard fields back to control_plane keys. "
        "The project name is ignored (single-tenant). "
        "Returns the full current settings so the UI can sync. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("PATCH", "/api/projects/{name}/control"))],
)
async def ops_projects_control_patch(name: str, request: Request) -> Any:
    """PATCH project control settings — mirrors api.py:4003-4053."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    # Validate maxConcurrentAgents before applying any writes (api.py:4021-4030).
    if "maxConcurrentAgents" in body:
        _mac = body["maxConcurrentAgents"]
        try:
            _mac_int = int(_mac)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="maxConcurrentAgents must be an integer")
        if _mac_int <= 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "maxConcurrentAgents must be > 0 "
                    "(0 would lock all spawns; use stop-dashboard.sh instead)"
                ),
            )

    cp = ControlPlane()
    cp.load()
    for field, (cp_key, coerce) in _SETTINGS_MAP.items():
        if field in body:
            val = coerce(body[field])
            cp.set(cp_key, val)

    # Return the full current settings so the UI can sync (api.py:4038-4053).
    cfg_text = cp._path.read_text()
    cfg = json.loads(cfg_text)
    gates = cfg.get("gates") or {}
    policies = cfg.get("policies") or {}
    return {
        "autoMerge": bool(gates.get("auto_merge", True)),
        "requireSecurityReview": bool(gates.get("security_review", True)),
        "maxConcurrentAgents": int(
            (policies.get("executor") or {}).get("max_concurrent", 3)
        ),
        "loopIntervalMinutes": int(cfg.get("loop_interval_minutes", 10)),
        "budgetAlertEnabled": bool(gates.get("budget_check", True)),
        "qualityGateThreshold": float(
            (policies.get("code_reviewer") or {}).get("quality_threshold", 0.8)
        ),
    }
