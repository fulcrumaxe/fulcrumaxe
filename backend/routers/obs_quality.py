"""
FastAPI router — /quality GET endpoints.

Migrates from api.py:
  GET /quality         (line 3242) — last 20 quality scores for PRs
  GET /quality/{n}     (line 3252) — quality score for a single PR by number

NOTE: /quality/stats is already migrated in P2 (backend/routers/stats.py).
      This file handles /quality (list) and /quality/{pr_number} (per-PR).

Requires auth + RBAC.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["observability"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/quality",
    summary="Quality scorer history",
    description=(
        "Returns the last 20 quality scores for merged PRs. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/quality"))],
)
def quality() -> Any:
    """Quality history — mirrors api.py:3242-3245."""
    from backend.quality_scorer import QualityScorer  # noqa: PLC0415 — deferred import same as legacy
    qs = QualityScorer()
    return {"scores": qs.history(limit=20)}


@router.get(
    "/quality/{pr_number}",
    summary="Quality score for a single PR",
    description=(
        "Returns the stored quality score for the given PR number. "
        "Returns 404 if no score has been recorded for this PR. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/quality/{pr_number}"))],
)
def quality_pr(pr_number: int) -> Any:
    """Per-PR quality score — mirrors api.py:3252-3268."""
    from backend.quality_scorer import QualityScorer  # noqa: PLC0415 — deferred import same as legacy
    qs = QualityScorer()
    score = qs._bb.read(f"quality/{pr_number}")
    if score is None:
        raise HTTPException(status_code=404, detail=f"no quality score for PR #{pr_number}")
    return score
