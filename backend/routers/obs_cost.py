"""
FastAPI router — cost and budget read-only GET routes.

Migrates from api.py:
  GET /budget/status  (line 2697) — budget tracker status
  GET /cost           (line 2701) — session cost totals
  GET /cost/summary   (line 2705) — aggregated cost summary

All three sit after the _check_auth() + _check_rbac("GET", path) gates in
the legacy server, so require_auth + make_require_rbac are applied.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend.budget import BudgetTracker
from backend.cost_tracker import CostTracker
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["observability"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/budget/status",
    summary="Budget tracker status",
    description="Returns current budget session status. Requires authentication.",
    dependencies=[Depends(make_require_rbac("GET", "/budget/status"))],
)
def budget_status() -> Any:
    """Budget status — mirrors api.py:2697-2699."""
    bt = BudgetTracker()
    return bt.get_status()


@router.get(
    "/cost",
    summary="Session cost totals",
    description="Returns current session cost totals. Requires authentication.",
    dependencies=[Depends(make_require_rbac("GET", "/cost"))],
)
def cost() -> Any:
    """Session cost — mirrors api.py:2701-2703."""
    ct = CostTracker()
    return ct.get_session_cost()


@router.get(
    "/cost/summary",
    summary="Aggregated cost summary",
    description="Returns aggregated cost summary across sessions. Requires authentication.",
    dependencies=[Depends(make_require_rbac("GET", "/cost/summary"))],
)
def cost_summary() -> Any:
    """Cost summary — mirrors api.py:2705-2707."""
    ct = CostTracker()
    return ct.get_summary()
