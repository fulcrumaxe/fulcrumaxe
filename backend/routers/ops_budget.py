"""
FastAPI router — budget operational POST endpoint.

Migrates from api.py:
  POST /budget/init  (line 3826) — initialise a budget session

Requires bearer auth + RBAC("POST", "/budget/init").
Side-effects: mutates the blackboard via BudgetTracker.init_session().
Tests MUST mock BudgetTracker.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from backend.budget import BudgetTracker
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["ops-budget"],
    dependencies=[Depends(require_auth)],
)


@router.post(
    "/budget/init",
    summary="Initialise a budget session",
    description=(
        "Initialises a new budget tracking session. "
        "Optional body: {\"ceiling\": int}. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/budget/init"))],
)
async def ops_budget_init(request: Request) -> Any:
    """Budget init — mirrors api.py:3826-3831."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    ceiling = body.get("ceiling")
    bt = BudgetTracker()
    bt.init_session(ceiling=ceiling)
    return {"ok": True, "status": bt.get_status()}
